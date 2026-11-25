"""PostgreSQL claim lease and public Job transitions for the dispatcher.

The lease itself lives in the canonical coordination columns: ``claim_batch`` takes a bucket
lease and CAS-es ``coordination_jobs`` into ``job_state = 'claimed'``, so a claim is one
transaction because the write and the ownership evidence are the same row. Everything a
worker does afterwards (renew, progress, terminal close) is conditional on that row still
carrying its current ``claim_owner_id`` / ``claim_version`` / non-expired
``claim_expires_at``; the draft ``claim_expires_at_us`` epoch column is no longer written
here, because canonical columns are authoritative once written (0065 only fills them where
they are still ``NULL``).

Lock order is fixed at bucket row -> Job row -> event/outbox: ``claim_batch`` reads the
bucket fence ``FOR UPDATE``, then touches ``coordination_jobs``, and the state store below
always takes the ``coordination_jobs`` row lock before the public ``speech_jobs`` row it
projects. Cancellation acceptance takes that same Job row first and closes its
``job_state`` before it writes the fence, which is why a Job whose cancellation was
accepted can no longer win the claim CAS. No ``SKIP LOCKED`` and no vendor error branch
are involved.

``_mark_running`` (S1b), ``_record_progress`` and ``_finish`` (S1) also append the
canonical ``job_events`` row for the same fact in the same transaction (cutover stages
S1/S1b, plan §5). The canonical sequence is ``coordination_jobs.last_event_seq + 1``,
*not* the draft ``MAX(speech_job_events) + 1``: the draft table carries one more fact than
canonical ever will — the creation row at ``event_seq`` 0, whose digest lives on
``coordination_jobs.initial_event_digest`` and on the outbox row instead — so the two
sequences are not the same sequence, and ``event_digest`` covers ``event_seq``: a digest
computed over a draft number would describe a different fact and could never be
reconciled with the canonical row. Since S2 the durable event stream reads canonical: the
canonical row decides which facts exist, and the draft row for the same ``(job_id, event_seq)``
only supplies the projection columns canonical does not store. The draft is still written
because that projection has no canonical source; ``canonical_event_read`` is the read side.

**A Job with no ``coordination_jobs`` row is refused, not skipped.** ``job_events.job_id``
references that row, so a started fact belonging to no coordination identity has no legal
canonical owner. ``_append_canonical_event`` is the single place that decides this: it
fails the transition rather than leaving a draft-only started fact — the one kind of gap
that reconciliation cannot close afterwards, which is why S1 refused in the other two
transitions as well. Because the refusal happens inside the same transaction, whatever
``_mark_running`` had already written is rolled back, so a refused Job keeps the exact
state it had before the call. The refusal cannot break a Job that used to dispatch: every
claim candidate statement starts ``FROM coordination_jobs``, so a Job without that row is
never handed to this state store. The gap that does exist is historical (starts written by
S1, before S1b), and ``started_event_gaps`` names every one of them so the read switch
cannot mistake them for a failed write.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from speechnexus_backend.speech.application.leases import (
    LeaseIsolation,
    LeaseIsolationGap,
    LeaseService,
    LeaseSnapshot,
)
from speechnexus_backend.speech.domain.job_dispatch import (
    DISPATCHABLE_JOB_KIND_UNION,
    DISPATCHABLE_JOB_KINDS,
    VOICE_VALIDATION_JOB_KINDS,
    ClaimedJob,
    DispatchClaimLostError,
    DispatchConflictError,
    DispatchJob,
    DispatchProtocolError,
    DispatchUnavailableError,
    identifier,
)
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import QuarantineLease
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    NOT_SUBMITTED_TABLE,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, SpeechPool
from speechnexus_backend.speech.infrastructure.public_job_progress_schema import (
    LIFECYCLE_KIND,
    PROGRESS_KIND,
)
from speechnexus_portable_sql.claims import (
    DEFAULT_CLAIM_STATE,
    STATEMENTS,
    BatchClaim,
    ClaimedJobView,
    claim_batch,
    claimed_jobs,
    event_digest,
    utc_from_epoch_us,
)
from speechnexus_portable_sql.clock import now_us, to_epoch_us
from speechnexus_portable_sql.contract import Connection, ContractError
from speechnexus_portable_sql.psycopg_driver import ContractCursor
from speechnexus_portable_sql.transaction import insert_if_absent
from speechnexus_portable_sql.vendor import (
    PoolClosed,
    PoolTimeout,
    TooManyRequests,
    psycopg,
    tuple_row,
)

#: 本轮轮询的起点桶。**所有派发者都从同一个桶起步,顺序不随 worker 或分钟轮转。**
#:
#: 为什么不用 ``start_bucket_for(owner_id, 分钟)``(§8.76 方案 B 的字面写法):``claim_batch``
#: 把**整圈**放在一笔事务里,而 ``bucket.fence`` 是 ``SELECT ... FOR UPDATE`` —— 桶行锁一直
#: 持有到提交,于是一笔事务会依次锁住它扫过的每个桶。两个起点不同的派发者在环上必然相交:
#: A 持 5..99 等 100、B 持 100.. 等 5,PostgreSQL 直接报 ``40P01 deadlock detected``。
#: 实测(真库、两个派发者、``claim_batch`` 原样)每次并发都是死锁,不是偶发。
#:
#: 固定升序不会降低覆盖:每轮仍然扫满 256 个桶,方案 B 依赖的是「整圈都看一遍」,不是
#: 「从哪儿开始」。代价是并发派发者的轮询在桶行上排队而不是各扫一段 —— 用可预测的等待
#: 换掉必然的死锁。
CLAIM_ROUND_START_BUCKET = 0

#: The canonical state a live claim holds. Defined once in ``claims.DEFAULT_CLAIM_STATE``:
#: renew/progress/close/fence guards all compare against this value, so a second spelling
#: here would make a claim invisible to its own worker.
_CLAIMED_STATE = DEFAULT_CLAIM_STATE

_JOB_SELECT = (
    "SELECT j.job_id, j.workspace_id, j.kind, j.state, j.status_version, "
    "j.created_at_us, j.request_digest, COALESCE(a.request_document, '') AS request_document "
    "FROM speech_jobs AS j "
    "LEFT JOIN admission_operations AS a ON a.operation_id = j.job_id "
)


class _TerminalGuardLostError(RuntimeError):
    """The current transaction must roll back without writing a terminal state."""


class _ContractConnection:
    """The pool connection seen through the portable contract instead of ``dict_row``.

    ``claims.py`` reads its statements by column position (``row.at(i)``) so the same text
    works on every declared profile, while this pool hands adapters named ``dict`` rows.
    ``ContractCursor`` is the one place that folds driver rows into the contract ``Row``; it
    is used here rather than teaching the claim primitives a second row convention.
    """

    def __init__(self, connection: SpeechConnection) -> None:
        self._connection = connection

    def cursor(self) -> Any:
        # ``tuple_row`` is explicit: the connection default is ``dict_row`` and
        # ``ContractCursor`` only folds tuples into ``Row``.
        return ContractCursor(self._connection, row_factory=tuple_row)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def _contract_connection(connection: SpeechConnection) -> Connection:
    return cast(Connection, _ContractConnection(connection))


def _canonical_moment(value: int, *, field: str) -> datetime:
    """epoch 微秒 -> canonical ``TIMESTAMP(6)``(naive UTC).

    换算只在 ``claims.utc_from_epoch_us`` 定义一次:canonical 时刻列不带时区,直接给驱动一个
    aware 值的话,存下来的是「会话时区下的墙上时间」,同一时刻在两个会话里会写成不同的值。
    ``None`` 只对应 ``0``/负数(「从未发生」),而数据库时钟与租约到期都严格大于 0。
    """

    moment = utc_from_epoch_us(value, field=field)
    if moment is None:
        raise ContractError(f"{field} must be a positive epoch for a canonical timestamp")
    return moment


def _next_event_seq(connection: SpeechConnection, job_id: str) -> int:
    """下一个草案事件序号:STORED MAX + 1,不是 ``status_version``.

    调用方已经持有该 Job 的 ``speech_jobs`` 行锁,所以「读 MAX 再插入」与旧语句里
    ``INSERT ... SELECT MAX(event_seq) + 1`` 的串行化等价,而这一步不再需要 CTE。
    """

    row = connection.execute(
        "SELECT COALESCE(MAX(event_seq), -1) + 1 AS next_seq "
        "FROM speech_job_events WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise DispatchProtocolError("Stored Job event head is unreadable")
    return int(row["next_seq"])


def _append_lifecycle_event(
    connection: SpeechConnection, *, job_id: str, state: str, occurred_at_us: int
) -> None:
    connection.execute(
        "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
        "VALUES (?, ?, ?, ?)",
        (job_id, _next_event_seq(connection, job_id), state, occurred_at_us),
    )


#: 推进 canonical 事件头。**序号由头决定,不由草案的 ``MAX+1`` 决定**:草案表多一条
#: canonical 侧永远没有的创建事实(``event_seq`` 0,摘要在
#: ``coordination_jobs.initial_event_digest`` 与 outbox 行上),所以「草案第几条」与
#: 「canonical 第几条」不是同一个数;``event_digest`` 又覆盖 ``event_seq``,两条表示要用同一个
#: 序号算摘要才可能被对账,那个序号只能是 ``coordination_jobs.last_event_seq + 1``。
#:
#: 只写 ``last_event_seq``,**不碰 ``claim_version``**:``claim_version`` 是 fence,``append_event``
#: 的 ``job.event_head_update`` 顺手把它加一;在这里也加一会让同一 worker 手里
#: ``ClaimedJob.claim_version`` 立刻过期,``_finish`` 的关闭 CAS 以 ``rowcount = 0`` 失败。
#: 本语句在持有该 ``coordination_jobs`` 行的 ``FOR UPDATE`` 锁之后执行,所以不需要版本 CAS。
_EVENT_HEAD_ADVANCE = "UPDATE coordination_jobs SET last_event_seq = ? WHERE job_id = ?"


def _append_canonical_event(
    connection: SpeechConnection,
    *,
    job_id: str,
    state: str,
    occurred_at_us: int,
    kind: str | None = None,
    progress_completed: int | None = None,
    progress_total: int | None = None,
) -> None:
    """同事务追加一条 canonical ``job_events`` 事实(S1 事件双写的写侧)。

    这是**第二条表示**,不是第二个权威:草案序号仍由 ``_next_event_seq`` 的 ``MAX+1`` 决定,
    两条表示在同一笔事务里写,所以不存在「一侧写了另一侧没写」的中间态;canonical 侧的失败
    (例如缺列、越权)会把整个迁移一起回滚。S2 之后 canonical 是**读**的权威(事实集、顺序与
    摘要),草案退为投影来源 —— 它仍然要写,因为 ``state``/``kind``/``progress_*`` 只有它有。

    语句与列清单都不在这里重写:读取复用 ``claims.STATEMENTS["job.event_head"]``(同一条
    ``SELECT ... FOR UPDATE``,``append_event`` 用的也是它),写入复用
    ``claims.STATEMENTS["job_event.insert"]``;后者的列清单由
    ``tests/ci/test_canonical_event_statement_parity.py`` 钉在
    ``canonical_event_schema.JOB_EVENT_INSERT_COLUMNS`` 上,漂移即红。

    ``payload_ref`` 一律 ``NULL``:产品决定是搬迁不引用外部负载(0068 之前已合并)。
    """

    head = connection.execute(STATEMENTS["job.event_head"], (job_id,)).fetchone()
    if head is None:
        # 进度与终态迁移的入口已经 ``SELECT ... FROM coordination_jobs ... FOR UPDATE`` 并据
        # ``rowcount`` 判过归属,而 started 只按公开行的 ``rowcount`` 判 —— 三种迁移里只有它
        # 可能在缺父行的 Job 上被调用(例如历史 Job 只有 ``speech_jobs`` 行)。静默跳过会留下
        # 「草案有、canonical 永远没有」的缺口,那正是对账之后无法补的形状,所以这里直接失败。
        # 消息里点名 Job:拒绝要能直接进工单,只给一个类型化错误的话,运维还得自己反推是哪个。
        raise DispatchProtocolError(
            f"Job {job_id!r} has no coordination_jobs row; "
            "its event has no canonical owner and no job_events row may reference it"
        )
    event_seq = int(head["last_event_seq"]) + 1
    digest = event_digest(
        job_id=job_id,
        event_seq=Decimal(event_seq),
        state=state,
        occurred_at=_canonical_moment(occurred_at_us, field="occurred_at"),
        kind=kind,
        progress_completed=progress_completed,
        progress_total=progress_total,
        payload_ref=None,
    )
    connection.execute(STATEMENTS["job_event.insert"], (job_id, event_seq, digest, None))
    connection.execute(_EVENT_HEAD_ADVANCE, (event_seq, job_id))


#: 「草案有 started 事实、canonical 侧一条事件都没有」的对账口径,**只在这里定义一次**。
#:
#: S1b 之后新写入的 started 事实两侧都有,这个查询回答的是其余那些:
#:
#: * S1 期间(本修订之前)写入的 Job:``_mark_running`` 当时只写草案事件,canonical 侧那条
#:   started 永远补不上 —— ``adopt_historical_events`` 对已有事件的 Job 直接拒绝,一次性
#:   回填不是修补手段。它们**按设计**有缺口,所以必须能被点名,否则「历史 Job 缺 started」
#:   会被当成一次写入失败,或者更糟:被 SSE 续读当成常态。S2 之后这条读路径已经切到
#:   canonical,缺口 Job 的流会少掉 started 那一帧(而不是退回去读草案),所以部署读翻转之前
#:   必须先把这批 Job 处理掉 —— 这份清单就是那个前置检查。
#: * 以及任何「有草案 started、canonical 一条事件都没有」的 Job(手工数据、半迁移库)。
#:
#: ``reason`` 区分两种成因:``no-coordination-row`` 是根本没有父行(``_mark_running`` 拒绝
#: 写入的那一类),``no-canonical-event`` 有父行但 canonical 侧为空。**有** canonical 事件
#: 但缺 started 的 Job 不在这里 —— 那需要逐事件比摘要,口径在
#: ``scripts/dev-env/portable-reconcile.py`` 的逐字段对账里,这里不复制第二份。
#: 行数上界(``STARTED_EVENT_GAPS_LIMIT``):对账的用途是点名到 Job,不是出报表;按 ``job_id``
#: 排序取前缀,所以两次跑的结果前缀一致,便于对比。
STARTED_EVENT_GAPS_LIMIT = 50

#: 投影与连接两段纯文本。语句分成两段是因为 ``kind`` 判定要拼进 ``LIFECYCLE_KIND`` ——
#: 那个常量是 ``kind`` 取值域的唯一来源(0039 的 ``DEFAULT`` 也从它渲染),这里不写第二份字面量。
_STARTED_EVENT_PROJECTION = (
    "SELECT e.job_id, e.event_seq, "
    "CASE WHEN c.job_id IS NULL THEN 'no-coordination-row' "
    "ELSE 'no-canonical-event' END AS reason "
    "FROM speech_job_events AS e "
    "LEFT JOIN coordination_jobs AS c ON c.job_id = e.job_id "
)
STARTED_EVENT_GAPS = (
    _STARTED_EVENT_PROJECTION + f"WHERE e.kind = '{LIFECYCLE_KIND}' AND e.state = 'running' "
    "AND NOT EXISTS (SELECT 1 FROM job_events AS j WHERE j.job_id = e.job_id) "
    "ORDER BY e.job_id, e.event_seq"
)


def started_event_gaps(connection: SpeechConnection) -> tuple[str, ...]:
    """列出示草案有 started、canonical 侧没有对应事件的 Job(口径见 ``STARTED_EVENT_GAPS``)。

    返回人能直接贴进工单的行,不是计数:「有 N 个 Job 缺 started」在切换读路径时既不能复核
    也不能豁免,而一行一个 Job 的缺口可以逐条归因(``no-coordination-row`` 与
    ``no-canonical-event`` 的处置不同)。

    **这是运维读,不是运行期读**:调用方给的是切换演练用的 owner DSN,与
    ``scripts/dev-env/portable-reconcile.py`` 的 ``--dsn`` 同一口径。派发身份从 S2 起**也能**
    跑它 —— ``job_event_grants`` 的列级 ``SELECT`` 已经补上 ``job_id``(读翻转与这条授权在
    同一提交里落地,否则 ``job_event.read`` 一上线就以 ``42501`` 失败)。它仍然定位成运维读,
    理由是用途不是权限:运行期不该为了「有没有缺口」多扫一遍事件表。
    """

    rows = connection.execute(STARTED_EVENT_GAPS).fetchall()
    return tuple(
        f"{row['job_id']}: draft started event at seq {row['event_seq']} "
        f"({row['reason']}) has no canonical job_events row"
        for row in rows[:STARTED_EVENT_GAPS_LIMIT]
    )


def _job(row: dict[str, Any]) -> DispatchJob:
    try:
        return DispatchJob(
            row["job_id"],
            row["workspace_id"],
            row["kind"],
            row["state"],
            int(row["status_version"]),
            int(row["created_at_us"]),
            row["request_digest"],
            row["request_document"],
        )
    except KeyError, TypeError, ValueError:
        raise DispatchProtocolError("Stored Job facts are invalid") from None


def _claim_job(view: ClaimedJobView, claim: BatchClaim, worker_id: str) -> ClaimedJob:
    """One won Job CAS plus its committed projection.

    ``lease_expires_at_us`` is converted back from the instant ``claim_batch`` wrote, not
    recomputed from ``lease_ms``: the renew and finish guards compare against the stored
    ``claim_expires_at``, so a single microsecond of drift here would move every later guard
    off the lease it is supposed to check (plan §8.77).
    """

    try:
        job = DispatchJob(
            view.job_id,
            view.workspace_id,
            # The view carries the stored strings; ``DispatchJob`` is the validator that
            # holds them to the laned-kind and public-state unions (it raises ValueError,
            # which becomes DispatchProtocolError below).
            cast(Any, view.kind),
            cast(Any, view.state),
            int(view.status_version),
            int(view.created_at_us),
            view.request_digest,
            # ``job.claimed_view`` keeps a missing anchor document NULL; ``DispatchJob``
            # needs the historical empty-string shape (the old claim COALESCEd it).
            view.request_document or "",
        )
    except KeyError, TypeError, ValueError:
        raise DispatchProtocolError("Stored Job facts are invalid") from None
    return ClaimedJob(job, worker_id, int(claim.claim_version), to_epoch_us(claim.expires_at))


class PostgresDispatchQueue:
    """One transaction claims due Jobs through the canonical claim bucket round.

    ``kinds`` names the lane this queue serves. It defaults to the ASR/TTS lane,
    so a worker never claims a voice generation Job whose engine adapter it does
    not own; a voice worker opts in explicitly instead of relying on row order.
    The filter is part of the claim statement (``job.candidates.lane*``), so a Job
    outside the lane neither consumes the batch limit nor turns into a protocol
    error that starves the kinds this worker owns.

    ``require_admission`` turns the production lane on: only a Job whose admission
    wrote a complete command, an attempt and a usable lease in the same commit is
    due. The predicate runs inside the claim statement (``job.candidates.*admitted``)
    rather than as post-filtering; an anchor left by an older release stays
    unreachable for the same reason.

    The default stays ``False`` because this queue also serves the legacy assemblies
    that create public Jobs without an admission pool. The constructor rejects a
    validation lane without admission instead of failing only at runtime.
    """

    def __init__(
        self,
        pool: SpeechPool,
        *,
        kinds: Iterable[str] = DISPATCHABLE_JOB_KINDS,
        require_admission: bool = False,
        timeout_seconds: float = 3,
    ) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Dispatch claims require bounded deadlines")
        if type(require_admission) is not bool:
            raise ValueError("Dispatch admission filter must be explicit")
        selected = tuple(kinds)
        if not selected:
            raise ValueError("Dispatch lane requires at least one Job kind")
        if len(set(selected)) != len(selected):
            raise ValueError("Dispatch lane Job kinds must be unique")
        if any(kind not in DISPATCHABLE_JOB_KIND_UNION for kind in selected):
            raise ValueError("Dispatch lane Job kind is outside the dispatchable union")
        if not require_admission and any(kind in VOICE_VALIDATION_JOB_KINDS for kind in selected):
            # Legacy anchor-only rows have no attempt/lease/command; admitting
            # them would make the worker claim a Job it cannot replay.
            raise ValueError("Voice validation dispatch requires admission")
        self._pool, self._timeout = pool, timeout_seconds
        self._kinds = selected
        self._require_admission = require_admission

    @property
    def kinds(self) -> tuple[str, ...]:
        return self._kinds

    @property
    def require_admission(self) -> bool:
        return self._require_admission

    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        identifier(worker_id)
        if not 1 <= lease_ms <= 60_000 or not 1 <= limit <= 32:
            raise ValueError("Dispatch claim bounds are invalid")
        return await asyncio.to_thread(self._claim, worker_id, lease_ms, limit)

    def _claim(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        """One bucket round claims at most ``limit`` due Jobs out of one transaction.

        The lane and the admission anchor travel as arguments, not as SQL assembled here:
        ``claim_batch`` selects one of the four registered candidate statements, takes the
        bucket lease, reclaims dead leases of that bucket, and CAS-es each Job into
        ``job_state = 'claimed'``. Nothing about the lane or the anchor is decided twice.

        ``lease_ms`` is the Job lease. The bucket lease never survives this call
        (``claim_batch`` releases every bucket it takes), so the single ``lease_ms``
        parameter stays sufficient -- plan §8.76.5 E.

        ``ContractError`` is deliberately **not** mapped to ``DispatchUnavailableError``:
        it reports broken canonical claim state (a missing bucket row, a fence that moved
        under the batch), which is a correctness alarm and must stay loud instead of
        becoming a retryable outage.
        """

        won: tuple[BatchClaim, ...] = ()
        views: tuple[ClaimedJobView, ...] = ()
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                # The database clock is the only clock: claim, lease and the candidate
                # predicate all derive from this one reading.
                now = now_us(connection)
                moment = _canonical_moment(now, field="now")
                won = claim_batch(
                    _contract_connection(connection),
                    owner_id=worker_id,
                    now=moment,
                    lease_until=_canonical_moment(now + lease_ms * 1000, field="lease_until"),
                    limit=limit,
                    start_bucket=CLAIM_ROUND_START_BUCKET,
                    kinds=self._kinds,
                    require_admission=self._require_admission,
                )
                views = claimed_jobs(
                    _contract_connection(connection),
                    job_ids=[claim.job_id for claim in won],
                )
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch claim storage is unavailable") from None
        by_id = {view.job_id: view for view in views}
        if len(by_id) != len(won):
            # A claimed Job without its own public row would be owned by nobody: the
            # claim is committed, so reporting it beats returning a shorter batch.
            raise DispatchProtocolError("Claimed Job projection is incomplete")
        claims = [
            _claim_job(by_id[claim.job_id], claim, worker_id)
            for claim in won
            if claim.job_id in by_id
        ]
        # Restore the pre-switch result order (the old statement ordered by
        # ``speech_jobs.created_at_us, job_id``); the view reads back by ``job_id``.
        claims.sort(key=lambda claim: (claim.job.created_at_us, claim.job.job_id))
        return claims


class PostgresDispatchStateStore:
    """Public lifecycle writes and coordination closure share one transaction."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Dispatch transitions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    async def reload(self, job_id: str) -> DispatchJob:
        return await asyncio.to_thread(self._reload, job_id)

    async def mark_running(self, job: DispatchJob) -> DispatchJob:
        return await asyncio.to_thread(self._mark_running, job)

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None:
        identifier(claim.worker_id)
        if not 1_000 <= lease_ms <= 60_000:
            raise ValueError("Dispatch renewal bounds are invalid")
        return await asyncio.to_thread(self._renew, claim, lease_ms)

    async def record_cancellation_not_submitted(self, job_id: str, *, observer: str) -> None:
        identifier(job_id)
        identifier(observer)
        return await asyncio.to_thread(self._record_cancellation_not_submitted, job_id, observer)

    async def record_progress(
        self, claim: ClaimedJob, *, unit: str, completed: int, total: int | None
    ) -> bool:
        if unit not in ("audio_ms", "text_codepoints", "items"):
            raise ValueError("Progress unit is invalid")
        if type(completed) is not int or completed < 0:
            raise ValueError("Progress value is invalid")
        if total is not None and (type(total) is not int or total < completed):
            raise ValueError("Progress total is invalid")
        return await asyncio.to_thread(self._record_progress, claim, unit, completed, total)

    async def finish(
        self,
        claim: ClaimedJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        return await asyncio.to_thread(self._finish, claim, target, result_json, error_code)

    def _reload(self, job_id: str) -> DispatchJob:
        identifier(job_id)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(_JOB_SELECT + "WHERE j.job_id = ?", (job_id,)).fetchall()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch state is unavailable") from None
        if len(rows) != 1:
            raise DispatchUnavailableError("Dispatch Job identity is absent or duplicated")
        return _job(rows[0])

    def _mark_running(self, job: DispatchJob) -> DispatchJob:
        """Append the started event at the next stored sequence and shadow it in canonical.

        The ``speech_jobs`` UPDATE decides whether this call appends at all (two workers
        holding stale copies of a pending Job must not both append), and the draft
        sequence is derived from the stored events instead of being assumed equal to
        ``status_version``. That equality only held because a pending Job carried exactly
        one event; deriving ``MAX(event_seq) + 1`` keeps the started fact insertable once
        a pending Job records more than one.

        The canonical head is locked *before* the public row, so this method keeps the
        module's lock order (coordination Job row, then the public row it projects) even
        though it is the only transition that mutates the public row first. The parent
        check itself is not repeated here: ``_append_canonical_event`` owns it, and a Job
        with no ``coordination_jobs`` row makes it fail the whole transaction, so neither
        representation moves. ``claim_version`` is not advanced — it is the lease fence,
        and bumping it would expire the claim the caller still holds.
        """
        changed = False
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                now = now_us(connection)
                # 事件头先锁:锁序是 coordination_jobs -> speech_jobs(模块 docstring)。这一读
                # 不判父行 —— 那个判定只有一处,在 ``_append_canonical_event`` 里 —— 它只保证
                # started 这一条迁移也按同一顺序取锁,不会成为状态机里唯一的反向边。
                connection.execute(STATEMENTS["job.event_head"], (job.job_id,)).fetchone()
                updated = connection.execute(
                    "UPDATE speech_jobs SET state = 'running', "
                    "status_version = status_version + 1, updated_at_us = ? "
                    "WHERE job_id = ? AND status_version = ? AND state = 'pending'",
                    (now, job.job_id, job.status_version),
                )
                if updated.rowcount == 1:
                    _append_lifecycle_event(
                        connection, job_id=job.job_id, state="running", occurred_at_us=now
                    )
                    # 同一笔事务里的 canonical 影子行(S1b,计划 §5)。started 的草案行由
                    # ``_append_lifecycle_event`` 写入,``kind`` 取 0039 的默认值 ``lifecycle``;
                    # 摘要覆盖 ``kind``,所以这里必须用同一个字面量,否则两侧摘要永远对不上。
                    _append_canonical_event(
                        connection,
                        job_id=job.job_id,
                        state="running",
                        occurred_at_us=now,
                        kind=LIFECYCLE_KIND,
                    )
                    changed = True
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch transition failed") from None
        if changed:
            return replace(job, state="running", status_version=job.status_version + 1)
        current = self._reload(job.job_id)
        if current.state == "running" and current.status_version >= job.status_version + 1:
            return current
        if current.state in ("succeeded", "failed"):
            return current
        raise DispatchConflictError("Public Job did not enter running")

    def _renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None:
        """Push the canonical ``claim_expires_at`` forward while this claim still owns it.

        The guard is the canonical triple (owner, version, ``claimed``); the returned
        expiry is the very instant written, so ``_finish`` compares against the stored
        value instead of a locally recomputed one.
        """
        renewed = False
        expires_us = 0
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                expires_us = now_us(connection) + lease_ms * 1000
                updated = connection.execute(
                    "UPDATE coordination_jobs SET claim_expires_at = ? "
                    "WHERE job_id = ? AND claim_owner_id = ? AND claim_version = ? "
                    "AND job_state = ?",
                    (
                        _canonical_moment(expires_us, field="lease_expires_at"),
                        claim.job.job_id,
                        claim.worker_id,
                        claim.claim_version,
                        _CLAIMED_STATE,
                    ),
                )
                renewed = updated.rowcount == 1
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch lease renewal is unavailable") from None
        if not renewed:
            return None
        return replace(claim, lease_expires_at_us=expires_us)

    def _record_progress(
        self, claim: ClaimedJob, unit: str, completed: int, total: int | None
    ) -> bool:
        """Append one strictly advancing progress fact while this claim owns the Job.

        The row is committed only for a running public Job whose declared unit
        matches the fact. The canonical Job row lock serialises this append with
        terminal transitions; the event guard rejects a terminal Job, a non-advancing
        value or an out-of-order sequence even if a writer bypasses this check.
        """
        job = claim.job
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                now = now_us(connection)
                owned = connection.execute(
                    "SELECT job_id FROM coordination_jobs WHERE job_id = ? "
                    "AND claim_owner_id = ? AND claim_version = ? AND job_state = ? "
                    "AND claim_expires_at > ? FOR UPDATE",
                    (
                        job.job_id,
                        claim.worker_id,
                        claim.claim_version,
                        _CLAIMED_STATE,
                        _canonical_moment(now, field="now"),
                    ),
                ).fetchone()
                if owned is None:
                    return False
                current = connection.execute(
                    "SELECT j.state, s.progress_unit FROM speech_jobs AS j "
                    "JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
                    "WHERE j.job_id = ? FOR UPDATE OF j",
                    (job.job_id,),
                ).fetchone()
                if (
                    current is None
                    or current["state"] != "running"
                    or current["progress_unit"] != unit
                ):
                    return False
                latest = connection.execute(
                    "SELECT COALESCE(MAX(event_seq), -1) AS seq, "
                    "MAX(CASE WHEN kind = 'progress' THEN progress_completed END) AS completed "
                    "FROM speech_job_events WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
                if latest is None:
                    return False
                previous = -1 if latest["completed"] is None else int(latest["completed"])
                if completed <= previous:
                    return False
                connection.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                    "progress_completed, progress_total) VALUES (?, ?, 'running', "
                    "?, 'progress', ?, ?, ?)",
                    (job.job_id, int(latest["seq"]) + 1, now, unit, completed, total),
                )
                # 同一笔事内写 canonical 影子事件(S1);失败则草案事件一起回滚。
                _append_canonical_event(
                    connection,
                    job_id=job.job_id,
                    state="running",
                    occurred_at_us=now,
                    kind=PROGRESS_KIND,
                    progress_completed=completed,
                    progress_total=total,
                )
            return True
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch progress storage failed") from None

    def _record_cancellation_not_submitted(self, job_id: str, observer: str) -> None:
        # 时钟不再走 CTE:同一行的时间戳由参数给出,插入是否真的发生由 `insert_if_absent`
        # 的返回值决定 —— 受限方言既没有 `WITH`,也没有 `ON CONFLICT`。
        query = (
            f"INSERT INTO {NOT_SUBMITTED_TABLE} (job_id, observed_at_us, observer) "
            "SELECT j.job_id, ?, ? FROM speech_jobs AS j "
            "WHERE j.job_id = ? AND j.state = 'cancelling'"
        )
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                insert_if_absent(connection, query, (now_us(connection), observer, job_id))
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError(
                "Cancellation not-submitted observation is unavailable"
            ) from None

    def _finish(
        self,
        claim: ClaimedJob,
        target: str,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        """Guarded terminal transition: canonical close and public result in one commit.

        The canonical guards (owner, version, ``claimed``, unexpired lease) are re-checked
        at close time as well as at the start, with the same instant: a guard that passes
        and a close that then fails on expiry would leave the public Job settled while the
        coordination row still advertises the lease.

        A finished Job keeps its ``claim_expires_at`` instead of clearing it: the terminal
        ``job_state`` already says the lease is over, no predicate reclaims a non-``claimed``
        row, and the stored instant stays the evidence of which lease ended the Job -- the
        same rule ``job.reclaim`` follows when it re-pends an expired claim.
        """
        if target not in ("succeeded", "failed"):
            raise ValueError("Dispatch can finish only in a terminal state")
        job = claim.job
        terminal: DispatchJob | None = None
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                # 守卫、迁移与关闭必须是同一个时间点:否则守卫通过、关闭却按过期失败.
                now = now_us(connection)
                moment = _canonical_moment(now, field="now")
                owned = connection.execute(
                    "SELECT job_id FROM coordination_jobs WHERE job_id = ? "
                    "AND claim_owner_id = ? AND claim_version = ? AND job_state = ? "
                    "AND claim_expires_at > ? FOR UPDATE",
                    (job.job_id, claim.worker_id, claim.claim_version, _CLAIMED_STATE, moment),
                ).fetchone()
                if owned is None:
                    # No public or event write may occur without the current lease.
                    raise _TerminalGuardLostError
                changed = connection.execute(
                    "UPDATE speech_jobs SET state = ?, "
                    "status_version = status_version + 1, result_json = ?, "
                    "error_code = ?, updated_at_us = ? "
                    "WHERE job_id = ? AND status_version = ? AND state = ?",
                    (
                        target,
                        result_json,
                        error_code,
                        now,
                        job.job_id,
                        job.status_version,
                        job.state,
                    ),
                )
                if changed.rowcount != 1:
                    # The public Job was settled or advanced by another writer.
                    raise _TerminalGuardLostError
                _append_lifecycle_event(
                    connection, job_id=job.job_id, state=target, occurred_at_us=now
                )
                # 同一笔事务里写 canonical 影子事件(S1):终态草案行与 canonical 行要么都在、
                # 要么都不在,否则两侧的终态事实无法对账。
                _append_canonical_event(
                    connection,
                    job_id=job.job_id,
                    state=target,
                    occurred_at_us=now,
                    # 草案的终态行由 ``_append_lifecycle_event`` 写入,``kind`` 取 0039 的默认值;
                    # 摘要覆盖 ``kind``,所以这里必须用同一个字面量,否则两侧摘要永远对不上。
                    kind=LIFECYCLE_KIND,
                )
                closed = connection.execute(
                    "UPDATE coordination_jobs SET job_state = ? WHERE job_id = ? "
                    "AND job_state = ? AND claim_owner_id = ? AND claim_version = ? "
                    "AND claim_expires_at > ?",
                    (
                        target,
                        job.job_id,
                        _CLAIMED_STATE,
                        claim.worker_id,
                        claim.claim_version,
                        moment,
                    ),
                )
                if closed.rowcount != 1:
                    # Roll back the public transition if the lease expired before close.
                    raise _TerminalGuardLostError
                terminal = replace(
                    job,
                    state=cast(Any, target),
                    status_version=job.status_version + 1,
                )
        except _TerminalGuardLostError:
            terminal = None
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch terminal transition failed") from None
        if terminal is not None:
            return terminal
        raise DispatchClaimLostError("Dispatch lease was lost before the terminal transition")


class PostgresLeaseSettler:
    """Return a settled or cancelled public Job's capacity through the lease service.

    The dispatcher owns terminal settlement for asr/tts and cancellation
    reconciliation owns it after a verified stop: either way, a Job that kept its
    admission lease would hold the attempt and every physical slot forever.
    Release remains a ``LeaseService`` decision over the original identity; this
    adapter only resolves the single lease, supplies its current version and
    replays a conflicting or unknown outcome. A release that cannot prove stop
    and cleanup returns ``False`` and leaves the lease for reconciliation; it
    never rewrites the Job's already committed terminal outcome.
    """

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Lease settlement requires bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds
        self._leases = LeaseService(
            PostgresLeaseTransactions(pool, timeout_seconds=timeout_seconds)
        )

    async def release(self, operation_id: str) -> bool:
        identifier(operation_id)
        return await asyncio.to_thread(self.release_sync, operation_id)

    def release_sync(self, operation_id: str) -> bool:
        """Blocking form for callers that already run their own storage step."""
        identifier(operation_id)
        return self._release(operation_id)

    def _lease_id(self, operation_id: str) -> str | None:
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(
                    "SELECT lease_id FROM admission_leases WHERE operation_id = ? "
                    "ORDER BY lease_id",
                    (operation_id,),
                ).fetchall()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Lease settlement storage is unavailable") from None
        if not rows:
            return None
        if len(rows) != 1:
            # Multi-stage admissions are not dispatched by this lane; leave the
            # whole attempt for reconciliation instead of releasing one stage.
            return ""
        return str(rows[0]["lease_id"])

    def lease_ids_sync(self, operation_id: str) -> tuple[str, ...]:
        """Every admission lease of one Job, in a stable order."""
        return tuple(lease.lease_id for lease in self.leases_sync(operation_id))

    def leases_sync(self, operation_id: str) -> tuple[LeaseSnapshot, ...]:
        """Every admission lease of one Job with the gateway's execution facts."""
        identifier(operation_id)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(
                    "SELECT l.lease_id, l.lease_state, l.cleanup_state, l.reserved_slots, "
                    "s.execution_state, s.cleanup_state AS execution_cleanup_state "
                    "FROM admission_leases AS l "
                    "LEFT JOIN submit_intents AS s ON s.lease_id = l.lease_id "
                    "WHERE l.operation_id = ? "
                    "ORDER BY l.lease_id",
                    (operation_id,),
                ).fetchall()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Lease settlement storage is unavailable") from None
        return tuple(
            LeaseSnapshot(
                lease_id=str(row["lease_id"]),
                lease_state=str(row["lease_state"]),
                cleanup_state=str(row["cleanup_state"]),
                reserved_slots=int(row["reserved_slots"]),
                execution_state=(
                    None if row["execution_state"] is None else str(row["execution_state"])
                ),
                execution_cleanup_state=(
                    None
                    if row["execution_cleanup_state"] is None
                    else str(row["execution_cleanup_state"])
                ),
            )
            for row in rows
        )

    def clock_sync(self) -> int:
        """Read the database clock used to validate an observed timestamp."""
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                observed = now_us(connection)
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Lease settlement storage is unavailable") from None
        return observed

    def isolate_sync(self, operation_id: str, *, reason_code: str) -> LeaseIsolation | None:
        """Move every lease of one Job into the quarantined bucket, or say why not.

        Quarantine moves reserved slots and the attempt quota from ``held`` to
        ``quarantined``: the capacity stays unavailable, but it is no longer
        reported as held by an execution that is still running. The domain rule
        decides whether the persisted execution facts authorize that, so a lease
        whose execution never became ``unknown_quarantined``/``quarantined`` is
        refused and keeps exactly what its owner recorded.

        Every lease is attempted: one refused lease must not hide the others, or
        the Job would be judged against a partially isolated operation. ``None``
        means the leases could not even be read, which is a retryable storage
        failure rather than a per-lease answer.
        """
        try:
            leases, now_us = self.leases_sync(operation_id), self.clock_sync()
        except DispatchUnavailableError:
            return None
        quarantined: list[str] = []
        gaps: list[LeaseIsolationGap] = []
        for lease in leases:
            if lease.lease_state == "quarantined":
                quarantined.append(lease.lease_id)
                continue
            if lease.lease_state == "released":
                continue  # Capacity is already back; nothing to isolate.
            result = self._leases.quarantine(QuarantineLease(lease.lease_id, reason_code, now_us))
            if result.fact is not None and result.fact.lease_state == "quarantined":
                quarantined.append(lease.lease_id)
                continue
            gaps.append(
                LeaseIsolationGap(
                    lease_id=lease.lease_id,
                    lease_state=lease.lease_state,
                    cleanup_state=lease.cleanup_state,
                    execution_state=lease.execution_state,
                    execution_cleanup_state=lease.execution_cleanup_state,
                    reserved_slots=lease.reserved_slots,
                    reason=(
                        "lease_storage_unavailable"
                        if result.outcome == "unknown"
                        else (result.error_code or "reconciliation_required")
                    ),
                )
            )
        return LeaseIsolation(
            operation_id=operation_id, quarantined=tuple(quarantined), gaps=tuple(gaps)
        )

    def _release(self, operation_id: str) -> bool:
        lease_id = self._lease_id(operation_id)
        if lease_id is None:
            return True
        if not lease_id:
            return False
        for _ in range(2):
            current = self._leases.query(lease_id)
            if current.fact is None:
                return False
            if current.fact.lease_state == "released":
                return True
            result = self._leases.release(ReleaseLease(lease_id, current.fact.version))
            if result.fact is not None and result.fact.lease_state == "released":
                return True
            if result.outcome != "unknown":
                return False
        return False


__all__ = ["PostgresDispatchQueue", "PostgresDispatchStateStore", "PostgresLeaseSettler"]
