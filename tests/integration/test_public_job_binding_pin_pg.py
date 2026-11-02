"""Real PostgreSQL head selection, key contention and bounded Job transactions."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from psycopg.rows import dict_row
from tests.integration.test_public_jobs_http import JobSandbox, _body
from tests.integration.test_public_jobs_http import jobs_db as jobs_db
from tests.unit.backend.test_public_jobs import Store

from speechnexus_backend.speech.application.public_jobs import (
    DisabledWorkspaceError,
    IdempotencyConflictError,
    ModelBindingError,
    PublicJobs,
)
from speechnexus_backend.speech.domain.public_jobs import NewPublicJob, PublicJobUnavailableError
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import lock_binding
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    PostgresPublicJobs,
    _insert,
    _lock_submission_key,
)

pytestmark = pytest.mark.integration
KEY = "synthetic-del356-key-0001"


def _seed(db: JobSandbox) -> PostgresPublicJobs:
    # Change only the synthetic fixture in this test's newly migrated schema.
    definition = json.dumps({"logical_model": "narrator-tts", "tasks": ["tts_file"]})
    db.query("UPDATE speech_binding_revisions SET definition_json=%s", (definition,))
    digest = "sha256:" + "b" * 64
    db.query(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,'tts-quality',1,'{}',%s)",
        (digest, digest),
    )
    db.query(
        "INSERT INTO speech_binding_revisions "
        "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
        "definition_digest,request_digest,created_at_us) "
        "VALUES ('tts-quality','2','binding-rev-2',%s,1,%s,%s,%s,2)",
        (digest, definition, digest, digest),
    )
    db.query(
        "INSERT INTO speech_publications "
        "(kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('binding','tts-quality','2','binding-rev-2',2)"
    )
    _head(db, "2")
    return PostgresPublicJobs(db.pool)


def _head(db: JobSandbox, revision: str | None) -> None:
    if revision is None:
        db.query("DELETE FROM speech_publication_heads WHERE kind='binding'")
    else:
        db.query(
            "UPDATE speech_publication_heads SET revision=%s, revision_id=%s "
            "WHERE kind='binding' AND object_id='tts-quality'",
            (revision, "binding-rev-" + revision),
        )


def _payload(revision: str = "2") -> dict[str, Any]:
    return _body(model="narrator-tts", model_binding={"id": "tts-quality", "revision": revision})


def _request(driver: PostgresPublicJobs, revision: str = "2") -> NewPublicJob:
    capture = Store()
    PublicJobs(driver, capture).submit(
        _payload(revision), request_id="req-pin", idempotency_key=KEY
    )
    return capture.requests[0]


def _counts(db: JobSandbox, expected: int) -> None:
    for table in (
        "speech_jobs",
        "speech_job_submissions",
        "admission_operations",
        "coordination_jobs",
        "coordination_outbox",
    ):
        assert db.count(table) == expected, table


@contextmanager
def _blocker(db: JobSandbox) -> Iterator[Any]:
    with psycopg.connect(db.url, row_factory=dict_row) as connection:
        connection.execute(f'SET search_path TO "{db.schema}", pg_catalog')
        yield connection


def _wait(db: JobSandbox, blocker_pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        rows = db.query(
            "SELECT pid FROM pg_stat_activity WHERE usename=%s AND %s=ANY(pg_blocking_pids(pid))",
            (db.role, blocker_pid),
        )
        if rows:
            assert rows[0]["pid"] != blocker_pid
            return
        time.sleep(0.005)
    raise AssertionError("No independent PostgreSQL lock wait observed")


def test_pin_uses_authoritative_head_after_2_to_1_activation(jobs_db: JobSandbox) -> None:
    driver = _seed(jobs_db)
    _head(jobs_db, "1")
    one, two = driver.binding_revision("tts-quality", 1), driver.binding_revision("tts-quality", 2)
    assert one is not None and one.current
    assert two is not None and not two.current
    created = PublicJobs(driver, driver).submit(
        _payload("1"), request_id="req-one", idempotency_key=KEY
    )
    assert json.loads(created.submission.effective_configuration_json)["models"]["tts"] == {
        "id": "tts-quality",
        "revision": "1",
    }
    with pytest.raises(ModelBindingError, match="model_binding_not_current"):
        PublicJobs(driver, driver).submit(_payload("2"), request_id="req-two", idempotency_key=None)
    _counts(jobs_db, 1)


def test_empty_head_does_not_fall_back_to_published_max(jobs_db: JobSandbox) -> None:
    driver = _seed(jobs_db)
    _head(jobs_db, None)
    for revision in ("1", "2"):
        fact = driver.binding_revision("tts-quality", int(revision))
        assert fact is not None and not fact.current
        with pytest.raises(ModelBindingError, match="model_binding_not_current"):
            PublicJobs(driver, driver).submit(
                _payload(revision), request_id="req-empty", idempotency_key=KEY
            )
    _counts(jobs_db, 0)


def test_exact_internal_revision_id_must_match_the_resolved_pin(jobs_db: JobSandbox) -> None:
    driver = _seed(jobs_db)
    request = _request(driver)
    assert request.expected_binding is not None
    changed = replace(
        request,
        expected_binding=replace(request.expected_binding, revision_id="other-internal-revision"),
    )
    assert driver.create(changed).outcome == "unknown"
    _counts(jobs_db, 0)


@pytest.mark.parametrize("mutation", ["rollback_head", "clear", "retire"])
def test_waiter_revalidates_after_catalogue_lock(jobs_db: JobSandbox, mutation: str) -> None:
    driver = _seed(jobs_db)
    request = _request(driver)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with _blocker(jobs_db) as blocker:
            lock_binding(blocker, "tts-quality")
            pending = executor.submit(driver.create, request)
            _wait(jobs_db, blocker.info.backend_pid)
            if mutation == "retire":
                blocker.execute(
                    "INSERT INTO speech_publication_retirements "
                    "(kind,object_id,revision,revision_id,retired_at_us,mode,reason) "
                    "VALUES ('binding','tts-quality','2','binding-rev-2',3,'retire',NULL)"
                )
            elif mutation == "clear":
                blocker.execute("DELETE FROM speech_publication_heads WHERE kind='binding'")
            else:
                blocker.execute(
                    "UPDATE speech_publication_heads SET revision='1',revision_id='binding-rev-1' "
                    "WHERE kind='binding'"
                )
        with pytest.raises(ModelBindingError):
            pending.result(timeout=4)
    _counts(jobs_db, 0)


@pytest.mark.parametrize("conflict", [False, True])
def test_same_key_wait_recovers_original_before_changed_head(
    jobs_db: JobSandbox,
    conflict: bool,
) -> None:
    driver = _seed(jobs_db)
    request = _request(driver)
    other = replace(request, job_id=request.job_id + "-other")
    if conflict:
        # A fresh application resolution of a different pin creates a different intent.
        _head(jobs_db, "1")
        other = _request(driver, "1")
        _head(jobs_db, "2")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with _blocker(jobs_db) as blocker:
            _lock_submission_key(
                blocker, request.workspace_id, cast(str, request.idempotency_key_digest)
            )
            lock_binding(blocker, "tts-quality")
            _insert(blocker, request)
            pending = executor.submit(driver.create, other)
            _wait(jobs_db, blocker.info.backend_pid)
            blocker.execute("DELETE FROM speech_publication_heads WHERE kind='binding'")
        result = pending.result(timeout=4)
    assert result.outcome == ("conflict" if conflict else "replayed")
    if not conflict:
        assert result.job is not None and result.job.job_id == request.job_id
        assert result.job.submission == request.submission
    _counts(jobs_db, 1)


def test_native_binding_lock_timeout_has_no_partial_job(jobs_db: JobSandbox) -> None:
    driver = _seed(jobs_db)
    request = _request(driver)
    bounded = PostgresPublicJobs(jobs_db.pool, timeout_seconds=0.4)
    with ThreadPoolExecutor(max_workers=1) as executor, _blocker(jobs_db) as blocker:
        lock_binding(blocker, "tts-quality")
        started = time.monotonic()
        pending = executor.submit(bounded.create, request)
        _wait(jobs_db, blocker.info.backend_pid)
        assert pending.result(timeout=2).outcome == "unknown"
        assert 0.25 <= time.monotonic() - started < 2
    _counts(jobs_db, 0)


class CommitReplyLoss:
    def __init__(self, pool: SpeechPool) -> None:
        self.pool, self.calls = pool, 0

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        self.calls += 1
        with self.pool.connection(timeout=timeout) as raw:
            yield raw
        # This is an actual DB commit followed by injected reply loss, not packet loss.
        raise psycopg.OperationalError("synthetic reply loss after actual commit")


def test_actual_commit_reply_loss_recovers_original_key(jobs_db: JobSandbox) -> None:
    driver = _seed(jobs_db)
    request = _request(driver)
    proxy = CommitReplyLoss(jobs_db.pool)
    lost = PostgresPublicJobs(cast(SpeechPool, proxy)).create(request)
    assert lost.outcome == "unknown" and proxy.calls == 1
    _counts(jobs_db, 1)
    _head(jobs_db, None)
    recovered = driver.find_by_key(
        request.workspace_id, cast(str, request.idempotency_key_digest), request.request_digest
    )
    assert recovered is not None and recovered.job is not None
    assert recovered.outcome == "replayed" and recovered.job.job_id == request.job_id
    assert recovered.job.submission == request.submission
    _counts(jobs_db, 1)


def test_exhausted_real_pool_does_not_invent_absence(jobs_db: JobSandbox) -> None:
    driver = PostgresPublicJobs(jobs_db.pool, timeout_seconds=0.1)
    with (
        jobs_db.pool.connection(),
        jobs_db.pool.connection(),
        pytest.raises(PublicJobUnavailableError),
    ):
        driver.find_by_key("workspace-default", "sha256:" + "a" * 64, "sha256:" + "b" * 64)
    _counts(jobs_db, 0)


class SlowSQL:
    """Delays are real SQL under the adapter's current statement_timeout."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Any:
        if "set_config" not in query:
            self.raw.execute("SELECT pg_sleep(0.065)")
        return self.raw.execute(query, parameters)


