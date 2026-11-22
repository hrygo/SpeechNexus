"""权威时间探针:先取得专用连接,写事务完成最后一次等待后再读取.

顺序是这条契约的全部要点(ADR 0016 第 7 条、设计 §7.1):

1. **开启写事务之前**从专用探针池取得一条连接;取不到就在写事务之前失败(限流),
   绝不允许「持有写锁再等共享池里的第二条连接」——那是池内互锁。
2. 开启写事务,按固定锁序取权威行锁。
3. 完成全部可能等待的写入(唯一键、外键、父行)。
4. 在专用连接上读取 `CURRENT_TIMESTAMP, schema_rev, schema_fingerprint, authority_epoch`,
   确认它属于同一权威主库与同一部署代次。
5. 回到写事务做最后一次资格复核;探针失败、代次不符或 deadline 到期即整笔回滚。

探针行不持久化时间;`CURRENT_TIMESTAMP` 只用于当前决定,不回退到应用时钟。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from speechnexus_portable_sql.contract import (
    Connection,
    ConnectionFactory,
    ContractError,
    Row,
)

__all__ = [
    "AUTHORITY_SQL",
    "SCHEMA_META_ID",
    "AuthorityMismatchError",
    "AuthorityProbe",
    "AuthorityProbePool",
    "AuthoritySnapshot",
    "ProbeUnavailableError",
    "SchemaMetaMissingError",
    "require_authority",
]

#: 唯一的 `schema_meta` 行:共同 `FROM` 锚点、schema 身份与权威代次。
SCHEMA_META_ID = "portable"

#: 权威探针。列名由驱动决定,所以按位置读 0..3。
AUTHORITY_SQL = (
    "SELECT CURRENT_TIMESTAMP, schema_rev, schema_fingerprint, authority_epoch "
    "FROM schema_meta WHERE meta_id = ?"
)


class SchemaMetaMissingError(RuntimeError):
    """`schema_meta` 没有当前 revision 行:数据库未 bootstrap 或连错实例。"""


class AuthorityMismatchError(RuntimeError):
    """探针返回的 schema / 代次不是本次决定期望的权威:可能连到副本或旧部署。"""


class ProbeUnavailableError(RuntimeError):
    """专用探针池已满:必须在开启写事务之前限流,不能持锁等待。"""


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    """一次探针读数;`at` 只对当前决定有效。"""

    at: datetime
    schema_rev: str
    schema_fingerprint: str
    authority_epoch: Decimal


def require_authority(
    snapshot: AuthoritySnapshot,
    *,
    schema_rev: str,
    schema_fingerprint: str,
    authority_epoch: Decimal,
) -> None:
    """比对探针读数与期望的 schema / 指纹 / 代次;任一不符即拒绝授予。"""

    if snapshot.schema_rev != schema_rev:
        raise AuthorityMismatchError(f"schema revision {snapshot.schema_rev!r} != {schema_rev!r}")
    if snapshot.schema_fingerprint != schema_fingerprint:
        raise AuthorityMismatchError("schema fingerprint does not match the deployed authority")
    if snapshot.authority_epoch != authority_epoch:
        raise AuthorityMismatchError(
            f"authority epoch {snapshot.authority_epoch} != {authority_epoch}"
        )


class AuthorityProbe:
    """一条专用探针连接;不参与业务写事务,也不与写事务共享连接池。"""

    __slots__ = ("_closed", "_connection")

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._closed = False

    def read(self, *, meta_id: str = SCHEMA_META_ID) -> AuthoritySnapshot:
        if self._closed:
            raise ContractError("probe connection is closed")
        cursor = self._connection.cursor()
        try:
            cursor.execute(AUTHORITY_SQL, (meta_id,))
            rows = cursor.fetchall()
        finally:
            cursor.close()
            # 只读探针不留下悬挂事务。
            self._connection.rollback()
        if len(rows) != 1:
            raise SchemaMetaMissingError(f"schema_meta has {len(rows)} rows for {meta_id!r}")
        return _snapshot(rows[0])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


class AuthorityProbePool:
    """固定容量的专用探针池.

    容量必须至少覆盖允许并发的写事务;`acquire()` 在池满时**立即**失败,让调用方在开启写事务
    之前限流。等待会重新引入「写事务等第二条连接」的互锁,所以这里不等待。
    """

    def __init__(self, factory: ConnectionFactory, *, capacity: int) -> None:
        if capacity < 1:
            raise ContractError("probe pool capacity must be at least 1")
        self._factory = factory
        self._capacity = capacity
        self._in_use = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    @property
    def available(self) -> int:
        with self._lock:
            return self._capacity - self._in_use

    def acquire(self, *, deadline: float | None = None) -> AuthorityProbe:
        if deadline is not None and time.monotonic() >= deadline:
            raise ProbeUnavailableError("deadline expired before the probe could be acquired")
        with self._lock:
            if self._in_use >= self._capacity:
                raise ProbeUnavailableError(
                    f"all {self._capacity} probe connections are in use; throttle before "
                    "opening a write transaction"
                )
            self._in_use += 1
        try:
            return AuthorityProbe(self._factory())
        except BaseException:
            self._release_slot()
            raise

    def release(self, probe: AuthorityProbe) -> None:
        probe.close()
        self._release_slot()

    @contextmanager
    def probe(self, *, deadline: float | None = None) -> Iterator[AuthorityProbe]:
        """取得并在退出时归还;异常路径同样归还槽位。"""

        acquired = self.acquire(deadline=deadline)
        try:
            yield acquired
        finally:
            self.release(acquired)

    def _release_slot(self) -> None:
        with self._lock:
            self._in_use = max(0, self._in_use - 1)


def _snapshot(row: Row) -> AuthoritySnapshot:
    at = row.at(0)
    revision = row.at(1)
    fingerprint = row.at(2)
    epoch = row.at(3)
    if not isinstance(at, datetime):
        raise ContractError(f"database time is {type(at).__name__}, not datetime")
    if not isinstance(revision, str) or not revision:
        raise ContractError("schema_rev must be a non-empty string")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ContractError("schema_fingerprint must be 64 hexadecimal characters")
    if not isinstance(epoch, Decimal):
        raise ContractError(f"authority_epoch is {type(epoch).__name__}, not Decimal")
    return AuthoritySnapshot(
        at=at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC),
        schema_rev=revision,
        schema_fingerprint=fingerprint,
        authority_epoch=epoch,
    )
