"""Binding disable persists its original intent atomically with lifecycle facts."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from tests.integration.test_binding_lifecycle_pg import Sandbox, _body, _seed
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.integration.test_coordination_foundation import seed
from tests.integration.test_production_jobs import JobDatabase

from speechnexus_backend.binding_disable_worker import _verify_principal
from speechnexus_backend.speech.application.binding_disable_coordinator import (
    BindingDisableCoordinator,
)
from speechnexus_backend.speech.application.public_jobs import (
    IdempotencyConflictError,
    JobRejectedError,
)
from speechnexus_backend.speech.domain.binding_disable_intent import DisableExecutionTarget
from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingLifecycleUnavailableError,
    BindingRetirementCommand,
)
from speechnexus_backend.speech.infrastructure import public_jobs_postgres
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import (
    lock_binding,
    revision_is_disabled,
)
from speechnexus_backend.speech.infrastructure.binding_disable_coordinator_postgres import (
    PostgresDisableCoordinatorStore,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_postgres import (
    capture_disable_targets,
    list_disable_event_ids,
    list_disable_targets,
    read_disable_target,
    record_abort_intent,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants,
)
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
    _Cursor,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

pytestmark = pytest.mark.integration


def _command() -> BindingRetirementCommand:
    return BindingRetirementCommand(
        binding_id="tts-quality",
        revision="2",
        request_id="synthetic-disable-request",
        actor_type="anonymous",
        credential_label=None,
        key_digest="sha256:" + "a" * 64,
        intent_digest="sha256:" + "b" * 64,
        mode="disable",
        reason="synthetic emergency",
    )


def test_disable_records_original_intent_once_with_its_lifecycle_event(
    sandbox: Sandbox,
) -> None:
    _seed(sandbox)
    command = _command()
    first = sandbox.lifecycle.retire(command)
    replay = sandbox.lifecycle.retire(command)
    assert first.status == "disabled"
    assert replay.replayed
    assert first.revision_id == replay.revision_id == "binding-rev-2"
    rows = sandbox.admin.execute(
        "SELECT e.binding_id, e.revision, e.revision_id, e.action, e.disabled_reason, "
        "e.intent_digest, e.idempotency_key_digest FROM speech_binding_disable_intents AS i "
        "JOIN speech_publication_lifecycle_events AS e ON e.event_id = i.event_id"
    ).fetchall()
    assert rows == [
        (
            command.binding_id,
            command.revision,
            "binding-rev-2",
            "model_binding.disable",
            command.reason,
            command.intent_digest,
            command.key_digest,
        )
    ]


def test_intent_write_failure_rolls_back_disable_and_head(
    sandbox: Sandbox,
) -> None:
    _seed(sandbox)
    sandbox.admin.execute(f'REVOKE INSERT ON speech_binding_disable_intents FROM "{sandbox.role}"')
    with pytest.raises(BindingLifecycleUnavailableError):
        sandbox.lifecycle.retire(_command())
    assert sandbox.admin.execute(
        "SELECT count(*) FROM speech_binding_disable_intents"
    ).fetchone() == (0,)
    assert sandbox.admin.execute(
        "SELECT count(*) FROM speech_publication_lifecycle_events"
    ).fetchone() == (0,)
    assert sandbox.admin.execute(
        "SELECT revision FROM speech_publication_heads "
        "WHERE kind='binding' AND object_id='tts-quality'"
    ).fetchone() == ("2",)


def test_disable_serializes_with_new_public_job_write(
    sandbox: Sandbox,
) -> None:
    _seed(sandbox)
    acquired, release = threading.Event(), threading.Event()
    holder_pid: list[int] = []

    class PausedDisable(PostgresBindingLifecycle):
        def _lock(self, cursor: _Cursor, binding_id: str) -> None:
            super()._lock(cursor, binding_id)
            holder_pid.append(cursor.raw.connection.info.backend_pid)
            acquired.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test did not release the binding lock")

    lifecycle = PausedDisable(sandbox.pool, timeout_seconds=15)
    with ThreadPoolExecutor(max_workers=2) as workers:
        disabled = workers.submit(lifecycle.retire, _command())
        try:
            assert acquired.wait(timeout=5)
            job = workers.submit(
                sandbox.jobs.submit,
                _body(),
                request_id="synthetic-job-race",
                idempotency_key="synthetic-job-race-key",
            )
            deadline = time.monotonic() + 2
            blocked = False
            while time.monotonic() < deadline:
                row = sandbox.admin.execute(
                    "SELECT pid FROM pg_stat_activity WHERE usename=%s AND pid<>%s "
                    "AND %s=ANY(pg_blocking_pids(pid))",
                    (sandbox.role, holder_pid[0], holder_pid[0]),
                ).fetchone()
                if row is not None:
                    blocked = True
                    break
                time.sleep(0.01)
            assert blocked, "job write did not wait on disable"
        finally:
            release.set()
        assert disabled.result(timeout=10).status == "disabled"
        with pytest.raises(JobRejectedError):
            job.result(timeout=10)
    assert sandbox.admin.execute("SELECT count(*) FROM speech_jobs").fetchone() == (0,)
    assert sandbox.admin.execute(
        "SELECT count(*) FROM speech_binding_disable_intents"
    ).fetchone() == (1,)


def test_original_key_replays_after_disable_but_changed_intent_conflicts(
    sandbox: Sandbox,
) -> None:
    _seed(sandbox)
    key = "synthetic-job-replay-key"
    original = sandbox.jobs.submit(_body(), request_id="first-request", idempotency_key=key)
    sandbox.lifecycle.retire(_command())
    replay = sandbox.jobs.submit(_body(), request_id="retry-request", idempotency_key=key)
    assert replay.job_id == original.job_id
    with pytest.raises(IdempotencyConflictError):
        sandbox.jobs.submit(_body(revision=3), request_id="changed-request", idempotency_key=key)
    assert sandbox.admin.execute("SELECT count(*) FROM speech_jobs").fetchone() == (1,)


@pytest.mark.parametrize("mode", ["retire", "disable"])
def test_barrier_distinguishes_retire_from_exact_revision_disable(
    sandbox: Sandbox, mode: str
) -> None:
    _seed(sandbox)
    command = _command()
    if mode == "retire":
        command = replace(command, mode="retire", reason=None)
    sandbox.lifecycle.retire(command)
    with sandbox.admin.transaction(), sandbox.admin.cursor(row_factory=dict_row) as cursor:
        lock_binding(cursor, "tts-quality")
        assert revision_is_disabled(cursor, "tts-quality", "binding-rev-2") == (mode == "disable")
        assert not revision_is_disabled(cursor, "tts-quality", "binding-rev-1")


def test_targets_commit_and_recover_original_identity_after_restart(sandbox: Sandbox) -> None:
    _seed(sandbox)

    class SeedDatabase:
        def query(self, sql: str, params: tuple[object, ...] = ()) -> Any:
            return sandbox.admin.execute(sql, params)

    seed(cast(JobDatabase, SeedDatabase()))
    sandbox.admin.execute(
        "UPDATE admission_attempts SET binding_revision_id='binding-rev-2' "
        "WHERE operation_id='operation-test'"
    )
    sandbox.lifecycle.retire(_command())
    event_row = sandbox.admin.execute(
        "SELECT event_id FROM speech_binding_disable_intents"
    ).fetchone()
    assert event_row is not None and isinstance(event_row[0], str)
    event_id = event_row[0]
    with sandbox.admin.transaction(), sandbox.admin.cursor(row_factory=dict_row) as cursor:
        captured = capture_disable_targets(cursor, event_id, wait_timeout_ms=1000, limit=10)
        assert record_abort_intent(cursor, event_id, "submit-test", recorded_at_us=1234)
    assert len(captured) == 1
    assert captured[0].submit_intent_id == "submit-test"
    assert captured[0].operation_id == "operation-test"
    assert captured[0].binding_revision_id == "binding-rev-2"
    with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as restarted:
        restarted.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        with restarted.transaction(), restarted.cursor() as cursor:
            assert list_disable_event_ids(cursor, after=None, limit=10) == [event_id]
            assert list_disable_event_ids(cursor, after=event_id, limit=10) == []
            assert list_disable_targets(cursor, event_id, after=None, limit=10) == captured
            assert list_disable_targets(cursor, event_id, after="submit-test", limit=10) == []
            assert read_disable_target(cursor, event_id, "submit-test") == captured[0]
            assert capture_disable_targets(cursor, event_id, wait_timeout_ms=1000, limit=10) == []
            assert not record_abort_intent(cursor, event_id, "submit-test", recorded_at_us=2345)
    assert sandbox.admin.execute(
        "SELECT count(*) FROM speech_binding_disable_targets"
    ).fetchone() == (1,)
    assert sandbox.admin.execute(
        "SELECT recorded_at_us FROM speech_binding_disable_abort_intents"
    ).fetchone() == (1234,)


def test_job_write_first_commits_before_waiting_disable(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(sandbox)
    acquired, release = threading.Event(), threading.Event()
    holder_pid: list[int] = []
    original = public_jobs_postgres._binding_is_still_selected

    def paused(connection: Any, kind: str, model_id: str, document: str) -> bool:
        selected = original(connection, kind, model_id, document)
        holder_pid.append(connection.info.backend_pid)
        acquired.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test did not release the job transaction")
        return selected

    monkeypatch.setattr(public_jobs_postgres, "_binding_is_still_selected", paused)
    with ThreadPoolExecutor(max_workers=2) as workers:
        job = workers.submit(
            sandbox.jobs.submit, _body(), request_id="job-first", idempotency_key="job-first-key"
        )
        try:
            assert acquired.wait(timeout=5)
            disabled = workers.submit(sandbox.lifecycle.retire, _command())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                row = sandbox.admin.execute(
                    "SELECT pid FROM pg_stat_activity WHERE usename=%s AND pid<>%s "
                    "AND %s=ANY(pg_blocking_pids(pid))",
                    (sandbox.role, holder_pid[0], holder_pid[0]),
                ).fetchone()
                if row is not None:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("disable did not wait on the original job transaction")
        finally:
            release.set()
        assert job.result(timeout=10).state == "pending"
        assert disabled.result(timeout=10).status == "disabled"
    assert sandbox.admin.execute("SELECT count(*) FROM speech_jobs").fetchone() == (1,)
    assert sandbox.admin.execute(
        "SELECT count(*) FROM speech_binding_disable_intents"
    ).fetchone() == (1,)


@pytest.mark.anyio
async def test_real_store_coordinator_recovers_using_versioned_abort(sandbox: Sandbox) -> None:
    _seed(sandbox)

    class SeedDatabase:
        def query(self, sql: str, params: tuple[object, ...] = ()) -> Any:
            return sandbox.admin.execute(sql, params)

    seed(cast(JobDatabase, SeedDatabase()))
    sandbox.admin.execute(
        "UPDATE admission_attempts SET binding_revision_id='binding-rev-2' "
        "WHERE operation_id='operation-test'"
    )
    sandbox.lifecycle.retire(_command())
    role = sandbox.role + "_cancel"
    sandbox.admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_cancel_test'")
    pool = speech_pool(
        make_conninfo(sandbox.url, user=role, password="synthetic_cancel_test"),
        schema=sandbox.schema,
        name="test-cancel",
        max_size=2,
    )

    class Gateway:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.queried: list[str] = []

        async def abort_idempotent(self, target: DisableExecutionTarget) -> object:
            self.sent.append(target.submit_intent_id)
            raise TimeoutError("synthetic lost acknowledgement")

        async def query_disable_target(self, target: DisableExecutionTarget) -> object:
            self.queried.append(target.submit_intent_id)
            return object()

    try:
        for statement in disable_coordinator_grants(sandbox.schema, role):
            sandbox.admin.execute(statement)
        pool.open()
        _verify_principal(pool, sandbox.schema)
        gateway = Gateway()
        store = PostgresDisableCoordinatorStore(pool, timeout_seconds=3)
        wrong_node = BindingDisableCoordinator(
            store,
            gateway,
            gateway_id="gateway-other",
            node_id="host-other",
            batch_size=10,
            wait_timeout_ms=1000,
        )
        unrelated = await wrong_node.run_pass()
        assert unrelated.attempted == unrelated.queried == 0
        assert gateway.sent == gateway.queried == []
        coordinator = BindingDisableCoordinator(
            store,
            gateway,
            gateway_id="gateway-test",
            node_id="host-test",
            batch_size=10,
            wait_timeout_ms=1000,
        )
        first = await coordinator.run_pass()
        restarted = BindingDisableCoordinator(
            store,
            gateway,
            gateway_id="gateway-test",
            node_id="host-test",
            batch_size=10,
            wait_timeout_ms=1000,
        )
        second = await restarted.run_pass()
        assert (first.attempted, first.queried, first.unavailable) == (1, 0, 1)
        assert (second.attempted, second.queried, second.unavailable) == (1, 1, 1)
        assert gateway.sent == ["submit-test", "submit-test"]
        assert gateway.queried == ["submit-test"]
        assert sandbox.admin.execute(
            "SELECT count(*) FROM speech_binding_disable_abort_intents"
        ).fetchone() == (1,)
        assert sandbox.admin.execute(
            "SELECT lease_state, cleanup_state FROM admission_leases"
        ).fetchone() == ("reserved", "pending")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as connection:
            connection.execute("SELECT request_document FROM submit_intents")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as connection:
            connection.execute("UPDATE admission_leases SET lease_state='released'")
        sandbox.admin.execute(f'GRANT SELECT (request_document) ON submit_intents TO "{role}"')
        with pytest.raises(ValueError, match="privileges"):
            _verify_principal(pool, sandbox.schema)
    finally:
        pool.close()
        sandbox.admin.execute(f'DROP OWNED BY "{role}"')
        sandbox.admin.execute(f'DROP ROLE "{role}"')
