"""S1/S1b 事件双写:整条生命周期同事务写 canonical ``job_events`` 影子行。

判据不是「新表里有行」,而是两侧能被**对账**:

* canonical 侧的 ``event_seq`` 是从 1 起的密序号 —— 它不是草案的 ``MAX+1``,而是
  ``coordination_jobs.last_event_seq + 1``。草案表多一条 canonical 侧永远没有的创建事实
  (``event_seq`` 0),所以两条序列不是同一条;本文件把这件事断言成事实,而不是绕开它。
  S1b 之后 ``mark_running`` 的 started 也双写,于是「一次完整生命周期
  (``mark_running`` → ``record_progress`` → ``finish``)」的 canonical 序号必须是
  ``1..N`` **无缺口** —— 留一个洞就是客户端从 progress 直接跳到 success 的那种症状。
* 每一行的 ``event_digest`` 必须等于用 ``claims.event_digest()`` 按**canonical 序号**对
  草案行的字段(`state` / `occurred_at_us` / `kind` / ``progress_*``)重算的值。摘要覆盖
  ``event_seq``,所以拿草案序号算出来的摘要是另一条事实 —— 那样两侧永远无法对账。
* ``payload_ref`` 一律 ``NULL``(搬迁不引用外部负载的产品决定,已并入 0068)。
* 一次失败必须同时回退两侧:把 canonical 表藏起来再跑同一迁移,草案事件与事件头都不许前进。
  这条对 started 同样成立:拒绝/失败之后公开 Job 仍是 ``pending``,草案侧没有 started 行。

**没有 ``coordination_jobs`` 行的 Job 是拒绝,不是跳过。** ``job_events.job_id`` 的外键指向
``coordination_jobs``,没有父行就没有合法的影子行;而跳过留下的是「草案有 started、canonical
永远没有」这种对账之后补不回来的缺口。本文件断言三件事:这种 Job 根本进不了派发队列(所有候选
语句都 ``FROM coordination_jobs``,所以「以前能派发的 Job」不可能是这个形状)、直接调用状态机
被类型化拒绝且不留半成品、以及历史缺口(本修订之前 ``mark_running`` 只写草案)能被
``started_event_gaps`` 点名。

每一条都配了反向注入(见报告):删掉 started 的 canonical 追加、把 canonical 追加挪到事务
之外、去掉父行检查,本文件都会红。``_reconcile`` 就是那个唯一的对账口径:两侧逐行比较,
不匹配时报出 job 与两侧的 seq。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_public_jobs_http import PREFIX, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db
from tests.support.legacy_jobs import seed_public_only_job

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchProtocolError,
    DispatchUnavailableError,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
    started_event_gaps,
)
from speechnexus_portable_sql.claims import event_digest, utc_from_epoch_us

pytestmark = pytest.mark.integration
#: 终态:这两个 state 由 ``_finish`` 写入,也是双写覆盖的第二类生命周期事件。
TERMINAL_STATES = ("succeeded", "failed")
#: ``speech_jobs`` 的 CHECK 要求 succeeded 必须带结果文档;本文件不关心内容,只要求它是
#: 合法 JSON —— 双写判据与结果形状无关。
RESULT_JSON = json.dumps({"object": "nexus.job_result", "synthetic": True})
#: 手工种的 Job 用的时刻:只要求是合法的微秒 epoch,内容不参与任何判据。
LEGACY_MOMENT_US = 1_760_000_000_000_000
LEGACY_WORKSPACE = "ws-legacy"
LEGACY_DIGEST = "sha256:" + "b" * 64


def _app(db: JobSandbox) -> Any:
    from tests.integration.test_public_jobs_http import _settings_for

    return create_app(settings=_settings_for(db), jobs=db.jobs)


def _body(reference: str) -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic canonical double write"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": reference,
    }


async def _submitted(db: JobSandbox, reference: str) -> str:
    app = _app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            PREFIX, json=_body(reference), headers={"Idempotency-Key": reference + "-key"}
        )
        assert created.status_code == 202, created.text
        return str(created.json()["id"])


async def _running_claim(db: JobSandbox, reference: str) -> tuple[PostgresDispatchStateStore, Any]:
    job_id = await _submitted(db, reference)
    queue = PostgresDispatchQueue(db.pool)
    states = PostgresDispatchStateStore(db.pool)
    claims = await queue.claim_due("worker-canonical-a", 30_000, 1)
    assert [claim.job.job_id for claim in claims] == [job_id]
    running = await states.mark_running(claims[0].job)
    assert running.state == "running"
    return states, replace(claims[0], job=running)


def _draft_events(db: JobSandbox, job_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT event_seq, state, occurred_at_us, kind, progress_completed, progress_total "
        "FROM speech_job_events WHERE job_id = %s ORDER BY event_seq",
        (job_id,),
    )


def _canonical_events(db: JobSandbox, job_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT event_seq, event_digest, payload_ref FROM job_events "
        "WHERE job_id = %s ORDER BY event_seq",
        (job_id,),
    )


def _head(db: JobSandbox, job_id: str) -> int:
    rows = db.query("SELECT last_event_seq FROM coordination_jobs WHERE job_id = %s", (job_id,))
    assert len(rows) == 1
    return int(rows[0]["last_event_seq"])


def _occurred_at(value: int) -> datetime:
    """epoch 微秒 -> 摘要输入:换算只在 ``claims.utc_from_epoch_us`` 定义一次。"""

    moment = utc_from_epoch_us(value, field="occurred_at")
    assert moment is not None
    return moment


def _covered(draft: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """本切片负责双写的草案行,顺序即 canonical 侧一一对应的顺序。

    覆盖 started(``kind = lifecycle`` 且 ``state = running``)、进度(``kind = progress``)
    与终态(``succeeded`` / ``failed``)。**不含创建事实** ``event_seq`` 0:它是提交那一刻的
    pending 行,摘要记在 ``coordination_jobs.initial_event_digest`` 与 outbox 上,canonical
    ``job_events`` 按设计没有它(S2/S5 之前也不会有);把它算进来会让对账对每一行差一。
    """

    return [
        row
        for row in draft
        if str(row["kind"]) == "progress" or str(row["state"]) in ("running", "succeeded", "failed")
    ]


def _reconcile(db: JobSandbox, job_id: str) -> list[dict[str, Any]]:
    """两侧逐行对账;任何不一致都以 job 与两侧 seq 报错,不返回布尔值。

    口径:canonical 行按 ``event_seq`` 升序,与草案侧被覆盖行(started + 进度 + 终态)按
    ``event_seq`` 升序一一对应。canonical 侧没有 ``state``/``progress_*`` 列,所以值只能来自
    草案行,摘要必须按 canonical 序号重算 —— 这正是「两条表示描述同一条事实」的定义。
    """

    covered = _covered(_draft_events(db, job_id))
    canonical = _canonical_events(db, job_id)
    if len(covered) != len(canonical):
        raise AssertionError(
            f"job {job_id}: canonical has {len(canonical)} event(s) "
            f"{[row['event_seq'] for row in canonical]}, the draft side covers "
            f"{len(covered)} {[row['event_seq'] for row in covered]}"
        )
    for index, (fact, stored) in enumerate(zip(covered, canonical, strict=True)):
        seq = int(stored["event_seq"])
        expected = event_digest(
            job_id=job_id,
            event_seq=Decimal(seq),
            state=str(fact["state"]),
            occurred_at=_occurred_at(int(fact["occurred_at_us"])),
            kind=None if fact["kind"] is None else str(fact["kind"]),
            progress_completed=(
                None if fact["progress_completed"] is None else int(fact["progress_completed"])
            ),
            progress_total=None if fact["progress_total"] is None else int(fact["progress_total"]),
            payload_ref=None,
        )
        if stored["payload_ref"] is not None:
            raise AssertionError(
                f"job {job_id}: canonical seq {seq} carries payload_ref "
                f"{stored['payload_ref']!r}; migrations never reference a payload"
            )
        if str(stored["event_digest"]) != expected:
            raise AssertionError(
                f"job {job_id}: canonical seq {seq} digest {stored['event_digest']} != "
                f"recomputed {expected} (draft seq {fact['event_seq']}, "
                f"state {fact['state']!r}, occurred_at_us {fact['occurred_at_us']})"
            )
        assert seq == index + 1, (
            f"job {job_id}: canonical seq {seq} at position {index}; the canonical sequence "
            "must be dense from 1"
        )
    return canonical


def _gaps(db: JobSandbox) -> tuple[str, ...]:
    """对账查询跑在**运维身份**(owner)上,与 ``portable-reconcile.py --dsn`` 同口径。

    派发身份从 S2 起也能跑它(``job_event_grants`` 补授了 ``job_id``,见
    ``test_canonical_event_read_pg.py`` 的 42501 回归),但它是运维读、不是运行期读,所以
    本文件仍用 owner DSN 跑,与 ``portable-reconcile.py`` 同一条口径。
    """

    return started_event_gaps(db.admin)


async def test_full_lifecycle_appends_dense_canonical_events(jobs_db: JobSandbox) -> None:
    states, claim = await _running_claim(jobs_db, "synthetic-canonical-dense")
    job_id = claim.job.job_id

    # started 已经双写:canonical 侧此刻正好是 1..1,而草案侧是创建事实 + started。
    assert [int(row["event_seq"]) for row in _canonical_events(jobs_db, job_id)] == [1]
    assert [(int(row["event_seq"]), row["state"]) for row in _draft_events(jobs_db, job_id)] == [
        (0, "pending"),
        (1, "running"),
    ]

    assert await states.record_progress(claim, unit="audio_ms", completed=10, total=None)
    assert await states.record_progress(claim, unit="audio_ms", completed=20, total=None)
    await states.finish(claim, "succeeded", result_json=RESULT_JSON, error_code=None)

    # 密序号从 1 起、无缺口:started(1)+ 两条进度(2、3)+ 终态(4)。
    canonical = _reconcile(jobs_db, job_id)
    assert [int(row["event_seq"]) for row in canonical] == [1, 2, 3, 4]
    assert all(row["payload_ref"] is None for row in canonical)
    # 事件头与 canonical 的 MAX 一致:否则下一次双写会重号,主键以 23505 失败。
    assert _head(jobs_db, job_id) == 4

    # 草案序号仍是它自己的序列(0..4),比 canonical 多一条创建事实。
    draft = _draft_events(jobs_db, job_id)
    assert [int(row["event_seq"]) for row in draft] == [0, 1, 2, 3, 4]
    assert [row["state"] for row in draft] == [
        "pending",
        "running",
        "running",
        "running",
        "succeeded",
    ]
    # started 的草案行由 ``_append_lifecycle_event`` 写入,``kind`` 取 0039 的 ``DEFAULT``:
    # 摘要覆盖 ``kind``,所以 canonical 侧必须用同一个字面量。
    assert [str(row["kind"]) for row in draft] == [
        "lifecycle",
        "lifecycle",
        "progress",
        "progress",
        "lifecycle",
    ]
    # 一个正常推进的 Job 不该出现在缺口清单里。
    assert not [line for line in _gaps(jobs_db) if job_id in line]


async def test_reconciliation_matches_both_stores_row_by_row(jobs_db: JobSandbox) -> None:
    """对账用例:同一 Job 读两侧、逐行比;不一致时报 job 与 seq(见 ``_reconcile``)。"""

    states, claim = await _running_claim(jobs_db, "synthetic-canonical-reconcile")
    job_id = claim.job.job_id
    assert await states.record_progress(claim, unit="audio_ms", completed=5, total=None)
    await states.finish(claim, "failed", result_json=None, error_code="engine_failure")

    canonical = _reconcile(jobs_db, job_id)
    assert [int(row["event_seq"]) for row in canonical] == [1, 2, 3]
    assert _head(jobs_db, job_id) == 3


async def test_a_failed_canonical_append_rolls_back_the_started_transition(
    jobs_db: JobSandbox,
) -> None:
    """``_mark_running`` 的同一笔事务:canonical 不可写时公开 Job 仍是 ``pending``。

    注入的是「canonical 对象不可写」这一种失败(把表改名,写入以 42P01 失败)。同一笔事务里的
    ``speech_jobs`` UPDATE 与草案 started 行必须跟着回滚;把 canonical 追加挪到事务之外,这条
    用例会以「Job 已经 running、草案多了一条 started」变红。
    """

    job_id = await _submitted(jobs_db, "synthetic-canonical-started-rollback")
    queue = PostgresDispatchQueue(jobs_db.pool)
    states = PostgresDispatchStateStore(jobs_db.pool)
    claims = await queue.claim_due("worker-canonical-started", 30_000, 1)
    assert [claim.job.job_id for claim in claims] == [job_id]
    before_draft = _draft_events(jobs_db, job_id)
    assert [row["state"] for row in before_draft] == ["pending"]

    hidden = "job_events_hidden"
    jobs_db.admin.execute(f"ALTER TABLE job_events RENAME TO {hidden}")
    try:
        with pytest.raises(DispatchUnavailableError):
            await states.mark_running(claims[0].job)
    finally:
        jobs_db.admin.execute(f"ALTER TABLE {hidden} RENAME TO job_events")

    # 三处都不许前进:公开 Job、草案事件、canonical 事件头。
    assert jobs_db.query(
        "SELECT state, status_version FROM speech_jobs WHERE job_id = %s", (job_id,)
    ) == [{"state": "pending", "status_version": 1}]
    assert _draft_events(jobs_db, job_id) == before_draft
    assert _canonical_events(jobs_db, job_id) == []
    assert _head(jobs_db, job_id) == 0

    # 恢复后同一条迁移正常推进:失败没有留下半成品。
    running = await states.mark_running(claims[0].job)
    assert running.state == "running"
    assert [int(row["event_seq"]) for row in _reconcile(jobs_db, job_id)] == [1]
    assert _head(jobs_db, job_id) == 1


async def test_a_failed_canonical_append_rolls_back_the_progress_event(
    jobs_db: JobSandbox,
) -> None:
    """canonical 侧失败时整笔迁移回滚:草案事件、事件头、canonical 都不前进。

    注入的是「canonical 对象不可写」这一种失败(把表改名,写入以 42P01 失败)。同一笔事务里的
    草案 INSERT 必须跟着回滚;若把 canonical 追加挪到事务之外,这条用例会以「草案行泄漏」变红。
    """

    states, claim = await _running_claim(jobs_db, "synthetic-canonical-rollback")
    job_id = claim.job.job_id
    assert await states.record_progress(claim, unit="audio_ms", completed=10, total=None)
    before_draft = _draft_events(jobs_db, job_id)
    before_head = _head(jobs_db, job_id)
    assert before_head == 2

    hidden = "job_events_hidden"
    jobs_db.admin.execute(f"ALTER TABLE job_events RENAME TO {hidden}")
    try:
        with pytest.raises(DispatchUnavailableError):
            await states.record_progress(claim, unit="audio_ms", completed=20, total=None)
    finally:
        jobs_db.admin.execute(f"ALTER TABLE {hidden} RENAME TO job_events")

    assert _draft_events(jobs_db, job_id) == before_draft, (
        "the draft progress event survived a failed canonical append: the two writes are "
        "not in one transaction"
    )
    assert _head(jobs_db, job_id) == before_head
    assert [int(row["event_seq"]) for row in _canonical_events(jobs_db, job_id)] == [1, 2]

    # 恢复之后同一事务仍然成立:失败没有留下半成品,下一次迁移正常推进到 3。
    assert await states.record_progress(claim, unit="audio_ms", completed=20, total=None)
    assert _head(jobs_db, job_id) == 3
    assert [int(row["event_seq"]) for row in _reconcile(jobs_db, job_id)] == [1, 2, 3]


async def test_a_failed_canonical_append_rolls_back_the_terminal_transition(
    jobs_db: JobSandbox,
) -> None:
    """``_finish`` 的同一笔事务:canonical 失败时公开 Job 仍是 running、协调行仍是 claimed。"""

    states, claim = await _running_claim(jobs_db, "synthetic-canonical-terminal-rollback")
    job_id = claim.job.job_id
    assert await states.record_progress(claim, unit="audio_ms", completed=10, total=None)
    before_draft = _draft_events(jobs_db, job_id)

    hidden = "job_events_hidden"
    jobs_db.admin.execute(f"ALTER TABLE job_events RENAME TO {hidden}")
    try:
        with pytest.raises(DispatchUnavailableError):
            await states.finish(claim, "succeeded", result_json=RESULT_JSON, error_code=None)
    finally:
        jobs_db.admin.execute(f"ALTER TABLE {hidden} RENAME TO job_events")

    # 四处都不许前进:公开 Job、协调行、草案终态事件、canonical 事件头。
    assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
        {"state": "running"}
    ]
    assert jobs_db.query(
        "SELECT job_state FROM coordination_jobs WHERE job_id = %s", (job_id,)
    ) == [{"job_state": "claimed"}]
    assert _draft_events(jobs_db, job_id) == before_draft
    assert _head(jobs_db, job_id) == 2
    assert [int(row["event_seq"]) for row in _reconcile(jobs_db, job_id)] == [1, 2]

    # 恢复后终态迁移把两侧一起推进。
    finished = await states.finish(claim, "succeeded", result_json=RESULT_JSON, error_code=None)
    assert finished.state == "succeeded"
    assert [int(row["event_seq"]) for row in _reconcile(jobs_db, job_id)] == [1, 2, 3]
    assert _head(jobs_db, job_id) == 3


def _seed_legacy(jobs_db: JobSandbox, job_id: str) -> None:
    seed_public_only_job(
        jobs_db.admin.execute,
        job_id=job_id,
        workspace_id=LEGACY_WORKSPACE,
        kind="tts",
        model_id="tts-quality",
        request_digest=LEGACY_DIGEST,
        created_at_us=LEGACY_MOMENT_US,
    )


async def test_a_job_without_a_coordination_row_is_refused_before_any_write(
    jobs_db: JobSandbox,
) -> None:
    """没有 ``coordination_jobs`` 行的 Job:``mark_running`` 类型化拒绝,且不留半成品。

    判据是「拒绝发生在写任何一侧之前」:公开 Job 仍 ``pending``、草案没有 started、canonical
    没有行、事件头也没动。跳过(只写草案)会让这个 Job 多出一条 canonical 永远补不上的
    started 事实,并把外键违规推迟到某次真正的写入上。
    """

    job_id = "legacy-public-only-refused"
    _seed_legacy(jobs_db, job_id)
    states = PostgresDispatchStateStore(jobs_db.pool)
    job = await states.reload(job_id)
    assert job.state == "pending"

    with pytest.raises(DispatchProtocolError) as caught:
        await states.mark_running(job)
    # 拒绝要点名 Job:否则运维还得自己反推是哪一条。
    assert job_id in str(caught.value)

    assert jobs_db.query(
        "SELECT state, status_version FROM speech_jobs WHERE job_id = %s", (job_id,)
    ) == [{"state": "pending", "status_version": 1}]
    assert [(int(row["event_seq"]), row["state"]) for row in _draft_events(jobs_db, job_id)] == [
        (0, "pending")
    ]
    assert _canonical_events(jobs_db, job_id) == []
    assert jobs_db.query("SELECT 1 AS n FROM coordination_jobs WHERE job_id = %s", (job_id,)) == []
    # 对账不点名它:拒绝没有留下「草案有 started」的缺口。
    assert not [line for line in _gaps(jobs_db) if job_id in line]


async def test_the_dispatch_queue_cannot_hand_over_a_job_without_a_coordination_row(
    jobs_db: JobSandbox,
) -> None:
    """队列永远不会把这种 Job 交给状态机:候选语句全部 ``FROM coordination_jobs``。

    这条用例是「拒绝不会打断以前能派发的 Job」的**可执行证据**:同一轮里有一个正常的
    可领取 Job(对照组),而没有 canonical 父行的那个 Job 既不在候选集里,也不会被交出去。
    """

    claimable = await _submitted(jobs_db, "synthetic-canonical-control")
    legacy = "legacy-public-only-claim"
    _seed_legacy(jobs_db, legacy)

    claims = await PostgresDispatchQueue(jobs_db.pool).claim_due("worker-legacy-proof", 30_000, 4)
    assert [claim.job.job_id for claim in claims] == [claimable]


async def test_the_reconciliation_names_a_started_event_with_no_canonical_counterpart(
    jobs_db: JobSandbox,
) -> None:
    """历史缺口:本修订之前 ``mark_running`` 只写草案,那条 started 永远补不上。

    这里复刻的是 S1 写侧留下的**数据形状**(草案多一条 started、canonical 侧一条都没有),
    而不是重新实现写者 —— ``adopt_historical_events`` 对已有事件的 Job 直接拒绝,所以一次性
    回填不是修补手段。读路径切 canonical(S2)之前,这批 Job 必须能被点名,否则会被当成
    写入失败或 SSE 缺口。
    """

    job_id = await _submitted(jobs_db, "synthetic-pre-s1b")
    jobs_db.admin.execute(
        "UPDATE speech_jobs SET state = 'running', status_version = status_version + 1 "
        "WHERE job_id = %s",
        (job_id,),
    )
    jobs_db.admin.execute(
        "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
        "SELECT %s, COALESCE(MAX(event_seq), -1) + 1, 'running', %s "
        "FROM speech_job_events WHERE job_id = %s",
        (job_id, LEGACY_MOMENT_US, job_id),
    )
    assert _canonical_events(jobs_db, job_id) == []

    named = [line for line in _gaps(jobs_db) if job_id in line]
    assert len(named) == 1, named
    assert "no-canonical-event" in named[0]
    assert "started event at seq 1" in named[0]


def test_the_canonical_sequence_is_not_the_draft_sequence() -> None:
    """摘要覆盖 ``event_seq``;同一个 Job 的两条序号不能互换(静态判据)。

    这条断言没有数据库也能跑:它钉住「同一 (job, seq) 的摘要不同」这件事,防止有人把
    ``_append_canonical_event`` 改成复用草案序号 —— 两条序列只在「每个草案事实都有 canonical
    双胞胎」时才逐条相等,而创建事实永远没有,所以它们不是同一条序列。
    """

    moment = _occurred_at(1_700_000_000_000_000)
    assert moment.tzinfo is None and moment.year == 2023
    canonical = event_digest(
        job_id="job-1",
        event_seq=Decimal(1),
        state="running",
        occurred_at=moment,
        kind="progress",
        progress_completed=10,
        payload_ref=None,
    )
    draft = event_digest(
        job_id="job-1",
        event_seq=Decimal(3),
        state="running",
        occurred_at=moment,
        kind="progress",
        progress_completed=10,
        payload_ref=None,
    )
    assert canonical != draft
    # 摘要对同一输入是稳定的,并且时间按 UTC 微秒定长折叠。
    assert canonical == event_digest(
        job_id="job-1",
        event_seq=Decimal(1),
        state="running",
        occurred_at=moment.replace(tzinfo=UTC),
        kind="progress",
        progress_completed=10,
        payload_ref=None,
    )