class SlowPool:
    def __init__(self, pool: SpeechPool) -> None:
        self.pool = pool

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        with self.pool.connection(timeout=timeout) as raw:
            yield SlowSQL(raw)


def test_real_key_wait_and_multiple_sql_share_deadline(jobs_db: JobSandbox) -> None:
    driver = _seed(jobs_db)
    request = _request(driver)
    slow = PostgresPublicJobs(cast(SpeechPool, SlowPool(jobs_db.pool)), timeout_seconds=0.65)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with _blocker(jobs_db) as blocker:
            _lock_submission_key(
                blocker, request.workspace_id, cast(str, request.idempotency_key_digest)
            )
            started = time.monotonic()
            pending = executor.submit(slow.create, request)
            _wait(jobs_db, blocker.info.backend_pid)
            blocker.execute("SELECT pg_sleep(0.25)")
        assert pending.result(timeout=2).outcome == "unknown"
        assert 0.5 <= time.monotonic() - started < 2
    _counts(jobs_db, 0)


@pytest.mark.parametrize("outcome", ["commit", "rollback", "conflict"])
def test_fact_rejection_reconciles_in_flight_original(jobs_db: JobSandbox, outcome: str) -> None:
    driver = _seed(jobs_db)
    request = _request(driver)
    empty, resume = threading.Event(), threading.Event()

    class PausedLookup(PostgresPublicJobs):
        first = True

        def find_by_key(self, workspace_id: str, key_digest: str, request_digest: str) -> Any:
            result = super().find_by_key(workspace_id, key_digest, request_digest)
            if self.first:
                self.first = False
                assert result is None
                empty.set()
                assert resume.wait(4)
            return result

    reader = PausedLookup(jobs_db.pool)
    payload = _payload()
    if outcome == "conflict":
        payload["client_reference"] = "different-intent"
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            PublicJobs(reader, reader).submit, payload, request_id="req-race", idempotency_key=KEY
        )
        try:
            assert empty.wait(3)
            with _blocker(jobs_db) as blocker:
                _lock_submission_key(
                    blocker, request.workspace_id, cast(str, request.idempotency_key_digest)
                )
                lock_binding(blocker, "tts-quality")
                _insert(blocker, request)
                # Separate authoritative workspace commit exposes an early rejection.
                jobs_db.query("UPDATE speech_workspaces SET workspace_state='disabled'")
                resume.set()
                _wait(jobs_db, blocker.info.backend_pid)
                if outcome == "rollback":
                    blocker.rollback()
            if outcome == "rollback":
                with pytest.raises(DisabledWorkspaceError):
                    pending.result(timeout=4)
            elif outcome == "conflict":
                with pytest.raises(IdempotencyConflictError):
                    pending.result(timeout=4)
            else:
                original = pending.result(timeout=4)
                assert (
                    original.job_id == request.job_id and original.submission == request.submission
                )
        finally:
            resume.set()
    _counts(jobs_db, 0 if outcome == "rollback" else 1)
