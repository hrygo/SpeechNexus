"""草稿行锁强度的并发判据:同一行只按一个强度锁,且等待图无环.

`public_job_admission` 现在两处都用 `SELECT ... FOR UPDATE` 锁 `speech_voice_drafts` 的一行,
与 `_lock_validation_target` / `_lock_preview_target` / 删除执行器同一强度。本文件是这笔
换算在真实 PostgreSQL 上的判据:

1. 两个并发 create 钉同一份草稿:都不死锁、都终止、都准入;同一时刻另一条只读共享请求
   必须被这条行锁挡住 —— 这是「强度是 FOR UPDATE」的行为判据(`FOR SHARE` 时那条请求
   直接通过,测试会红)。
2. 一个 create 钉草稿 A、另一笔事务先持草稿 B 再要 A:等待图只有 B 持有者 -> A 持有者
   一条边,A 的持有者不等待任何东西(无环),两笔都终止。
3. 正控:两笔事务各持一份草稿再互相索要,`deadlock_timeout` 窗口内服务端确实报
   `DeadlockDetected` —— 前两条的「不死锁」不是看不见死锁的假绿。

证据取真实等待边(`pg_stat_activity` + `pg_blocking_pids`),并用事务内读到的
`current_setting('deadlock_timeout')` 说明检测窗口真的是本测试设的那个值。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_public_job_atomic_admission_pg import (
    BINDING,
    VALIDATION_DRAFT,
    WORKSPACE,
    _seed,
    _seed_validation_candidate,
)
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.domain.public_jobs import (
    JobSubmission,
    NewPublicJob,
    canonical_digest,
    idempotency_digest,
)
from speechnexus_backend.speech.infrastructure import public_jobs_postgres
from speechnexus_backend.speech.infrastructure.admission_sql import admit_in_transaction
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

#: 第二份草稿:只用来让另一笔事务持有一行,不参与准入。
SECOND_DRAFT = "draft-draft-lock-second"
SECOND_JOB = "job-draft-lock-second-gen"
#: 等待侧的检测窗口。比默认 1s 短,让「有环」在测试时限内可见而不是靠 join 超时。
DEADLOCK_TIMEOUT = "250ms"
#: create 的事务时限。默认 3s 会把「被测试故意停住的 create」当成超预算失败。
CREATE_TIMEOUT_SECONDS = 30


def _draft_job(suffix: str) -> NewPublicJob:
    """一份 voice_validation create:不带 expected_binding,走未钉住的创建路径。

    未钉住时 `_lock_validation_target` 不运行,草稿行只由 `build_public_job_admission`
    的锁语句拿到 —— 正是这次换算的那一处。
    """
    job_id = "job-draft-lock-" + suffix
    intent = {
        "workspace_id": WORKSPACE,
        "kind": "voice_validation",
        "model": BINDING,
        "input": {
            "voice_draft_id": VALIDATION_DRAFT,
            "draft_revision": "1",
            "validation_set_revision": "1",
        },
        "output": {"transcript_format": "json", "save_artifacts": True},
        "constraints": {"allow_degradation": False, "require_exact_parameters": False},
    }
    return NewPublicJob(
        job_id=job_id,
        workspace_id=WORKSPACE,
        kind="voice_validation",
        model_id=BINDING,
        request_digest=canonical_digest(intent),
        request_document=json.dumps(intent),
        initial_event_digest=canonical_digest({"job_id": job_id, "event_seq": 0}),
        idempotency_key_digest=idempotency_digest("draft-lock-" + suffix),
        submission=JobSubmission(
            request_id="request-" + job_id,
            client_reference=None,
            effective_configuration_json=json.dumps(
                {"models": {"voice": {"id": BINDING, "revision": "1"}}}
            ),
            execution_revision=1,
            progress_unit="audio_ms",
            progress_total=1000,
            submitted_at_us=1_800_000_000_000_000,
        ),
    )


def _long_store(admission: AdmissionDatabase) -> PostgresPublicJobs:
    pool = admission.runtime.pool
    assert pool is not None
    assert admission.admission_pool is not None
    return PostgresPublicJobs(
        pool,
        admission_pool=admission.admission_pool,
        require_admission=True,
        timeout_seconds=CREATE_TIMEOUT_SECONDS,
        storage_bucket="transcription-inputs",
    )


def _seed_second_draft(db: Any) -> None:
    """A 之外的第二份草稿,连同它的 generation Job 与修订行。"""
    db.query(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, result_json, created_at_us, updated_at_us) "
        "VALUES (%s, %s, 'tts', %s, %s, 'succeeded', 2, '{}', 1, 1)",
        (SECOND_JOB, WORKSPACE, BINDING, "sha256:" + "c" * 64),
    )
    db.query(
        "INSERT INTO speech_voice_drafts (draft_id, generation_job_id, kind, status, "
        "head_revision, metadata_revision, display_name, input_json, binding_json, "
        "created_at_us, updated_at_us) "
        "VALUES (%s, %s, 'clone', 'candidate', '1', 1, 'Second draft', %s, %s, 1, 1)",
        (SECOND_DRAFT, SECOND_JOB, "{}", '{"id":"' + BINDING + '","revision":"1"}'),
    )
    db.query(
        "INSERT INTO speech_voice_draft_revisions (draft_id, revision, kind, "
        "generation_job_id, input_json, binding_json, created_at_us) "
        "VALUES (%s, '1', 'clone', %s, '{}', %s, 1)",
        (SECOND_DRAFT, SECOND_JOB, '{"id":"' + BINDING + '","revision":"1"}'),
    )


def _widen_capacity(db: Any) -> None:
    """两份草稿的准入各占一份尝试与一个槽位,判据要比的是锁不是容量。"""
    db.query(
        "UPDATE workspace_quotas SET max_attempts = 8 WHERE workspace_id = %s",
        (WORKSPACE,),
    )
    db.query("UPDATE engine_instances SET max_slots = 4")
    # unified_memory 架构不能再把同一块内存记成 host/device RAM,只放宽其余维度。
    db.query(
        "UPDATE host_nodes SET allocatable_unified_physical_memory_bytes = 1000000, "
        "allocatable_cpu_decode_millicores = 1000000, "
        "allocatable_host_pcm_buffer_bytes = 1000000, "
        "allocatable_scratch_disk_bytes = 1000000"
    )


def _tighten_deadlock_detection(db: Any, role: str) -> None:
    db.query(f"ALTER ROLE \"{role}\" SET deadlock_timeout = '{DEADLOCK_TIMEOUT}'")


def _reset_deadlock_detection(db: Any, role: str) -> None:
    db.query(f'ALTER ROLE "{role}" RESET deadlock_timeout')


def _waits(db: Any, *fragments: str) -> list[dict[str, Any]]:
    """当前被 `fragments` 全部命中的等待后端,含它们的阻塞者(真实等待边)。"""
    predicates = " AND ".join("strpos(query, %s) > 0" for _ in fragments)
    rows: list[dict[str, Any]] = db.query(
        "SELECT pid, wait_event_type, wait_event, state, query, "
        "pg_blocking_pids(pid) AS blockers "
        "FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND " + predicates,
        fragments,
    )
    return rows


def _await_waits(db: Any, *fragments: str) -> dict[str, Any] | None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = _waits(db, *fragments)
        if rows:
            return rows[0]
        time.sleep(0.02)
    return None


def _blockers_of(db: Any, pid: int) -> list[int]:
    rows = db.query("SELECT pg_blocking_pids(%s) AS blockers", (pid,))
    return list(rows[0]["blockers"])


class _AdmissionPause:
    """把第一笔 create 停在准入写入处 —— 此时它的草稿行锁已拿到且未释放。

    `admit_in_transaction` 是 `build_public_job_admission` 之后的第一步写,所以停在这里的
    事务正持有 `SELECT ... FOR UPDATE` 的草稿行锁。第二笔请求的等待因此可观测,不必靠
    sleep 猜时序。
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.deadlock_timeout: str | None = None
        self._calls = 0
        self._guard = threading.Lock()

    def __call__(self, cursor: Any, prepared: Any, **kwargs: Any) -> Any:
        with self._guard:
            self._calls += 1
            first = self._calls == 1
        if first:
            cursor.execute("SELECT current_setting('deadlock_timeout') AS value")
            rows = cursor.fetchall()
            self.deadlock_timeout = rows[0]["value"] if rows else None
            self.entered.set()
            assert self.release.wait(20), "the paused create was never released"
        return admit_in_transaction(cursor, prepared, **kwargs)


