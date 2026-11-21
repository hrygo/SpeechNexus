"""准入预算:持久 grant、按锁序预留、幂等续租与一次回收.

语义边界只有三条:

1. **准入是一个事务**:先锁 `workspace_quotas`,再锁 `host_capacity_budgets`,再插入 grant,
   最后写 Job 与 outbox。任何路径都按这个顺序取锁,所以不会与回收或续租互锁。
2. **grant 是已提交的扣除,不是内存余额**:网关只能消费已提交的 `granted_units`,续租按
   `(session_id, meter_kind, authority_epoch, renew_seq)` 与 `version` 做 CAS,失败者读回原
   grant,不会凭空多出一份额度。
3. **时间与代次只能来自权威探针**:需要时间判断的写路径先拿到专用探针连接,开启写事务并完成
   全部可能阻塞的写入,再读 `CURRENT_TIMESTAMP / schema_rev / schema_fingerprint /
   authority_epoch`,回到写事务做最终复核;探针与写事务共享同一个硬 deadline。

本模块不读应用时钟、不回退副本时间、不分类品牌 SQLSTATE。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from speechnexus_portable_sql.contract import (
    Connection,
    ConnectionFactory,
    ContractError,
    Params,
)
from speechnexus_portable_sql.schema import (
    AuthorityMismatchError,
    AuthoritySnapshot,
    require_authority,
)
from speechnexus_portable_sql.transaction import transaction
from speechnexus_portable_sql.types import require_decimal, require_text, require_utc

__all__ = [
    "STATEMENTS",
    "Deadline",
    "Grant",
    "Reservation",
    "ReservationRequest",
    "deadline_after",
    "guarded_reserve",
    "hold_budget",
    "hold_quota",
    "insert_grant",
    "lock_budget",
    "lock_quota",
    "quarantine_grant",
    "read_grant",
    "reclaim_grant",
    "release_budget",
    "release_quota",
    "renew_grant",
    "require_final_eligibility",
    "reserve",
]

GRANT_ACTIVE = "active"
GRANT_RETURNED = "returned"
GRANT_UNKNOWN = "unknown"

STATEMENTS: dict[str, str] = {
    "quota.lock": (
        "SELECT limit_units, held_units, committed_units, version FROM workspace_quotas "
        "WHERE workspace_id = ? AND meter_kind = ? FOR UPDATE"
    ),
    "quota.hold": (
        "UPDATE workspace_quotas SET held_units = held_units + ?, version = version + 1 "
        "WHERE workspace_id = ? AND meter_kind = ? AND version = ? "
        "AND held_units + ? <= limit_units"
    ),
    "quota.release": (
        "UPDATE workspace_quotas SET held_units = held_units - ?, version = version + 1 "
        "WHERE workspace_id = ? AND meter_kind = ? AND version = ? AND held_units >= ?"
    ),
    "budget.lock": (
        "SELECT limit_units, held_units, version FROM host_capacity_budgets "
        "WHERE host_id = ? AND meter_kind = ? FOR UPDATE"
    ),
    "budget.hold": (
        "UPDATE host_capacity_budgets SET held_units = held_units + ?, version = version + 1 "
        "WHERE host_id = ? AND meter_kind = ? AND version = ? AND held_units + ? <= limit_units"
    ),
    "budget.release": (
        "UPDATE host_capacity_budgets SET held_units = held_units - ?, version = version + 1 "
        "WHERE host_id = ? AND meter_kind = ? AND version = ? AND held_units >= ?"
    ),
    "grant.insert": (
        "INSERT INTO admission_budget_grants (grant_id, session_id, lease_id, meter_kind, "
        "authority_epoch, renew_seq, granted_units, consumed_units, returned_units, version, "
        "grant_state, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "grant.read": (
        "SELECT grant_id, session_id, lease_id, meter_kind, authority_epoch, renew_seq, "
        "granted_units, consumed_units, returned_units, version, grant_state, expires_at "
        "FROM admission_budget_grants WHERE grant_id = ?"
    ),
    "grant.renew": (
        "UPDATE admission_budget_grants SET granted_units = granted_units + ?, renew_seq = ?, "
        "version = version + 1, expires_at = ? WHERE grant_id = ? AND version = ? "
        "AND authority_epoch = ? AND grant_state = ?"
    ),
    "grant.quarantine": (
        "UPDATE admission_budget_grants SET grant_state = ?, version = version + 1 "
        "WHERE grant_id = ? AND version = ? AND grant_state = ?"
    ),
    "grant.reclaim": (
        "UPDATE admission_budget_grants SET consumed_units = ?, returned_units = ?, "
        "grant_state = ?, version = version + 1 WHERE grant_id = ? AND version = ? "
        "AND grant_state = ?"
    ),
}


class ProbeReader(Protocol):
    """探针的读接口;真实 `AuthorityProbe` 与测试替身都满足。"""

    def read(self) -> AuthoritySnapshot: ...


class ProbePool(Protocol):
    """`AuthorityProbePool` 的结构契约:测试替身与真实池都可以传入。"""

    def acquire(self, *, deadline: float | None = None) -> ProbeReader: ...

    def release(self, probe: ProbeReader) -> None: ...


@dataclass(frozen=True, slots=True)
class GateRow:
    """quota / budget 的权威行;`version` 是后续 CAS 的锚点。"""

    limit_units: Decimal
    held_units: Decimal
    committed_units: Decimal
    version: Decimal


@dataclass(frozen=True, slots=True)
class Grant:
    grant_id: str
    session_id: str
    lease_id: str
    meter_kind: str
    authority_epoch: Decimal
    renew_seq: Decimal
    granted_units: Decimal
    consumed_units: Decimal
    returned_units: Decimal
    version: Decimal
    grant_state: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    grant_id: str
    workspace_id: str
    host_id: str
    meter_kind: str
    session_id: str
    lease_id: str
    units: Decimal
    lease_expires_at: datetime
    grant_expires_at: datetime


@dataclass(frozen=True, slots=True)
class Reservation:
    grant: Grant
    quota_version: Decimal
    budget_version: Decimal


class Deadline:
    """硬 deadline:所有阶段共享同一预算,任何一步超时都不能再提交。"""

    __slots__ = ("_budget", "_started")

    def __init__(self, budget_seconds: float) -> None:
        if budget_seconds <= 0:
            raise ContractError("deadline budget must be positive")
        self._budget = budget_seconds
        self._started = time.monotonic()

    def remaining(self) -> float:
        return self._budget - (time.monotonic() - self._started)

    def check(self, stage: str) -> None:
        if self.remaining() <= 0:
            raise ContractError(f"deadline expired before {stage}")

    def expires_at(self) -> float:
        return time.monotonic() + max(self.remaining(), 0.0)


def deadline_after(seconds: float) -> Deadline:
    return Deadline(seconds)


def _require_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ContractError(f"{field} is {type(value).__name__}, not datetime")
    return require_utc(value, field=field)


def _decimal(value: object, *, field: str) -> Decimal:
    return require_decimal(Decimal(str(value)), field=field)


def lock_quota(connection: Connection, *, workspace_id: str, meter_kind: str) -> GateRow | None:
    return _lock_gate(
        connection,
        statement=STATEMENTS["quota.lock"],
        params=(require_text(workspace_id, field="workspace_id", max_bytes=128), meter_kind),
        with_committed=True,
    )


def lock_budget(connection: Connection, *, host_id: str, meter_kind: str) -> GateRow | None:
    return _lock_gate(
        connection,
        statement=STATEMENTS["budget.lock"],
        params=(require_text(host_id, field="host_id", max_bytes=128), meter_kind),
        with_committed=False,
    )


def _lock_gate(
    connection: Connection, *, statement: str, params: Params, with_committed: bool
) -> GateRow | None:
    cursor = connection.cursor()
    try:
        cursor.execute(statement, params)
        rows = cursor.fetchall()
    finally:
        cursor.close()
    if not rows:
        return None
    row = rows[0]
    return GateRow(
        limit_units=_decimal(row.at(0), field="limit_units"),
        held_units=_decimal(row.at(1), field="held_units"),
        committed_units=_decimal(row.at(2), field="committed_units")
        if with_committed
        else Decimal(0),
        version=_decimal(row.at(-1), field="version"),
    )


def hold_quota(
    connection: Connection,
    *,
    workspace_id: str,
    meter_kind: str,
    units: Decimal,
    expected_version: Decimal,
) -> bool:
    return _adjust_gate(
        connection,
        statement=STATEMENTS["quota.hold"],
        params=(
            units,
            workspace_id,
            meter_kind,
            expected_version,
            units,
        ),
    )


def release_quota(
    connection: Connection,
    *,
    workspace_id: str,
    meter_kind: str,
    units: Decimal,
    expected_version: Decimal,
) -> bool:
    return _adjust_gate(
        connection,
        statement=STATEMENTS["quota.release"],
        params=(units, workspace_id, meter_kind, expected_version, units),
    )


def hold_budget(
    connection: Connection,
    *,
    host_id: str,
    meter_kind: str,
    units: Decimal,
    expected_version: Decimal,
) -> bool:
    return _adjust_gate(
        connection,
        statement=STATEMENTS["budget.hold"],
        params=(units, host_id, meter_kind, expected_version, units),
    )


def release_budget(
    connection: Connection,
    *,
    host_id: str,
    meter_kind: str,
    units: Decimal,
    expected_version: Decimal,
) -> bool:
    return _adjust_gate(
        connection,
        statement=STATEMENTS["budget.release"],
        params=(units, host_id, meter_kind, expected_version, units),
    )


def _adjust_gate(connection: Connection, *, statement: str, params: Params) -> bool:
    cursor = connection.cursor()
    try:
        cursor.execute(statement, params)
        return cursor.rowcount == 1
    finally:
        cursor.close()


def insert_grant(
    connection: Connection,
    *,
    grant_id: str,
    session_id: str,
    lease_id: str,
    meter_kind: str,
    authority_epoch: Decimal,
    units: Decimal,
    expires_at: datetime,
) -> Grant:
    require_text(grant_id, field="grant_id", max_bytes=128)
    require_text(session_id, field="session_id", max_bytes=128)
    require_text(lease_id, field="lease_id", max_bytes=128)
    require_text(meter_kind, field="meter_kind", max_bytes=24)
    require_decimal(units, field="units")
    expiry = require_utc(expires_at, field="expires_at")
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["grant.insert"],
            (
                grant_id,
                session_id,
                lease_id,
                meter_kind,
                authority_epoch,
                Decimal(0),
                units,
                Decimal(0),
                Decimal(0),
                Decimal(0),
                GRANT_ACTIVE,
                expiry,
            ),
        )
    finally:
        cursor.close()
    return Grant(
        grant_id=grant_id,
        session_id=session_id,
        lease_id=lease_id,
        meter_kind=meter_kind,
        authority_epoch=authority_epoch,
        renew_seq=Decimal(0),
        granted_units=units,
        consumed_units=Decimal(0),
        returned_units=Decimal(0),
        version=Decimal(0),
        grant_state=GRANT_ACTIVE,
        expires_at=expiry,
    )


def read_grant(connection: Connection, *, grant_id: str) -> Grant | None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["grant.read"], (require_text(grant_id, field="grant_id", max_bytes=128),)
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
    if not rows:
        return None
    row = rows[0]
    return Grant(
        grant_id=str(row.at(0)),
        session_id=str(row.at(1)),
        lease_id=str(row.at(2)),
        meter_kind=str(row.at(3)),
        authority_epoch=_decimal(row.at(4), field="authority_epoch"),
        renew_seq=_decimal(row.at(5), field="renew_seq"),
        granted_units=_decimal(row.at(6), field="granted_units"),
        consumed_units=_decimal(row.at(7), field="consumed_units"),
        returned_units=_decimal(row.at(8), field="returned_units"),
        version=_decimal(row.at(9), field="version"),
        grant_state=str(row.at(10)),
        expires_at=_require_datetime(row.at(11), field="expires_at"),
    )


def renew_grant(
    connection: Connection,
    *,
    grant_id: str,
    expected_version: Decimal,
    expected_epoch: Decimal,
    renew_seq: Decimal,
    additional_units: Decimal,
    expires_at: datetime,
) -> bool:
    """幂等续租:同 `renew_seq` 重放只会在第一次成功,旧代次一律 `rowcount = 0`。"""

    require_decimal(additional_units, field="additional_units")
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["grant.renew"],
            (
                additional_units,
                renew_seq,
                require_utc(expires_at, field="expires_at"),
                grant_id,
                expected_version,
                expected_epoch,
                GRANT_ACTIVE,
            ),
        )
        return cursor.rowcount == 1
    finally:
        cursor.close()


def quarantine_grant(connection: Connection, *, grant_id: str, expected_version: Decimal) -> bool:
    """缺少恢复证据时保留占用:状态转为 `unknown`,额度和 quota 都不动,绝不伪造零消费。"""

    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["grant.quarantine"],
            (GRANT_UNKNOWN, grant_id, expected_version, GRANT_ACTIVE),
        )
        return cursor.rowcount == 1
    finally:
        cursor.close()


def reclaim_grant(
    connection: Connection,
    *,
    grant_id: str,
    expected_version: Decimal,
    consumed_units: Decimal,
    returned_units: Decimal,
    state: str = GRANT_RETURNED,
) -> bool:
    """只在终态转换时成功一次;`unknown` 保留也走这里,但状态不是 `returned`。"""

    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["grant.reclaim"],
            (consumed_units, returned_units, state, grant_id, expected_version, GRANT_ACTIVE),
        )
        return cursor.rowcount == 1
    finally:
        cursor.close()


def reserve(connection: Connection, request: ReservationRequest, *, epoch: Decimal) -> Reservation:
    """固定锁序:quota → budget → grant。任一步 CAS 失败都抛错,由调用方回滚整笔。"""

    require_decimal(request.units, field="units")
    if request.units <= 0:
        raise ContractError("units must be positive")
    request_units = request.units

    quota = lock_quota(connection, workspace_id=request.workspace_id, meter_kind=request.meter_kind)
    if quota is None:
        raise ContractError(f"workspace {request.workspace_id!r} has no {request.meter_kind} quota")
    budget = lock_budget(connection, host_id=request.host_id, meter_kind=request.meter_kind)
    if budget is None:
        raise ContractError(f"host {request.host_id!r} has no {request.meter_kind} budget")

    if not hold_quota(
        connection,
        workspace_id=request.workspace_id,
        meter_kind=request.meter_kind,
        units=request_units,
        expected_version=quota.version,
    ):
        raise ContractError("workspace quota cannot hold the reservation")
    if not hold_budget(
        connection,
        host_id=request.host_id,
        meter_kind=request.meter_kind,
        units=request_units,
        expected_version=budget.version,
    ):
        raise ContractError("host budget cannot hold the reservation")

    grant = insert_grant(
        connection,
        grant_id=request.grant_id,
        session_id=request.session_id,
        lease_id=request.lease_id,
        meter_kind=request.meter_kind,
        authority_epoch=epoch,
        units=request_units,
        expires_at=request.grant_expires_at,
    )
    return Reservation(
        grant=grant,
        quota_version=quota.version + 1,
        budget_version=budget.version + 1,
    )


def require_final_eligibility(
    snapshot: AuthoritySnapshot,
    *,
    grant: Grant,
    request: ReservationRequest,
    expected_rev: str,
    expected_fingerprint: str,
    quarantine_on_expiry: bool = False,
) -> None:
    """锁后的最终复核:代次、schema 身份与 lease 时效都必须在提交前成立。"""

    if snapshot.authority_epoch != grant.authority_epoch:
        raise AuthorityMismatchError(
            f"authority epoch moved from {grant.authority_epoch} to {snapshot.authority_epoch}"
        )
    require_authority(
        snapshot,
        schema_rev=expected_rev,
        schema_fingerprint=expected_fingerprint,
        authority_epoch=snapshot.authority_epoch,
    )
    if snapshot.at >= require_utc(request.lease_expires_at, field="lease_expires_at"):
        if quarantine_on_expiry:
            raise ContractError("lease expired while waiting for locks; roll back and quarantine")
        raise ContractError("lease expired while waiting for locks; roll back")


def guarded_reserve(
    pool: ProbePool,
    factory: ConnectionFactory,
    request: ReservationRequest,
    *,
    expected_rev: str,
    expected_fingerprint: str,
    deadline: Deadline,
) -> Reservation:
    """先取探针连接,再开写事务;锁后读权威值并复核,全部通过才提交。"""

    deadline.check("acquiring the probe connection")
    probe = pool.acquire(deadline=deadline.expires_at())
    connection: Connection | None = None
    try:
        before = probe.read()
        deadline.check("opening the write transaction")
        connection = factory()
        with transaction(connection):
            reservation = reserve(connection, request, epoch=before.authority_epoch)
            deadline.check("reading the authority snapshot")
            snapshot = probe.read()
            require_final_eligibility(
                snapshot,
                grant=reservation.grant,
                request=request,
                expected_rev=expected_rev,
                expected_fingerprint=expected_fingerprint,
            )
        return reservation
    finally:
        if connection is not None:
            connection.close()
        pool.release(probe)
