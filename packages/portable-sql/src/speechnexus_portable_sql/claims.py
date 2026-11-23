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
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from speechnexus_portable_sql.contract import Connection, ContractError, ListMarker, Param, Row
from speechnexus_portable_sql.types import (
    new_identity,
    optional_text,
    require_digest,
    require_text,
    require_utc,
)

__all__ = [
    "BUCKETS",
    "EVENT_DIGEST_FIELDS",
    "STATEMENTS",
    "BatchClaim",
    "BucketFence",
    "ClaimCandidate",
    "ClaimOutcome",
    "ClaimedJobView",
    "EventAppend",
    "acquire_bucket",
    "append_event",
    "bucket_for",
    "candidates",
    "claim_batch",
    "claim_job",
    "claim_next",
    "claimed_jobs",
    "event_digest",
    "insert_outbox",
    "mark_outbox_delivered",
    "read_events",
    "read_undelivered",
    "release_bucket",
    "require_fence",
    "start_bucket_for",
]

#: 固定 bucket 数量。扩大它只改变竞争分布,不改变正确性。
BUCKETS = 256

#: 派发候选的可见列。`job.candidates` 是最初的冻结文本,带 alias 的族共用下面三段,
#: 免得四条语句各写一遍 keyset 谓词(`due_at > ? OR (due_at = ? AND job_id > ?)`)。
_CANDIDATE_COLUMNS = "SELECT c.job_id, c.claim_version, c.due_at FROM coordination_jobs AS c "
#: lane 是 `speech_jobs.kind`:canonical 的 `coordination_jobs` 没有 kind 列,生产 `_claim()`
#: 也是 join `speech_jobs` 才拿到它,所以这里加的是同一个 join,不是新语义。
_CANDIDATE_LANE_SOURCE = "JOIN speech_jobs AS j ON j.job_id = c.job_id "
_CANDIDATE_WHERE = (
    "WHERE c.bucket_no = ? AND c.job_state = ? AND c.due_at <= ? "
    "AND (c.due_at > ? OR (c.due_at = ? AND c.job_id > ?)) "
)
_CANDIDATE_ORDER = "ORDER BY c.due_at, c.job_id"
#: 一个 `?` 代表整份 lane 列表:元数在运行期才定,展开由驱动层的 `ListMarker` 完成。
_CANDIDATE_LANE = "AND j.kind IN (?) "
#: 准入锚点:与生产 `_claim()` 的 `admission` 片段逐条一致(文档非空、摘要一致、
#: 指到 current_attempt_id、lease 在 reserved/executing 且 cleanup 未完成)。
#: 生产用三个 inner join;这里必须用 `EXISTS`,因为一条 lease 有两个阶段行时 join 会把
#: 同一候选放大成多行,而候选列表是按行数分页的。
_CANDIDATE_ADMISSION = (
    "AND EXISTS (SELECT 1 FROM admission_operations AS a "
    "WHERE a.operation_id = c.job_id "
    "AND a.request_document IS NOT NULL AND CHAR_LENGTH(a.request_document) > 0 "
    "AND a.request_digest = c.request_digest "
    "AND EXISTS (SELECT 1 FROM admission_attempts AS t "
    "WHERE t.operation_id = a.operation_id AND t.attempt_id = a.current_attempt_id "
    "AND EXISTS (SELECT 1 FROM admission_leases AS l "
    "WHERE l.operation_id = t.operation_id AND l.attempt_id = t.attempt_id "
    "AND l.lease_state IN ('reserved', 'executing') "
    "AND l.cleanup_state = 'pending')))"
)

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
    "job.candidates.lane": (
        _CANDIDATE_COLUMNS
        + _CANDIDATE_LANE_SOURCE
        + _CANDIDATE_WHERE
        + _CANDIDATE_LANE
        + _CANDIDATE_ORDER
    ),
    "job.candidates.admitted": (
        _CANDIDATE_COLUMNS + _CANDIDATE_WHERE + _CANDIDATE_ADMISSION + " " + _CANDIDATE_ORDER
    ),
    "job.candidates.lane_admitted": (
        _CANDIDATE_COLUMNS
        + _CANDIDATE_LANE_SOURCE
        + _CANDIDATE_WHERE
        + _CANDIDATE_LANE
        + _CANDIDATE_ADMISSION
        + " "
        + _CANDIDATE_ORDER
    ),
    "job.claimed_view": (
        "SELECT j.job_id, j.workspace_id, j.kind, j.state, j.status_version, j.created_at_us, "
        "COALESCE(a.request_digest, j.request_digest), a.request_document "
        "FROM speech_jobs AS j "
        "LEFT JOIN admission_operations AS a ON a.operation_id = j.job_id "
        "WHERE j.job_id IN (?) ORDER BY j.job_id"
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
class BatchClaim:
    """一次批量轮询里真正赢得 Job CAS 的一条结果.

    只有这三个字段是认领当场的事实:`claim_version` 是 CAS 推进后的新版本,`expires_at` 是
    本次认领导致的 `claim_expires_at`。派发需要的其余列(workspace/kind/state/摘要/命令)
    属于 Job 的当前投影,用 `claimed_jobs()` 按 `job_id` 集合读,不在认领事务里重复返回。
    """

    job_id: str
    claim_version: Decimal
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedJobView:
    """`job.claimed_view` 的一行:派发结果的 Job 侧字段.

    `request_digest` 已经是权威摘要(有准入行时取准入摘要,否则取 Job 自己的);
    `request_document` 保持可空,「没有准入行」与「准入行文档为空」在写入侧就被 CHECK 分开了,
    读取侧不再用空串冒充缺失。
    """

    job_id: str
    workspace_id: str
    kind: str
    state: str
    status_version: Decimal
    created_at_us: Decimal
    request_digest: str
    request_document: str | None


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


#: 草案 `coordination_jobs.claim_expires_at_us` 用 0 表示「未领取」;canonical 用 NULL。
#: 这条等价关系只在这里定义一次,搬迁脚本与运维查询都不得自己写 `= 0`。
UNDECIDED_EPOCH_US = 0


def _utc_from_epoch_us(value: object, *, field: str) -> datetime | None:
    """epoch 微秒 -> canonical 时间;0 与负数是「从未发生」,不是 1970 年.

    返回值是**naive UTC**:canonical 的列是 `TIMESTAMP(6)`(不带时区),写进去的是 UTC 墙上
    时间。如果直接把 aware 的 datetime 交给驱动,存下来的其实是「会话时区下的墙上时间」——
    同一个时刻在不同 `TimeZone` 的会话里会存成不同的值,而且读回来没有任何报错。真库演练
    (`test_canonical_job_conversion_pg.py`)在 +08:00 会话里把它测出来了:12:00Z 存成了 20:00。
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field} must be an integer epoch in microseconds")
    if value <= UNDECIDED_EPOCH_US:
        return None
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC).replace(tzinfo=None)


def canonical_job_from_draft(
    *,
    job_id: str,
    operation_id: str,
    attempt_id: str,
    request_digest: str,
    job_state: str,
    claim_owner_id: str | None,
    claim_expires_at_us: int,
    claim_version: int,
    due_at_us: int,
    last_event_seq: int,
) -> dict[str, object]:
    """把一条草案 job 行换算成 canonical 行的唯一实现.

    为什么要有这个函数:换算规则(0 表示未领取、bucket 由 `job_id` 派生、时间折成
    `TIMESTAMP(6)`)写进 SQL、写进脚本、再写进文档,就是三份实现;搬迁演练与生产切换
    必须走同一段代码。它只做换算,不做 I/O——所以能在真库上按行校验,也能在单测里逐字段
    钉住。
    """

    require_text(job_id, field="job_id", max_bytes=128)
    require_text(job_state, field="job_state", max_bytes=24)
    require_digest(request_digest, field="request_digest")
    if claim_version < 0 or last_event_seq < 0:
        raise ContractError("claim_version and last_event_seq must not be negative")
    expires_at = _utc_from_epoch_us(claim_expires_at_us, field="claim_expires_at_us")
    due_at = _utc_from_epoch_us(due_at_us, field="due_at_us")
    if due_at is None:
        raise ContractError("due_at_us must be a positive epoch, a Job cannot be due at the epoch")
    return {
        "job_id": job_id,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "request_digest": request_digest,
        "bucket_no": bucket_for(job_id),
        "due_at": due_at,
        "job_state": job_state,
        "claim_owner_id": claim_owner_id,
        "claim_expires_at": expires_at,
        "claim_version": Decimal(claim_version),
        "last_event_seq": Decimal(last_event_seq),
    }


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


#: 候选语句的四个变体由「有没有 lane 过滤」「有没有准入锚点」两个布尔决定。
#: 映射表是唯一的选择点:调用方给两个开关,不自己拼语句,也不自己拼 SQL。
_CANDIDATE_STATEMENT = {
    (False, False): "job.candidates",
    (True, False): "job.candidates.lane",
    (False, True): "job.candidates.admitted",
    (True, True): "job.candidates.lane_admitted",
}


def _lane(kinds: Sequence[str] | None) -> ListMarker | None:
    """lane 过滤的入参规范化:每个 kind 都是一段短文本;`None` 表示不过滤.

    返回 `ListMarker` 而不是元组,是为了让「空 lane 直接拒绝」只有 `ListMarker` 这一个
    定义点,并且**在发出任何 I/O 之前**就失败:`()` 不能被读成「不过滤」——那会让一个
    只想要某几条队列的调用方静默认领全部队列,而且失败与否取决于当时有没有空闲桶。
    """

    if kinds is None:
        return None
    return ListMarker(tuple(require_text(kind, field="kind", max_bytes=32) for kind in kinds))


def _candidate_page(
    connection: Connection,
    *,
    bucket_no: Decimal,
    now: datetime,
    state: str,
    page_size: int,
    after: tuple[datetime | None, str] | None,
    lane: ListMarker | None,
    require_admission: bool,
) -> tuple[list[ClaimCandidate], bool]:
    """`candidates` 的实际读取:语句选择与参数摊平都在这里,列表标记已规范化。"""

    if page_size < 1:
        raise ContractError("page_size must be at least 1")
    # 第一页用纪元作为游标下界:NOT NULL 列上的 keyset 谓词不能拿 NULL 当锚点。
    cursor_due, cursor_job = after if after is not None else (_EPOCH, "")
    params: list[Param | ListMarker] = [
        bucket_no,
        state,
        require_utc(now, field="now"),
        cursor_due,
        cursor_due,
        cursor_job,
    ]
    if lane is not None:
        params.append(lane)
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS[_CANDIDATE_STATEMENT[(lane is not None, require_admission)]],
            tuple(params),
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


def candidates(
    connection: Connection,
    *,
    bucket_no: Decimal,
    now: datetime,
    state: str = PENDING_STATE,
    page_size: int = 32,
    after: tuple[datetime | None, str] | None = None,
    kinds: Sequence[str] | None = None,
    require_admission: bool = False,
) -> tuple[list[ClaimCandidate], bool]:
    """按 `(due_at, job_id)` 取一页候选,多取一条判断 `has_more`.

    不使用 `LIMIT`:整页由 keyset 谓词限定,`page_size + 1` 由应用侧裁剪。`kinds` 与
    `require_admission` 只切换 canonical statement,四个组合都由
    `_CANDIDATE_STATEMENT` 映射到已登记的语句。
    """

    return _candidate_page(
        connection,
        bucket_no=bucket_no,
        now=now,
        state=state,
        page_size=page_size,
        after=after,
        lane=_lane(kinds),
        require_admission=require_admission,
    )


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


def _bucket_number(value: object) -> Decimal:
    """起点桶:接受 `int` 或整数值 `Decimal`,拒绝超出 `[0, BUCKETS)` 的值.

    不取模纠正:`start_bucket` 越界说明调用方算错了轮询相位,静默折回会把这个错误藏起来,
    而它的症状(总是从同一个桶开始)只有在负载上来以后才看得出来。
    """

    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ContractError("start_bucket must be an integer bucket number")
    bucket_no = Decimal(value)
    if bucket_no != bucket_no.to_integral_value() or not Decimal(0) <= bucket_no < BUCKETS:
        raise ContractError(f"start_bucket must be an integer in [0, {BUCKETS})")
    return bucket_no


def _batch_bound(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{name} must be a positive integer")
    return value


def claim_batch(
    connection: Connection,
    *,
    owner_id: str,
    now: datetime,
    lease_until: datetime,
    limit: int,
    start_bucket: int | Decimal,
    kinds: Sequence[str] | None = None,
    require_admission: bool = False,
    page_size: int = 32,
) -> tuple[BatchClaim, ...]:
    """从 `start_bucket` 起按整圈轮询认领至多 `limit` 个 Job,返回赢下 CAS 的那些.

    轮询而不是固定分片:固定分片下某个 worker 挂掉后它那片桶再没人看,而项目边界里没有
    「调度器再平衡」这一层;整圈扫的代价是最多 `BUCKETS` 次带版本条件的 `UPDATE`,与
    「每轮全表 `SKIP LOCKED`」同阶(`docs/plans/2026-11-21-portable-sql-delivery.md` §8.76)。

    每个桶的顺序固定为:读 fence → 只在「桶空闲或租约已过期」时按读到的版本 CAS 拿桶 →
    **同一事务内**复核 fence → 按候选逐条 `claim_job` → 立刻归还桶。`limit` 是本轮认领数,
    不是桶数。任一桶被别的 owner 持有时只跳过该桶,不等待它过期、不重试。

    两条实现上的取舍,读者不必再猜:

    * **bucket 租约绝不带出调用。** 复核对不上、候选分页、Job CAS 失败都不改变归还路径:
      `release_bucket` 在 `finally` 里执行,而「归还丢了自己的租约」在成功路径上直接报错。
    * **复核不是并发判据,而是不变量断言。** `bucket.fence` 带 `FOR UPDATE`,事务型驱动下
      拿到行锁后桶行不可能在本事务内再变;复核真正拦截的是「行锁只覆盖单条语句」的驱动
      或自动提交连接(ODBC 缺省),以及本函数未来的改动把 acquire 与 claim 拆到两个事务。
      跨事务的过期 fence 由 `acquire_bucket` 的 `version = ?` CAS 拒绝,那条判据在
      `claim_next` 上才是可达的。

    事务边界由调用方拥有:本函数不 `commit` 也不 `rollback`,整笔轮询应当一次提交。
    """

    owner = require_text(owner_id, field="owner_id", max_bytes=128)
    moment = require_utc(now, field="now")
    until = require_utc(lease_until, field="lease_until")
    if until <= moment:
        raise ContractError(
            "lease_until must be after now; an already expired lease claims nothing"
        )
    wanted = _batch_bound("limit", limit)
    page = _batch_bound("page_size", page_size)
    start = _bucket_number(start_bucket)
    lane = _lane(kinds)

    claims: list[BatchClaim] = []
    for offset in range(BUCKETS):
        if len(claims) >= wanted:
            break
        bucket_no = (start + Decimal(offset)) % BUCKETS
        fence = read_fence(connection, bucket_no=bucket_no)
        if fence is None:
            continue
        if (
            fence.owner_id is not None
            and fence.lease_until is not None
            and fence.lease_until > moment
        ):
            # 别的 owner 的租约还没到期:这个桶本轮不属于我们,跳过而不是等它过期。
            continue
        if not acquire_bucket(
            connection,
            bucket_no=bucket_no,
            owner_id=owner,
            now=moment,
            lease_until=until,
            expected_version=fence.version,
        ):
            # 读到的版本已经不是当前版本:另一笔事务刚拿走了桶,本轮跳过。
            continue
        # acquire 成功后桶的版本就是「读到的版本 + 1」,复核用的正是这个值。
        held_version = fence.version + 1
        released = False
        try:
            require_fence(
                read_fence(connection, bucket_no=bucket_no),
                owner_id=owner,
                now=moment,
                expected_version=held_version,
            )
            claims.extend(
                _claim_from_bucket(
                    connection,
                    bucket_no=bucket_no,
                    owner_id=owner,
                    now=moment,
                    expires_at=until,
                    wanted=wanted - len(claims),
                    lane=lane,
                    require_admission=require_admission,
                    page_size=page,
                )
            )
        finally:
            released = release_bucket(
                connection, bucket_no=bucket_no, owner_id=owner, expected_version=held_version
            )
        if not released:
            raise ContractError(
                f"bucket {bucket_no} lease moved under the batch; its claims are not trustworthy"
            )
    return tuple(claims)


def _claim_from_bucket(
    connection: Connection,
    *,
    bucket_no: Decimal,
    owner_id: str,
    now: datetime,
    expires_at: datetime,
    wanted: int,
    lane: ListMarker | None,
    require_admission: bool,
    page_size: int,
) -> list[BatchClaim]:
    """在已持有的桶上按 keyset 翻页认领,直到 `wanted` 或候选耗尽.

    候选只是「可能可认领」:每一条都要过 `claim_job` 的版本 + 状态 CAS,失败就继续下一条,
    因为竞争失败的那一条已经被别人认领,重试它没有意义。
    """

    claims: list[BatchClaim] = []
    after: tuple[datetime | None, str] | None = None
    while len(claims) < wanted:
        page, has_more = _candidate_page(
            connection,
            bucket_no=bucket_no,
            now=now,
            state=PENDING_STATE,
            page_size=page_size,
            after=after,
            lane=lane,
            require_admission=require_admission,
        )
        if not page:
            break
        for candidate in page:
            if claim_job(
                connection,
                job_id=candidate.job_id,
                owner_id=owner_id,
                expires_at=expires_at,
                expected_version=candidate.claim_version,
            ):
                claims.append(
                    BatchClaim(
                        job_id=candidate.job_id,
                        claim_version=candidate.claim_version + 1,
                        expires_at=expires_at,
                    )
                )
                if len(claims) >= wanted:
                    break
        if not has_more:
            break
        last = page[-1]
        after = (last.due_at, last.job_id)
    return claims


def claimed_jobs(connection: Connection, *, job_ids: Sequence[str]) -> tuple[ClaimedJobView, ...]:
    """按 `job_id` 集合读派发结果需要的 Job 列,顺序按 `job_id` 稳定.

    认领事务只回 `job_id`/`claim_version`/`expires_at`;其余列是 Job 的当前投影,在提交后按
    集合读一次。空集合不发语句:没有认领结果时读视图没有意义,`ListMarker` 也不接受空列表。
    """

    identities = tuple(require_text(job_id, field="job_id", max_bytes=128) for job_id in job_ids)
    if not identities:
        return ()
    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["job.claimed_view"], (ListMarker(identities),))
        rows: Sequence[Row] = cursor.fetchall()
    finally:
        cursor.close()
    return tuple(
        ClaimedJobView(
            job_id=str(row.at(0)),
            workspace_id=str(row.at(1)),
            kind=str(row.at(2)),
            state=str(row.at(3)),
            status_version=Decimal(str(row.at(4))),
            created_at_us=Decimal(str(row.at(5))),
            request_digest=str(row.at(6)),
            request_document=_text_or_none(row.at(7)),
        )
        for row in rows
    )


def start_bucket_for(owner_id: str, now: datetime) -> Decimal:
    """本轮轮询的起点桶:同一个 worker 在同一个 UTC 分钟内从同一个桶开始.

    文档写的是 `bucket_for(owner_id + 分钟)`(§8.76 方案 B),这句话有两处没说死,这里按
    「最简确定性规则」定下来:

    1. **「分钟」是 UTC epoch 分钟数**,即 `int(now.timestamp() // 60)`。不用本地时间、也不
       用 `HH:MM` 字符串:不同时区/夏令时下的进程会在同一时刻算出不同的起点,而轮询的共同
       相位正是这个起点存在的理由。
    2. **拼接插一个 `:` 分隔符**。`owner_id` 是任意文本,直接和数字相连会让
       `"worker-1" + "23"` 与 `"worker-12" + "3"` 落到同一个字符串上,两个不同 worker 从此
       总从同一个桶起步;分隔符让这一对映射唯一。

    返回值与 `bucket_for` 同域:`[0, BUCKETS)` 内的 `Decimal`。
    """

    owner = require_text(owner_id, field="owner_id", max_bytes=128)
    moment = require_utc(now, field="now")
    minute = int(moment.timestamp() // 60)
    return bucket_for(f"{owner}:{minute}")


#: 公共事件摘要覆盖的字段,顺序即序列化顺序。加字段 = 改公共契约,必须同一处改。
EVENT_DIGEST_FIELDS = (
    "job_id",
    "event_seq",
    "state",
    "occurred_at",
    "kind",
    "progress_completed",
    "progress_total",
    "payload_ref",
)


def _canonical_time(value: datetime) -> str:
    """UTC 微秒定长:同一时刻的不同时区写法必须折叠成同一个字符串。"""

    moment = require_utc(value, field="occurred_at")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event_digest(
    *,
    job_id: str,
    event_seq: Decimal,
    state: str,
    occurred_at: datetime,
    kind: str | None = None,
    progress_completed: int | None = None,
    progress_total: int | None = None,
    payload_ref: str | None = None,
) -> str:
    """公共事件摘要的唯一算法:`sha256:` 加规范序列化后的十六进制.

    为什么要有这一个函数:草案表 `speech_job_events` 只存 `state`/`occurred_at_us`/
    `progress_*`,没有任何摘要列;搬迁(#1102)必须**按同一规则重算**摘要,而 SSE 去重、
    outbox 幂等与事件重放都依赖这个规则。规则散落在两个文件里就是两份实现,合并时必然
    出现「同一条事件两种摘要」。所以只在这里定义一次:

    - 字段集合固定为 `EVENT_DIGEST_FIELDS`,顺序固定,缺省值写 `null`,不漏字段;
    - `occurred_at` 一律折成 UTC 微秒定长(`2026-11-23T00:00:00.000000Z`);
    - JSON 用 `sort_keys` + 紧凑分隔符 + `ensure_ascii=False`,再对 UTF-8 字节取摘要。
    """

    require_text(job_id, field="job_id", max_bytes=128)
    require_text(state, field="state", max_bytes=24)
    if kind is not None:
        require_text(kind, field="kind", max_bytes=16)
    if payload_ref is not None:
        require_text(payload_ref, field="payload_ref", max_bytes=512)
    for name, value in (
        ("progress_completed", progress_completed),
        ("progress_total", progress_total),
    ):
        if value is not None and value < 0:
            raise ContractError(f"{name} must not be negative")
    if (
        progress_completed is not None
        and progress_total is not None
        and progress_total < progress_completed
    ):
        raise ContractError("progress_total must not be below progress_completed")

    document = {
        "job_id": job_id,
        "event_seq": int(event_seq),
        "state": state,
        "occurred_at": _canonical_time(occurred_at),
        "kind": kind,
        "progress_completed": progress_completed,
        "progress_total": progress_total,
        "payload_ref": payload_ref,
    }
    if tuple(document) != EVENT_DIGEST_FIELDS:
        raise ContractError("event digest fields drifted from EVENT_DIGEST_FIELDS")
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


@dataclass(frozen=True)
class HistoricalEvent:
    """草案 `speech_job_events` 的一行,按 canonical 规则重建摘要。"""

    state: str
    occurred_at_us: int
    kind: str | None = None
    progress_completed: int | None = None
    progress_total: int | None = None
    payload_ref: str | None = None


def adopt_historical_events(
    connection: Connection, *, job_id: str, events: Sequence[HistoricalEvent]
) -> Decimal:
    """把历史事件搬进 canonical `job_events` 并把 head 推到末尾,返回新的 `last_event_seq`.

    为什么必须走这条路径:canonical 的事件去重依赖摘要,而草案表没有摘要列,所以历史事件
    的 `event_seq` 与摘要都要在这里重建一次。序号必须从 1 连续,head 必须等于搬完后的最大值
    ——否则下一次 `append_event` 会撞主键,而撞主键在重试语义里会被当成「已经写过」。

    这里不新增 SQL:复用 `job.event_head` / `job_event.insert` / `job.event_head_update`,
    所以搬迁与在线写入走的是同一组语句。
    """

    require_text(job_id, field="job_id", max_bytes=128)
    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["job.event_head"], (job_id,))
        head_rows = cursor.fetchall()
        if len(head_rows) != 1:
            raise ContractError(f"coordination_jobs has {len(head_rows)} rows for {job_id!r}")
        head = head_rows[0]
        seq = Decimal(str(head.at(0)))
        version = Decimal(str(head.at(1)))
        if seq != 0:
            raise ContractError(f"{job_id!r} already has events; adoption is a one-shot backfill")
        for index, event in enumerate(events, start=1):
            occurred_at = _utc_from_epoch_us(event.occurred_at_us, field="occurred_at_us")
            if occurred_at is None:
                raise ContractError("a historical event needs a real timestamp")
            digest = event_digest(
                job_id=job_id,
                event_seq=Decimal(index),
                state=event.state,
                occurred_at=occurred_at.replace(tzinfo=UTC),
                kind=event.kind,
                progress_completed=event.progress_completed,
                progress_total=event.progress_total,
                payload_ref=event.payload_ref,
            )
            cursor.execute(
                STATEMENTS["job_event.insert"], (job_id, index, digest, event.payload_ref)
            )
        last = Decimal(len(events))
        cursor.execute(STATEMENTS["job.event_head_update"], (last, version + 1, job_id, version))
        if cursor.rowcount != 1:
            raise ContractError("event head moved under the backfill; rerun it in one transaction")
    finally:
        cursor.close()
    return last


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