def _create_in_thread(store: Any, job: NewPublicJob) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def run() -> None:
        result["result"] = store.create(job)

    thread = threading.Thread(target=run, name="create-" + job.job_id)
    thread.start()
    return thread, result


def _probe_connection(url: str, schema: str) -> Any:
    connection = portable_pg_connect(url, row_factory=dict_row)
    connection.execute(f'SET search_path TO "{schema}", pg_catalog')
    connection.execute(f"SET deadlock_timeout = '{DEADLOCK_TIMEOUT}'")
    return connection


@pytest.fixture
def tightened_roles(admission_database: AdmissionDatabase) -> Any:
    """两条相关角色新建连接时的 deadlock_timeout,判据结束即还原。"""
    db = admission_database.runtime.foundation.database
    role = admission_database.runtime.foundation.speech_role
    assert admission_database.admission_role is not None
    _tighten_deadlock_detection(db, role)
    _tighten_deadlock_detection(db, admission_database.admission_role)
    try:
        yield
    finally:
        _reset_deadlock_detection(db, role)
        _reset_deadlock_detection(db, admission_database.admission_role)


def test_two_creates_on_one_draft_serialize_under_for_update_and_both_admit(
    admission_database: AdmissionDatabase,
    tightened_roles: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一份草稿上的两笔 create:行锁是 FOR UPDATE,两笔都终止且都准入。

    第一笔停在准入写入处时,另一条只读 `FOR SHARE` 请求必须被挡住:这只有在持有者是
    排他强度(`FOR UPDATE` / `FOR NO KEY UPDATE`)时成立,`FOR SHARE` 持有者会直接放行。
    这条请求在被观察到之后取消,免得它的共享锁替第二笔 create 挡路。第二笔 create 随后
    加入等待(同一 binding 的既有 advisory 锁),阻塞者仍是第一笔的后端,而第一笔不等待
    任何人 —— 无环。释放后两笔都 commit,`speech_jobs` 多两行。
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _seed_validation_candidate(db)
    _widen_capacity(db)
    pause = _AdmissionPause()
    monkeypatch.setattr(public_jobs_postgres, "admit_in_transaction", pause)
    store = _long_store(admission_database)
    before = int(db.query("SELECT count(*) AS n FROM speech_jobs")[0]["n"])
    probe = _probe_connection(db.url, db.schema)
    probe_errors: list[BaseException] = []
    probe_done = threading.Event()

    def run_probe() -> None:
        try:
            probe.execute(
                "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR SHARE",
                (VALIDATION_DRAFT,),
            ).fetchall()
        except BaseException as error:
            probe_errors.append(error)
        finally:
            probe_done.set()

    probe_thread = threading.Thread(target=run_probe, name="share-probe")
    first, first_result = _create_in_thread(store, _draft_job("first"))
    second: threading.Thread | None = None
    try:
        assert pause.entered.wait(20), "the first create never reached its admission writes"
        # 这条 create 会话里真正生效的检测窗口,不是推理。
        assert pause.deadlock_timeout == DEADLOCK_TIMEOUT, pause.deadlock_timeout

        probe_thread.start()
        probe_wait = _await_waits(db, "speech_voice_drafts", "FOR SHARE")
        assert probe_wait is not None, (
            "a FOR SHARE request was not blocked by the create's draft row: it is not FOR UPDATE"
        )
        assert probe_wait["blockers"], probe_wait
        holder = probe_wait["blockers"][0]
        # 无环:持有者不等待任何人。
        assert _blockers_of(db, holder) == []
        # 观察到边之后取消,别让这条共享请求的锁替第二笔 create 挡路。
        probe.cancel()
        assert probe_done.wait(20), "the cancelled probe never returned"
        assert len(probe_errors) == 1 and isinstance(probe_errors[0], psycopg.errors.QueryCanceled)

        second, second_result = _create_in_thread(store, _draft_job("second"))
        create_wait = _await_waits(db, "pg_advisory_xact_lock")
        assert create_wait is not None, "the second create never waited for the first"
        assert holder in create_wait["blockers"], (create_wait, holder)
        pause.release.set()
        first.join(CREATE_TIMEOUT_SECONDS)
        second.join(CREATE_TIMEOUT_SECONDS)
        assert not first.is_alive() and not second.is_alive()
        assert first_result["result"].outcome == "created", first_result["result"].error_code
        assert second_result["result"].outcome == "created", (
            second_result["result"].error_code,
            second_result["result"].detail,
        )
        after = int(db.query("SELECT count(*) AS n FROM speech_jobs")[0]["n"])
        assert after == before + 2
    finally:
        pause.release.set()
        for thread in (first, second, probe_thread):
            if thread is not None:
                thread.join(CREATE_TIMEOUT_SECONDS)
        probe.rollback()
        probe.close()


def test_a_create_on_one_draft_and_a_holder_of_another_draft_do_not_cycle(
    admission_database: AdmissionDatabase,
    tightened_roles: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持 B 的事务再要 A:等待图只有一条边,A 的持有者不等待。

    这是「换锁强度会不会造出原本不存在的环」的形状问题:一笔 create 只锁它自己那一行
    草稿;另一笔事务(删除/发布者的位置)先持 B 再索要 A。只要 A 的持有者不回头要 B,
    等待图就无环 —— 上报的是 `pg_blocking_pids` 取到的边,不是「应该不会」。
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _seed_validation_candidate(db)
    _seed_second_draft(db)
    _widen_capacity(db)
    pause = _AdmissionPause()
    monkeypatch.setattr(public_jobs_postgres, "admit_in_transaction", pause)
    store = _long_store(admission_database)
    holder = _probe_connection(db.url, db.schema)
    checker = _probe_connection(db.url, db.schema)
    ask_errors: list[BaseException] = []

    def ask_a() -> None:
        try:
            holder.execute(
                "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR UPDATE",
                (VALIDATION_DRAFT,),
            ).fetchall()
        except BaseException as error:
            ask_errors.append(error)

    first, first_result = _create_in_thread(store, _draft_job("cross"))
    ask_thread: threading.Thread | None = None
    try:
        assert pause.entered.wait(20), "the create never reached its admission writes"
        # 先持有 B:用第三方 NOWAIT 请求证明 B 真的被这笔事务持有。
        holder.execute(
            "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR UPDATE",
            (SECOND_DRAFT,),
        ).fetchall()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            checker.execute(
                "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR UPDATE NOWAIT",
                (SECOND_DRAFT,),
            ).fetchall()
        checker.rollback()
        ask_thread = threading.Thread(target=ask_a, name="ask-draft-a")
        ask_thread.start()
        wait = _await_waits(db, "speech_voice_drafts", "FOR UPDATE")
        assert wait is not None, "the cross-draft request never waited on draft A"
        create_pid = wait["blockers"][0]
        # create 的后端在等什么?什么都不等 —— 环需要它回头索要 B。
        assert _blockers_of(db, create_pid) == []
        pause.release.set()
        first.join(CREATE_TIMEOUT_SECONDS)
        ask_thread.join(CREATE_TIMEOUT_SECONDS)
        assert not first.is_alive() and not ask_thread.is_alive()
        assert not ask_errors, ask_errors
        assert first_result["result"].outcome == "created", first_result["result"].error_code
        holder.commit()
    finally:
        pause.release.set()
        for thread in (first, ask_thread):
            if thread is not None:
                thread.join(CREATE_TIMEOUT_SECONDS)
        holder.rollback()
        holder.close()
        checker.rollback()
        checker.close()


def test_a_real_two_draft_cycle_is_detected_within_the_configured_timeout(
    admission_database: AdmissionDatabase,
) -> None:
    """正控:两笔事务各持一份草稿再互要,服务端真的报死锁。

    「不死锁」只有在能看见死锁时才算判据。这里刻意造出产品路径不允许的环,要求它在
    `deadlock_timeout` 窗口内被服务端检出(而不是挂到 join 超时),并且恰好死掉一侧。
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _seed_validation_candidate(db)
    _seed_second_draft(db)
    left = _probe_connection(db.url, db.schema)
    right = _probe_connection(db.url, db.schema)
    errors: list[BaseException] = []

    def cross(connection: Any) -> None:
        try:
            connection.execute(
                "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR UPDATE",
                (SECOND_DRAFT,),
            ).fetchall()
        except BaseException as error:
            errors.append(error)

    started = time.monotonic()
    try:
        left.execute(
            "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR UPDATE",
            (VALIDATION_DRAFT,),
        ).fetchall()
        right.execute(
            "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR UPDATE",
            (SECOND_DRAFT,),
        ).fetchall()
        crossed = threading.Thread(target=cross, args=(left,), name="cycle-left")
        crossed.start()
        wait = _await_waits(db, "speech_voice_drafts", "FOR UPDATE")
        assert wait is not None, "the first half of the cycle never waited"
        right_error: BaseException | None = None
        try:
            right.execute(
                "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR UPDATE",
                (VALIDATION_DRAFT,),
            ).fetchall()
        except psycopg.errors.DeadlockDetected as error:
            right_error = error
        crossed.join(10)
        assert not crossed.is_alive()
        victims = [
            error
            for error in (*errors, right_error)
            if isinstance(error, psycopg.errors.DeadlockDetected)
        ]
        # 服务端只死掉一侧;另一侧的索取成功。恰好一个 DeadlockDetected 才说明
        # 「没死锁」的断言不是看不见死锁。
        assert len(victims) == 1, (right_error, errors)
        assert time.monotonic() - started < 10
    finally:
        left.rollback()
        left.close()
        right.rollback()
        right.close()
