"""claim bucket、Job CAS 与每 Job 事件序号:替代 `SKIP LOCKED` 的唯一实现.

为什么是 bucket:集群可能同时有多个 dispatcher,而 `SKIP LOCKED` 是 PostgreSQL 方言,也不是
「两个 worker 只有一个能认领同一 Job」的判据。这里把竞争范围压到固定数量的 bucket 上,
再用两次 `rowcount` CAS 定输赢:

1. `bucket.acquire` 抢 bucket 租约;`rowcount = 0` 表示 bucket 被别人持有或版本已变,退避重试。
2. 持有者按 `(claim_expires_at, job_id)` 取候选;候选只是「可能可认领」,不是认领证据。
3. 认领同一个 Job 的事务按 **bucket → Job → outbox** 固定顺序复核:先锁 bucket 行确认自己仍是
   持有者,再对 Job 做带版本条件与状态条件的 `UPDATE`,最后写事件与 outbox。

时间参数一律由权威探针给出(`now`),本模块不读应用时钟,也不读数据库时间。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from speechnexus_portable_sql.contract import Connection, ContractError, Row
from speechnexus_portable_sql.types import (
    new_identity,
    optional_text,
    require_digest,
    require_text,
    require_utc,
)

__all__ = [
    "BUCKETS",
    "STATEMENTS",
    "BucketFence",
    "ClaimCandidate",
    "ClaimOutcome",
    "EventAppend",
    "acquire_bucket",
    "append_event",
    "bucket_for",
    "candidates",
    "claim_job",
    "claim_next",
    "insert_outbox",
    "mark_outbox_delivered",
    "read_events",
    "read_undelivered",
    "release_bucket",
    "require_fence",
]

#: 固定 bucket 数量。扩大它只改变竞争分布,不改变正确性。
BUCKETS = 256

#: statement ID → SQL。manifest 必须登记同一份文本,guard 会逐条比对。
STATEMENTS = {
    "bucket.acquire": (
        "UPDATE coordination_claim_buckets SET claim_owner_id = ?, lease_until = ?, "
        "version = ? WHERE bucket_no = ? "
        "AND version = ? "
        "AND (claim_owner_id IS NULL OR lease_until <= ?)"
    ),
    "bucket.fence": (
        "SELECT version, claim_owner_id, lease_until FROM coordination_claim_buckets "
        "WHERE bucket_no = ? FOR UPDATE"
    ),
    "bucket.release": (
        "UPDATE coordination_claim_buckets SET claim_owner_id = ?, lease_until = ?, version = ? "
        "WHERE bucket_no = ? AND version = ? AND claim_owner_id = ?"
    ),
    "job.candidates": (
        "SELECT job_id, claim_version, due_at FROM coordination_jobs "
        "WHERE bucket_no = ? AND job_state = ? AND due_at <= ? "
        "AND (due_at > ? OR (due_at = ? AND job_id > ?)) "
        "ORDER BY due_at, job_id"
    ),
    "job.claim": (
        "UPDATE coordination_jobs SET job_state = ?, claim_owner_id = ?, claim_expires_at = ?, "
        "claim_version = ? WHERE job_id = ? AND claim_version = ? AND job_state = ?"
    ),
    "job.event_head": (
        "SELECT last_event_seq, claim_version FROM coordination_jobs WHERE job_id = ? FOR UPDATE"
    ),
    "job.event_head_update": (
        "UPDATE coordination_jobs SET last_event_seq = ?, claim_version = ? "
        "WHERE job_id = ? AND claim_version = ?"
    ),
    "job_event.insert": (
        "INSERT INTO job_events (job_id, event_seq, event_digest, payload_ref) VALUES (?, ?, ?, ?)"
    ),
    "job_event.read": (
        "SELECT event_seq, event_digest, payload_ref FROM job_events "
        "WHERE job_id = ? AND event_seq > ? ORDER BY event_seq"
    ),
    "outbox.insert": (
        "INSERT INTO coordination_outbox (event_id, job_id, event_digest, delivered_at) "
        "VALUES (?, ?, ?, ?)"
    ),
    "outbox.undelivered": (
        "SELECT event_id, job_id FROM coordination_outbox "
        "WHERE delivered_at IS NULL AND event_id > ? ORDER BY event_id"
    ),
    "outbox.mark_delivered": (
        "UPDATE coordination_outbox SET delivered_at = ? "
        "WHERE event_id = ? AND delivered_at IS NULL"
    ),
}

PENDING_STATE = "pending"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DEFAULT_CLAIM_STATE = "claimed"


@dataclass(frozen=True, slots=True)
class BucketFence:
    bucket_no: Decimal
    owner_id: str | None
    lease_until: datetime | None
    version: Decimal


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    job_id: str
    claim_version: Decimal
    due_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    job_id: str
    owner_id: str
    next_version: Decimal


@dataclass(frozen=True, slots=True)
class EventAppend:
    job_id: str
    event_seq: Decimal
    event_id: str
    digest: str


def _datetime_or_none(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ContractError(f"{field} is {type(value).__name__}, not datetime")
    # 驱动返回的 TIMESTAMP 没有时区;契约里的 datetime 一律是 UTC aware。
    return require_utc(value, field=field)


def _require_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ContractError(f"{field} is {type(value).__name__}, not datetime")
    return require_utc(value, field=field)


def _text_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def bucket_for(job_id: str, buckets: int = BUCKETS) -> Decimal:
    """第一个定义点:同一 `job_id` 在任何进程里都落在同一 bucket。"""

    require_text(job_id, field="job_id", max_bytes=128)
    if buckets < 1:
        raise ContractError("bucket count must be at least 1")
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return Decimal(int.from_bytes(digest[:8], "big") % buckets)


def acquire_bucket(
    connection: Connection,
    *,
    bucket_no: Decimal,
    owner_id: str,
    now: datetime,
    lease_until: datetime,
    expected_version: Decimal,
) -> bool:
    """抢 bucket 租约;`rowcount == 1` 才算拿到。"""

    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["bucket.acquire"],
            (
                owner_id,
                require_utc(lease_until, field="lease_until"),
                expected_version + 1,
                bucket_no,
                expected_version,
                require_utc(now, field="now"),
            ),
        )
        return cursor.rowcount == 1
    finally:
        cursor.close()


def release_bucket(
    connection: Connection,
    *,
    bucket_no: Decimal,
    owner_id: str,
    expected_version: Decimal,
) -> bool:
    """归还 bucket:清空 owner 并把租约推到过去,只有当前持有者且版本匹配才生效。"""

    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["bucket.release"],
            (None, None, expected_version + 1, bucket_no, expected_version, owner_id),
        )
        return cursor.rowcount == 1
    finally:
        cursor.close()


def read_fence(connection: Connection, *, bucket_no: Decimal) -> BucketFence | None:
    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["bucket.fence"], (bucket_no,))
        rows = cursor.fetchall()
    finally:
        cursor.close()
    if not rows:
        return None
    row = rows[0]
    owner = row.at(1)
    lease = row.at(2)
    return BucketFence(
        bucket_no=bucket_no,
        owner_id=None if owner is None else str(owner),
        lease_until=_datetime_or_none(lease, field="lease_until"),
        version=Decimal(str(row.at(0))),
    )


def require_fence(
    fence: BucketFence | None,
    *,
    owner_id: str,
    now: datetime,
    expected_version: Decimal | None = None,
) -> None:
    """认领前复核:仍是自己持有、租约未过期、版本没被别人推进。"""

    if fence is None:
        raise ContractError("bucket row is missing; bootstrap did not seed the buckets")
    if fence.owner_id != owner_id:
        raise ContractError(f"bucket {fence.bucket_no} is owned by {fence.owner_id!r}")
    if fence.lease_until is None or fence.lease_until <= require_utc(now, field="now"):
        raise ContractError(f"bucket {fence.bucket_no} lease already expired")
    if expected_version is not None and fence.version != expected_version:
        raise ContractError("bucket version moved; stale candidate list must be discarded")


def candidates(
    connection: Connection,
    *,
    bucket_no: Decimal,
    now: datetime,
    state: str = PENDING_STATE,
    page_size: int = 32,
    after: tuple[datetime | None, str] | None = None,
) -> tuple[list[ClaimCandidate], bool]:
    """按 `(claim_expires_at, job_id)` 取一页候选,多取一条判断 `has_more`.

    不使用 `LIMIT`:整页由 keyset 谓词限定,`page_size + 1` 由应用侧裁剪。
    """

    if page_size < 1:
        raise ContractError("page_size must be at least 1")
    # 第一页用纪元作为游标下界:NOT NULL 列上的 keyset 谓词不能拿 NULL 当锚点。
    cursor_due, cursor_job = after if after is not None else (_EPOCH, "")
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["job.candidates"],
            (
                bucket_no,
                state,
                require_utc(now, field="now"),
                cursor_due,
                cursor_due,
                cursor_job,
            ),
        )
        rows: Sequence[Row] = cursor.fetchall()
    finally:
        cursor.close()
    page = [
        ClaimCandidate(
            job_id=str(row.at(0)),
            claim_version=Decimal(str(row.at(1))),
            due_at=_require_datetime(row.at(2), field="due_at"),
        )
        for row in rows[:page_size]
    ]
    return page, len(rows) > page_size


def claim_job(
    connection: Connection,
    *,
    job_id: str,
    owner_id: str,
    expires_at: datetime,
    expected_version: Decimal,
    expected_state: str = PENDING_STATE,
    next_state: str = DEFAULT_CLAIM_STATE,
) -> bool:
    """带版本与状态条件的 Job CAS;`rowcount == 1` 才是认领成功。"""

    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["job.claim"],
            (
                next_state,
                owner_id,
                require_utc(expires_at, field="expires_at"),
                expected_version + 1,
                job_id,
                expected_version,
                expected_state,
            ),
        )
        return cursor.rowcount == 1
    finally:
        cursor.close()


def claim_next(
    connection: Connection,
    *,
    bucket_no: Decimal,
    owner_id: str,
    now: datetime,
    expires_at: datetime,
    bucket_version: Decimal,
    state: str = PENDING_STATE,
    page_size: int = 32,
) -> ClaimOutcome | None:
    """固定顺序认领:复核 bucket fence → 取候选 → Job CAS.

    适用一个事务内的单次尝试。任何一个候选失败(被别的 worker 抢走)就继续下一个,
    候选列表本身只在 fence 复核之后才是有效的。
    """

    fence = read_fence(connection, bucket_no=bucket_no)
    require_fence(fence, owner_id=owner_id, now=now, expected_version=bucket_version)
    page, _ = candidates(connection, bucket_no=bucket_no, now=now, state=state, page_size=page_size)
    for candidate in page:
        if claim_job(
            connection,
            job_id=candidate.job_id,
            owner_id=owner_id,
            expires_at=expires_at,
            expected_version=candidate.claim_version,
            expected_state=state,
        ):
            return ClaimOutcome(
                job_id=candidate.job_id,
                owner_id=owner_id,
                next_version=candidate.claim_version + 1,
            )
    return None


def append_event(
    connection: Connection,
    *,
    job_id: str,
    event_digest: str,
    payload_ref: str | None = None,
    deliverable: bool = True,
) -> EventAppend:
    """锁定 Job head 后取 `last_event_seq + 1`,与 outbox 同事务写入.

    `event_seq` 由权威 head 生成,所以「先取时间后提交」的两个事务也不会换序;
    `created_at` 和 outbox ULID 都不参与公共事件顺序。
    """

    require_text(job_id, field="job_id", max_bytes=128)
    digest = require_digest(event_digest)
    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["job.event_head"], (job_id,))
        head_rows = cursor.fetchall()
        if len(head_rows) != 1:
            raise ContractError(f"coordination_jobs has {len(head_rows)} rows for {job_id!r}")
        head = head_rows[0]
        next_seq = Decimal(str(head.at(0))) + 1
        head_version = Decimal(str(head.at(1)))

        reference = optional_text(payload_ref, field="payload_ref", max_bytes=512)
        cursor.execute(STATEMENTS["job_event.insert"], (job_id, next_seq, digest, reference))
        cursor.execute(
            STATEMENTS["job.event_head_update"],
            (next_seq, head_version + 1, job_id, head_version),
        )
        if cursor.rowcount != 1:
            raise ContractError("event head version moved inside the locked transaction")

        event_id = new_identity()
        if deliverable:
            insert_outbox(
                connection,
                event_id=event_id,
                job_id=job_id,
                event_digest=digest,
                delivered_at=None,
            )
    finally:
        cursor.close()
    return EventAppend(job_id=job_id, event_seq=next_seq, event_id=event_id, digest=digest)


def read_events(
    connection: Connection, *, job_id: str, after_seq: Decimal
) -> list[tuple[Decimal, str, str | None]]:
    """`Last-Event-ID` 只按 `(job_id, event_seq)` 续读。"""

    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["job_event.read"], (job_id, after_seq))
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [(Decimal(str(row.at(0))), str(row.at(1)), _text_or_none(row.at(2))) for row in rows]


def insert_outbox(
    connection: Connection,
    *,
    event_id: str,
    job_id: str,
    event_digest: str,
    delivered_at: datetime | None,
) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["outbox.insert"],
            (event_id, job_id, event_digest, delivered_at),
        )
    finally:
        cursor.close()


def read_undelivered(
    connection: Connection, *, after_event_id: str = "", page_size: int = 32
) -> list[tuple[str, str]]:
    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["outbox.undelivered"], (after_event_id,))
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [(str(row.at(0)), str(row.at(1))) for row in rows[:page_size]]


def mark_outbox_delivered(connection: Connection, *, event_id: str, delivered_at: datetime) -> bool:
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["outbox.mark_delivered"],
            (require_utc(delivered_at, field="delivered_at"), event_id),
        )
        return cursor.rowcount == 1
    finally:
        cursor.close()
