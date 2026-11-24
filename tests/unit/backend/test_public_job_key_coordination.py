"""Production adapter against a synthetic binding-lock oracle, not PostgreSQL.

The submission key has no lock of its own: its unique constraint decides the
writer race, and the binding lock every identical writer holds until commit is
what a reconciliation read joins before it may report absence.
"""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, Lock
from typing import Any, cast

import psycopg
import pytest

from speechnexus_backend.speech.application.public_jobs import (
    DisabledWorkspaceError,
    IdempotencyConflictError,
    PublicJobs,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.public_jobs import (
    NewPublicJob,
    PublicJobUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from tests.support.keyed_lock_double import business_inserts
from tests.unit.backend.test_public_job_pin_postgres import Pool, Rows, _public_row, _request
from tests.unit.backend.test_public_jobs import _pin_facts, _pinned_body, _service


class BarrierPool:
    def __init__(self, request: NewPublicJob, outcome: str) -> None:
        self.request, self.outcome = request, outcome
        self.rows: list[dict[str, Any]] = []
        self.binding_rows = Pool(request).binding_rows
        self.binding_lock = Lock()
        self.writer_at_insert, self.release_writer, self.waiter_at_lock = Event(), Event(), Event()
        self.inserts = 0

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        transaction = Transaction(self)
        try:
            yield transaction
            if transaction.staged:
                self.rows = [_public_row(self.request)]
                if self.outcome == "lost_reply":
                    # Synthetic commit completed; only the reply is lost.
                    raise psycopg.OperationalError("synthetic commit reply lost")
        finally:
            if transaction.key_held:
                self.binding_lock.release()


class Transaction:
    def __init__(self, pool: BarrierPool) -> None:
        self.pool = pool
        self.key_held = self.staged = False
        self._rows = Rows([])

    def cursor(self) -> Transaction:
        # 键锁自己开游标;替身既是连接也是那条游标。
        return self

    def close(self) -> None:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows.fetchall()

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        self._rows = self._dispatch(query, parameters)
        return self._rows

    def _dispatch(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        pool = self.pool
        if "coordination_keyed_locks" in query and "FOR UPDATE" in query:
            assert not self.key_held, "the writer already holds the binding lock"
            if not pool.binding_lock.acquire(blocking=False):
                pool.waiter_at_lock.set()
                assert pool.binding_lock.acquire(timeout=3), "synthetic binding wait timed out"
            self.key_held = True
            return Rows([{"bucket_no": parameters[0]}])
        if query.startswith("SELECT h.revision"):
            reference = json.loads(pool.request.submission.effective_configuration_json)["models"][
                pool.request.kind
            ]
            return Rows([{"revision": reference["revision"]}])
        if "AS is_current" in query:
            return Rows(pool.binding_rows)
        if "j.idempotency_key_digest = ?" in query:
            # The first lookup is deliberately lock-free; the repeat after the
            # binding lock is the one a reconciliation read also performs.
            return Rows(pool.rows)
        if query.startswith("INSERT INTO speech_jobs "):
            assert self.key_held, "the writer must re-check its key under the binding lock"
            pool.writer_at_insert.set()
            assert pool.release_writer.wait(3), "writer barrier timed out"
            if pool.outcome == "rollback":
                raise psycopg.errors.CheckViolation("synthetic rollback before insert")
            pool.inserts += 1
            self.staged = True
        if "WHERE j.job_id = ?" in query:
            return Rows([_public_row(pool.request)] if self.staged else [])
        return Rows([])


class PauseFirstReplay(PostgresPublicJobs):
    def __init__(self, pool: BarrierPool) -> None:
        super().__init__(cast(SpeechPool, pool))
        self.first_empty, self.resume = Event(), Event()
        self.first = True

    def find_by_key(
        self,
        workspace_id: str,
        key_digest: str,
        request_digest: str,
        retry_of_job_id: str | None,
        *,
        binding_id: str | None,
    ) -> Any:
        result = super().find_by_key(
            workspace_id,
            key_digest,
            request_digest,
            retry_of_job_id,
            binding_id=binding_id,
        )
        if self.first:
            self.first = False
            assert result is None
            self.first_empty.set()
            assert self.resume.wait(3), "first-read barrier timed out"
        return result


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("outcome", ["commit", "lost_reply", "rollback", "conflict"])
def test_early_rejection_waits_for_in_flight_keyed_writer(
    pinned: bool, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    _, facts, store = _service()
    if pinned:
        _pin_facts(facts)
        command = _pinned_body()
    else:
        command = {
            "kind": "tts",
            "model": "tts-quality",
            "input": {"text": "synthetic hello"},
            "voice": {"id": "voice-vivian", "revision": "4"},
        }
        PublicJobs(facts, store).submit(command, request_id="a", idempotency_key="idem-pin-0001")
        request = store.requests[0]
    pool = BarrierPool(request, outcome)
    writer = PostgresPublicJobs(cast(SpeechPool, pool))
    reader = PauseFirstReplay(pool)
    service = PublicJobs(facts, reader)
    if outcome == "conflict":
        command = {**command, "client_reference": "different"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(
            service.submit, command, request_id="b", idempotency_key="idem-pin-0001"
        )
        try:
            assert reader.first_empty.wait(3)
            writing = executor.submit(writer.create, request)
            assert pool.writer_at_insert.wait(3)
            # Workspace state is independent of the binding object lock.
            monkeypatch.setattr(facts, "workspace", lambda _: WorkspaceFact(7, False))
            reader.resume.set()
            assert pool.waiter_at_lock.wait(3)
            assert not pending.done(), "rejection must await the keyed writer"
        finally:
            reader.resume.set()
            pool.release_writer.set()
        created = writing.result(timeout=3)
        if outcome == "rollback":
            assert created.outcome == "rejected"
            with pytest.raises(DisabledWorkspaceError):
                pending.result(timeout=3)
            assert not pool.rows and pool.inserts == 0
        elif outcome == "conflict":
            with pytest.raises(IdempotencyConflictError):
                pending.result(timeout=3)
            assert created.outcome == "created" and pool.inserts == 1
        else:
            original = pending.result(timeout=3)
            assert original.job_id == request.job_id
            assert original.submission == request.submission
            assert created.outcome == ("unknown" if outcome == "lost_reply" else "created")
            assert len(pool.rows) == pool.inserts == 1


@pytest.mark.parametrize("error", [psycopg.errors.QueryCanceled, psycopg.OperationalError])
def test_replay_lock_failure_cannot_report_absence_or_write(error: type[psycopg.Error]) -> None:
    class BrokenPool(Pool):
        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
            if "coordination_keyed_locks" in query and "FOR UPDATE" in query:
                raise error("synthetic binding settlement unavailable")
            return super().execute(query, parameters)

    pool = BrokenPool(_request())
    with pytest.raises(PublicJobUnavailableError):
        PostgresPublicJobs(cast(SpeechPool, pool)).find_by_key(
            pool.request.workspace_id,
            cast(str, pool.request.idempotency_key_digest),
            pool.request.request_digest,
            pool.request.retry_of_job_id,
            binding_id=pool.request.model_id,
        )
    assert pool.rollbacks == 1
    assert business_inserts(pool.calls) == []


def test_keyless_create_does_not_read_or_write_a_key() -> None:
    request = replace(_request(), idempotency_key_digest=None)
    pool = Pool(request)
    assert PostgresPublicJobs(cast(SpeechPool, pool)).create(request).outcome == "created"
    assert not any("j.idempotency_key_digest = ?" in query for query, _ in pool.calls)


@pytest.mark.parametrize("write", [False, True])
def test_deadline_exhausted_after_binding_wait_never_reads_absence_or_inserts(
    write: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    class KeyTimeoutPool(Pool):
        elapsed = 0.0

        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
            rows = super().execute(query, parameters)
            if "coordination_keyed_locks" in query and "FOR UPDATE" in query:
                self.elapsed = 3.1
            return rows

    pool = KeyTimeoutPool(_request())
    driver = PostgresPublicJobs(cast(SpeechPool, pool), timeout_seconds=3)
    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.public_jobs_postgres.time.monotonic",
        lambda: pool.elapsed,
    )
    if write:
        assert driver.create(pool.request).outcome == "unknown"
    else:
        with pytest.raises(PublicJobUnavailableError):
            driver.find_by_key(
                pool.request.workspace_id,
                cast(str, pool.request.idempotency_key_digest),
                pool.request.request_digest,
                pool.request.retry_of_job_id,
                binding_id=pool.request.model_id,
            )
    assert business_inserts(pool.calls) == []
    if not write:
        # The settlement read is the only key read and it must not happen after
        # the lock consumed the budget; the write path's early lookup is lock-free.
        assert not any("j.idempotency_key_digest = ?" in query for query, _ in pool.calls)
