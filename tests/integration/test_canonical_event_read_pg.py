"""S2 事件读翻转:durable 事件流从 canonical ``job_events`` 取事实集。

判据不是「新表里有行」,而是三件事同时成立:

1. **同一组事实。** 一次完整生命周期(started → 进度 → 终态)之后,流式读者给出的有序
   ``(event_seq, digest)`` 事实与「按草案行字段、用 canonical 序号重算」的摘要逐条相等 ——
   也就是翻转前后是同一批事实,只是这次从 canonical 读出来。
2. **canonical 决定成员。** 删掉一条 canonical 行之后,流里对应的那一帧不再出现;草案多出的
   行不再被发出。这一条把「读了 canonical」与「只是多读了一次、仍然按草案发」区分开。
3. **读面完整。** 真实身份是**非属主**角色(``jobs_db`` 的角色,只拿到
   ``job_runtime_grants`` + ``job_dispatch_grants``)。列级 ``SELECT`` 少了 ``job_id`` 时,
   ``claims.STATEMENTS["job_event.read"]`` 以 ``42501`` 失败 —— S1b 实测到的那个缺口。

``Last-Event-ID`` 的口径也在这里钉住:线路 ``id`` 是 ``event_seq + 1``,所以 ``0`` 与 ``-1``
不是合法游标(HTTP 边界以 ``400 invalid_job_request`` 拒绝),而没有游标 / ``id`` 1 的客户端
看到的帧集合与翻转前完全一致 —— 创建事实(``event_seq`` 0)按设计没有 canonical 行,仍由草案
提供,所以它不会因为这次翻转消失。
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_public_jobs_http import PREFIX, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.infrastructure.canonical_event_read import read_job_events
from speechnexus_backend.speech.infrastructure.canonical_event_schema import (
    JOB_EVENT_PRIVILEGES,
    JOB_EVENT_READ_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
)
from speechnexus_portable_sql.claims import event_digest, utc_from_epoch_us

pytestmark = pytest.mark.integration
EVENTS = PREFIX + "/{job_id}/events"


def _app(db: JobSandbox) -> Any:
    from tests.integration.test_public_jobs_http import _settings_for

    return create_app(settings=_settings_for(db), jobs=db.jobs)


def _body(reference: str) -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic canonical read"},
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


async def _finished_job(db: JobSandbox, reference: str) -> str:
    """一次完整生命周期:started → 两条进度 → succeeded,全部经真实状态机写入。"""

    job_id = await _submitted(db, reference)
    queue = PostgresDispatchQueue(db.pool)
    states = PostgresDispatchStateStore(db.pool)
    claims = await queue.claim_due("worker-canonical-read", 30_000, 1)
    assert [claim.job.job_id for claim in claims] == [job_id]
    running = await states.mark_running(claims[0].job)
    assert running.state == "running"
    claim = replace(claims[0], job=running)
    assert await states.record_progress(claim, unit="audio_ms", completed=10, total=None)
    assert await states.record_progress(claim, unit="audio_ms", completed=20, total=None)
    # 终态取 ``failed``:``succeeded`` 的帧还要求一条 ready Artifact,而本文件判的是事件事实。
    finished = await states.finish(claim, "failed", result_json=None, error_code="engine_failure")
    assert finished.state == "failed"
    return job_id


def _frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        identifier: str | None = None
        data: str | None = None
        for line in block.splitlines():
            if line.startswith("id: "):
                identifier = line[4:]
            elif line.startswith("data: "):
                data = line[6:]
        if identifier is not None and data is not None:
            frames.append({"id": identifier, "event": json.loads(data)})
    return frames


async def _stream(db: JobSandbox, job_id: str, cursor: str | None = None) -> list[dict[str, Any]]:
    app = _app(db)
    headers = {} if cursor is None else {"Last-Event-ID": cursor}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(EVENTS.format(job_id=job_id), headers=headers)
    assert response.status_code == 200, response.text
    return _frames(response.text)


def _draft_facts(db: JobSandbox, job_id: str) -> list[tuple[int, str]]:
    """草案行按 canonical 规则重算的 ``(event_seq, digest)``,跳过创建事实。

    canonical 序号与草案序号在 ``event_seq >= 1`` 上逐条相等(草案多出的只有创建事实 0),
    所以这里的重算就是「翻转前后同一批事实」的定义。
    """

    rows = db.query(
        "SELECT event_seq, state, occurred_at_us, kind, progress_completed, progress_total "
        "FROM speech_job_events WHERE job_id = %s ORDER BY event_seq",
        (job_id,),
    )
    facts: list[tuple[int, str]] = []
    for row in rows:
        seq = int(row["event_seq"])
        if seq < 1:
            continue
        moment = utc_from_epoch_us(int(row["occurred_at_us"]), field="occurred_at_us")
        assert moment is not None
        facts.append(
            (
                seq,
                event_digest(
                    job_id=job_id,
                    event_seq=Decimal(seq),
                    state=str(row["state"]),
                    occurred_at=moment,
                    kind=None if row["kind"] is None else str(row["kind"]),
                    progress_completed=(
                        None
                        if row["progress_completed"] is None
                        else int(row["progress_completed"])
                    ),
                    progress_total=(
                        None if row["progress_total"] is None else int(row["progress_total"])
                    ),
                    payload_ref=None,
                ),
            )
        )
    return facts


def _canonical_facts(db: JobSandbox, job_id: str) -> list[tuple[int, str, str | None]]:
    """走产品代码那条读路径(``job_event.read``),身份是**非属主**角色。"""

    with db.pool.connection() as connection:
        facts = read_job_events(connection, job_id=job_id, after_seq=-1)
    return [(fact.event_seq, fact.event_digest, fact.payload_ref) for fact in facts]


async def test_the_stream_serves_the_same_ordered_facts_from_canonical(jobs_db: JobSandbox) -> None:
    """完整生命周期之后,流式读者的有序事实与草案重算逐条相等,且都从 canonical 读出。"""

    job_id = await _finished_job(jobs_db, "synthetic-canonical-read-facts")

    # 1. canonical 侧的事实:序号从 1 起密、摘要等于按草案行字段重算的值。
    canonical = _canonical_facts(jobs_db, job_id)
    assert [seq for seq, _, _ in canonical] == [1, 2, 3, 4]
    assert [(seq, digest) for seq, digest, _ in canonical] == _draft_facts(jobs_db, job_id)

    # 2. 流式读者给出的帧:创建事实(id 1)来自草案,其余来自 canonical,顺序不变。
    frames = await _stream(jobs_db, job_id)
    assert [frame["id"] for frame in frames] == ["1", "2", "3", "4", "5"]
    assert [frame["event"]["type"] for frame in frames] == [
        "job.queued",
        "job.started",
        "job.progress",
        "job.progress",
        "job.failed",
    ]
    # 线路 id 与持久序号只差一:canonical 序号 1..4 就是 id 2..5。
    assert [int(frame["id"]) - 1 for frame in frames] == [0, 1, 2, 3, 4]


async def test_canonical_decides_which_facts_the_stream_emits(jobs_db: JobSandbox) -> None:
    """删掉一条 canonical 行:流里对应的帧消失。canonical 是成员判定,不是旁路校验。"""

    job_id = await _finished_job(jobs_db, "synthetic-canonical-membership")
    assert [frame["id"] for frame in await _stream(jobs_db, job_id)] == ["1", "2", "3", "4", "5"]

    jobs_db.admin.execute("DELETE FROM job_events WHERE job_id = %s AND event_seq = 2", (job_id,))
    try:
        # 草案仍有一条 event_seq 2 的进度行;翻转之后它不再被发出,因为 canonical 没有这条事实。
        assert jobs_db.query(
            "SELECT count(*) AS n FROM speech_job_events WHERE job_id = %s AND event_seq = 2",
            (job_id,),
        ) == [{"n": 1}]
        assert [frame["id"] for frame in await _stream(jobs_db, job_id)] == ["1", "2", "4", "5"]
    finally:
        # 恢复:同一行按原摘要写回,后续断言(与夹具的 DROP SCHEMA)不受影响。
        digest = dict(_draft_facts(jobs_db, job_id))[2]
        jobs_db.admin.execute(
            "INSERT INTO job_events (job_id, event_seq, event_digest, payload_ref) "
            "VALUES (%s, 2, %s, NULL)",
            (job_id, digest),
        )
    assert [frame["id"] for frame in await _stream(jobs_db, job_id)] == ["1", "2", "3", "4", "5"]


async def test_a_draft_era_cursor_resumes_without_duplicating_or_losing_a_fact(
    jobs_db: JobSandbox,
) -> None:
    """``Last-Event-ID`` 的口径:``id`` 1 之后从 2 继续;``0``/``-1`` 不是合法游标。

    线路 ``id`` 是 ``event_seq + 1``,所以最小合法值是 1,翻转没有移动这个编码。创建事实
    (``event_seq`` 0)仍由草案提供,所以「存了 1 的客户端」续读时既不重复也不丢帧。
    """

    job_id = await _finished_job(jobs_db, "synthetic-canonical-resume")

    fresh = await _stream(jobs_db, job_id)
    assert [frame["id"] for frame in fresh] == ["1", "2", "3", "4", "5"]

    # 存了 1(创建事实)的客户端:从 2 继续,不重复 1、不跳过 2。
    resumed = await _stream(jobs_db, job_id, cursor="1")
    assert [frame["id"] for frame in resumed] == ["2", "3", "4", "5"]
    # 续读的帧内容与完整流逐字节相同 —— 不重算、不重发同值。
    assert resumed == fresh[1:]

    # 存了中间某个 id 的客户端:从它之后继续。
    middle = await _stream(jobs_db, job_id, cursor="3")
    assert [frame["id"] for frame in middle] == ["4", "5"]

    # 已经消费到终态的客户端:再连一次得到空流(终态不重放)。
    assert await _stream(jobs_db, job_id, cursor="5") == []

    # ``0`` 与 ``-1`` 不是合法游标:线路序号从 1 起,二者都停在 HTTP 边界之外。
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for header in ("0", "-1"):
            rejected = await client.get(
                EVENTS.format(job_id=job_id), headers={"Last-Event-ID": header}
            )
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()["error"]["code"] == "invalid_job_request"
        # 没有游标 = 从最早事件读起,与「存了 0」的意图等价,并且覆盖全部帧。
        assert _frames((await client.get(EVENTS.format(job_id=job_id))).text) == fresh


async def test_the_dispatch_identity_may_run_the_read_statement(jobs_db: JobSandbox) -> None:
    """42501 回归:非属主身份在 head 上跑 ``job_event.read`` 必须成功。

    这条用例的注入是「把 ``job_id`` 从列级 ``SELECT`` 里去掉」:同一条语句随即以
    ``42501 permission denied for table job_events`` 失败(报告里引了实测输出)。所以它同时
    是「读面完整」的判据与「补授与读翻转必须同一提交」的判据。
    """

    job_id = await _finished_job(jobs_db, "synthetic-canonical-privilege")
    with jobs_db.pool.connection() as connection:
        identity = connection.execute("SELECT current_user AS role").fetchone()
        assert identity is not None
        # 真实身份不是属主:属主会绕过列级授权,那样这条判据什么都不检查。
        assert identity["role"] == jobs_db.role
        owner = connection.execute(
            "SELECT pg_catalog.pg_has_role(current_user, "
            "(SELECT relowner FROM pg_catalog.pg_class WHERE relname = 'job_events'), "
            "'MEMBER') AS owner"
        ).fetchone()
        assert owner is not None and owner["owner"] is False

        # 声明面里的每一列都可读,其余列都不可读:授予面就是读面,不是表级 SELECT。
        rows = connection.execute(
            "SELECT column_name, pg_catalog.has_column_privilege(current_user, "
            "'job_events', column_name, 'SELECT') AS readable "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'job_events'"
        ).fetchall()
        readable = {row["column_name"] for row in rows if row["readable"]}
        assert readable == set(JOB_EVENT_READ_COLUMNS)
        assert JOB_EVENT_PRIVILEGES["SELECT"] == JOB_EVENT_READ_COLUMNS

        # 语句本身:派发身份原样执行,不 42501,且返回 canonical 事实。
        facts = read_job_events(connection, job_id=job_id, after_seq=0)
        assert [fact.event_seq for fact in facts] == [1, 2, 3, 4]


async def test_no_reader_exposes_a_payload_reference(jobs_db: JobSandbox) -> None:
    """``payload_ref`` 按产品决定恒为 NULL:canonical 行没有引用,线路也没有这个字段。"""

    job_id = await _finished_job(jobs_db, "synthetic-canonical-payload")
    canonical = _canonical_facts(jobs_db, job_id)
    assert canonical and all(payload is None for _, _, payload in canonical)
    frames = await _stream(jobs_db, job_id)
    for frame in frames:
        assert "payload_ref" not in json.dumps(frame["event"])
