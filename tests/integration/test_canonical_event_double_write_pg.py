"""S1 事件双写:进度与终态迁移同事务写 canonical ``job_events`` 影子行。

判据不是「新表里有行」,而是两侧能被**对账**:

* canonical 侧的 ``event_seq`` 是从 1 起的密序号 —— 它不是草案的 ``MAX+1``,而是
  ``coordination_jobs.last_event_seq + 1``。``_mark_running`` 只写草案事件(它不持有
  ``coordination_jobs`` 行锁),所以两条序列在同一个 Job 上必然不同;本文件把这件事断言成
  事实,而不是绕开它。
* 每一行的 ``event_digest`` 必须等于用 ``claims.event_digest()`` 按**canonical 序号**对
  草案行的字段(`state` / `occurred_at_us` / `kind` / `progress_*`)重算的值。摘要覆盖
  ``event_seq``,所以拿草案序号算出来的摘要是另一条事实 —— 那样两侧永远无法对账。
* ``payload_ref`` 一律 ``NULL``(搬迁不引用外部负载的产品决定,已并入 0068)。
* 一次失败必须同时回退两侧:把 canonical 表藏起来再跑同一迁移,草案事件与事件头都不许前进。

每一条都配了反向注入(见提交说明与报告):删掉 canonical INSERT、把摘要改按草案序号算、
把 canonical 追加挪到事务之外,本文件都会红。``_reconcile`` 就是那个唯一的对账口径:两侧逐行
比较,不匹配时报出 job 与两侧的 seq。
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

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchUnavailableError,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
)
from speechnexus_portable_sql.claims import event_digest, utc_from_epoch_us

pytestmark = pytest.mark.integration
#: 终态:这两个 state 由 ``_finish`` 写入,也是本切片覆盖的第二类事件。
TERMINAL_STATES = ("succeeded", "failed")
#: ``speech_jobs`` 的 CHECK 要求 succeeded 必须带结果文档;本文件不关心内容,只要求它是
#: 合法 JSON —— 双写判据与结果形状无关。
RESULT_JSON = json.dumps({"object": "nexus.job_result", "synthetic": True})


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
    """本切片负责双写的草案行:进度事实与终态事实,不含 ``_mark_running`` 的 started 行。"""

    return [row for row in draft if row["kind"] == "progress" or row["state"] in TERMINAL_STATES]


def _reconcile(db: JobSandbox, job_id: str) -> list[dict[str, Any]]:
    """两侧逐行对账;任何不一致都以 job 与两侧 seq 报错,不返回布尔值。

    口径:canonical 行按 ``event_seq`` 升序,与草案侧被覆盖行(进度 + 终态)按
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


async def test_progress_and_finish_append_dense_canonical_events(jobs_db: JobSandbox) -> None:
    states, claim = await _running_claim(jobs_db, "synthetic-canonical-dense")
    job_id = claim.job.job_id

    # started 只写草案:canonical 侧此刻仍是空的,这是 S1 的既定形状(见模块 docstring)。
    assert _canonical_events(jobs_db, job_id) == []
    draft_started = _draft_events(jobs_db, job_id)
    assert [row["state"] for row in draft_started] == ["pending", "running"]

    assert await states.record_progress(claim, unit="audio_ms", completed=10, total=None)
    assert await states.record_progress(claim, unit="audio_ms", completed=20, total=None)
    await states.finish(claim, "succeeded", result_json=RESULT_JSON, error_code=None)

    canonical = _reconcile(jobs_db, job_id)
    assert [int(row["event_seq"]) for row in canonical] == [1, 2, 3]
    assert all(row["payload_ref"] is None for row in canonical)
    # 事件头与 canonical 的 MAX 一致:否则下一次双写会重号,主键以 23505 失败。
    assert _head(jobs_db, job_id) == 3

    # 草案序号仍是它自己的序列(0..4),与 canonical 的 1..3 不同 —— 这就是「摘要按哪条
    # 序号算」必须写死的原因。
    draft = _draft_events(jobs_db, job_id)
    assert [int(row["event_seq"]) for row in draft] == [0, 1, 2, 3, 4]
    assert [row["state"] for row in draft] == [
        "pending",
        "running",
        "running",
        "running",
        "succeeded",
    ]


async def test_reconciliation_matches_both_stores_row_by_row(jobs_db: JobSandbox) -> None:
    """对账用例:同一 Job 读两侧、逐行比;不一致时报 job 与 seq(见 ``_reconcile``)。"""

    states, claim = await _running_claim(jobs_db, "synthetic-canonical-reconcile")
    job_id = claim.job.job_id
    assert await states.record_progress(claim, unit="audio_ms", completed=5, total=None)
    await states.finish(claim, "failed", result_json=None, error_code="engine_failure")

    canonical = _reconcile(jobs_db, job_id)
    assert len(canonical) == 2
    assert [int(row["event_seq"]) for row in canonical] == [1, 2]
    assert _head(jobs_db, job_id) == 2


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
    assert before_head == 1

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
    assert [int(row["event_seq"]) for row in _canonical_events(jobs_db, job_id)] == [1]

    # 恢复之后同一事务仍然成立:失败没有留下半成品,下一次迁移正常推进到 2。
    assert await states.record_progress(claim, unit="audio_ms", completed=20, total=None)
    assert _head(jobs_db, job_id) == 2
    assert [int(row["event_seq"]) for row in _reconcile(jobs_db, job_id)] == [1, 2]


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

    # 三处都不许前进:公开 Job、草案终态事件、canonical 事件头。
    assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
        {"state": "running"}
    ]
    assert jobs_db.query(
        "SELECT job_state FROM coordination_jobs WHERE job_id = %s", (job_id,)
    ) == [{"job_state": "claimed"}]
    assert _draft_events(jobs_db, job_id) == before_draft
    assert _head(jobs_db, job_id) == 1
    assert [int(row["event_seq"]) for row in _reconcile(jobs_db, job_id)] == [1]

    # 恢复后终态迁移把两侧一起推进。
    finished = await states.finish(claim, "succeeded", result_json=RESULT_JSON, error_code=None)
    assert finished.state == "succeeded"
    assert [int(row["event_seq"]) for row in _reconcile(jobs_db, job_id)] == [1, 2]
    assert _head(jobs_db, job_id) == 2


def test_the_canonical_sequence_is_not_the_draft_sequence() -> None:
    """两条序号由不同来源决定;摘要只按 canonical 的那一条算(静态判据)。

    这条断言没有数据库也能跑:它钉住「同一 (job, seq) 的摘要不同」这件事,防止有人把
    ``_append_canonical_event`` 改成复用草案序号而两侧看起来仍然「有行」。
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
