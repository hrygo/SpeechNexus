"""Two writers racing one public Job key, on a real migrated PostgreSQL.

The key is decided by the unique constraint on
``(workspace_id, idempotency_key_digest)``, not by an advisory lock. These cases
pin the two answers a loser may have -- the winner's own Job, or 409 when the
key was reused for a different request -- and each one proves the transactions
really overlapped by observing a ``pg_blocking_pids`` wait, so a sequential run
cannot pass them.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from tests.integration.test_public_job_binding_pin_pg import _blocker, _wait
from tests.integration.test_public_jobs_http import JobSandbox, _body
from tests.integration.test_public_jobs_http import jobs_db as jobs_db
from tests.unit.backend.test_public_jobs import Store

from speechnexus_backend.speech.application.public_jobs import IdempotencyConflictError, PublicJobs
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import _insert

pytestmark = pytest.mark.integration
KEY = "synthetic-key-race-0001"


def _observe_blocked(db: JobSandbox, *, timeout: float = 5.0) -> None:
    """Fail unless one public-Job backend is blocked by another right now."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = db.query(
            "SELECT pid FROM pg_stat_activity WHERE usename = %s "
            "AND cardinality(pg_blocking_pids(pid)) > 0",
            (db.role,),
        )
        if rows:
            return
        time.sleep(0.005)
    raise AssertionError("No concurrent PostgreSQL lock wait observed")


def _hold_job_inserts(db: JobSandbox) -> None:
    """Make every Job insert sleep inside its transaction.

    Without this the winner commits before the loser starts, and "both answer
    the same Job" would only prove sequencing. With it the winner is visibly
    uncommitted while the loser runs.
    """
    db.query(
        "CREATE FUNCTION synthetic_hold_job() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN PERFORM pg_sleep(0.5); RETURN NEW; END $$"
    )
    db.query(
        "CREATE TRIGGER synthetic_hold_job BEFORE INSERT ON speech_jobs "
        "FOR EACH ROW EXECUTE FUNCTION synthetic_hold_job()"
    )


def _release_job_inserts(db: JobSandbox) -> None:
    db.query("DROP TRIGGER IF EXISTS synthetic_hold_job ON speech_jobs")
    db.query("DROP FUNCTION IF EXISTS synthetic_hold_job()")


def _seed_alternative_binding(db: JobSandbox) -> None:
    """Publish a second tts binding so two requests take different binding locks.

    The two creates then cannot meet on ``lock_binding`` and can only meet in
    the key's unique index, which is what makes the insert loser observable.
    """
    digest = "sha256:" + "c" * 64
    db.query(
        "INSERT INTO speech_binding_families (binding_id, latest_revision) VALUES ('tts-alt', '1')"
    )
    db.query(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,'tts-alt',1,'{}',%s)",
        (digest, digest),
    )
    db.query(
        "INSERT INTO speech_binding_revisions "
        "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
        "definition_digest,request_digest,created_at_us) "
        "VALUES ('tts-alt','1','binding-alt-1',%s,1,%s,%s,%s,3)",
        (digest, json.dumps({"tasks": ["tts_file"]}), digest, digest),
    )
    db.query(
        "INSERT INTO speech_publications "
        "(kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('binding','tts-alt','1','binding-alt-1',3)"
    )
    db.query(
        "INSERT INTO speech_publication_heads "
        "(kind,object_id,revision,revision_id,updated_at_us) "
        "VALUES ('binding','tts-alt','1','binding-alt-1',3)"
    )


def _capture_request(db: JobSandbox) -> Any:
    """Resolve one Job intent without writing it, for the blocker to insert."""
    capture = Store()
    PublicJobs(db.jobs._facts, capture).submit(
        _body(), request_id="req-race-holder", idempotency_key=KEY
    )
    return capture.requests[0]


def test_concurrent_identical_public_job_creates_answer_one_job(jobs_db: JobSandbox) -> None:
    """Two identical creates overlap and both answer the one committed Job.

    The winner is held inside its INSERT by a trigger, so the loser runs while
    the key's row is uncommitted: it must wait for that writer (observed) and
    then report the same Job, with no second row and no conflict.
    """
    _hold_job_inserts(jobs_db)
    try:
        barrier = threading.Barrier(2)

        def submit() -> Any:
            barrier.wait(timeout=5)
            return jobs_db.jobs.submit(
                _body(), request_id="req-race-" + str(threading.get_ident()), idempotency_key=KEY
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(submit)
            second = workers.submit(submit)
            _observe_blocked(jobs_db)
            left, right = first.result(timeout=20), second.result(timeout=20)
    finally:
        _release_job_inserts(jobs_db)

    assert jobs_db.count("speech_jobs") == 1
    assert left.job_id == right.job_id
    assert left.state == right.state == "pending"


def test_public_job_key_reuse_loses_in_the_unique_index_and_answers_conflict(
    jobs_db: JobSandbox,
) -> None:
    """The insert loser of a reused key answers 409, never 503 or a second Job.

    Both creates take different binding locks (``tts-quality`` and the seeded
    ``tts-alt``), so their only meeting point is the key's unique index. The
    loser's INSERT waits on the winner's uncommitted row, the constraint then
    refuses it, and the retry reads the committed original: the differing
    request digest is a conflict, and the deferral of the decision to the
    constraint is what turns a ``23505`` into that answer instead of ``unknown``.
    """
    _seed_alternative_binding(jobs_db)
    request = _capture_request(jobs_db)
    workers = ThreadPoolExecutor(max_workers=1)
    try:
        with _blocker(jobs_db) as blocker:
            _insert(blocker, request, admitted=False)
            loser = workers.submit(
                jobs_db.jobs.submit,
                _body(model="tts-alt"),
                request_id="req-race-loser",
                idempotency_key=KEY,
            )
            _wait(jobs_db, blocker.info.backend_pid)
        # Committed above: the unique index now refuses the loser's insert.
        with pytest.raises(IdempotencyConflictError):
            loser.result(timeout=20)
    finally:
        workers.shutdown(wait=True)

    assert jobs_db.count("speech_jobs") == 1


def test_concurrent_identical_public_job_cancellations_keep_one_receipt(
    jobs_db: JobSandbox,
) -> None:
    """Two identical cancellations converge on the one durable acceptance.

    The winner is held inside its receipt INSERT, so the loser runs while the
    Job row is still being written: it waits for that transaction (observed),
    then replays the first receipt instead of accepting a second time. One
    receipt, one fence, and both callers report the first accepted fact.
    """
    job = jobs_db.jobs.submit(_body(), request_id="req-cancel-source", idempotency_key=None)
    jobs_db.jobs.advance(job.job_id, "running")
    jobs_db.query(
        "CREATE FUNCTION synthetic_hold_cancel() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN PERFORM pg_sleep(0.5); RETURN NEW; END $$"
    )
    jobs_db.query(
        "CREATE TRIGGER synthetic_hold_cancel BEFORE INSERT ON speech_job_cancellations "
        "FOR EACH ROW EXECUTE FUNCTION synthetic_hold_cancel()"
    )
    try:
        barrier = threading.Barrier(2)

        def cancel() -> Any:
            barrier.wait(timeout=5)
            return jobs_db.jobs.cancel(
                job.job_id,
                request_id="req-cancel-" + str(threading.get_ident()),
                idempotency_key=KEY,
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(cancel)
            second = workers.submit(cancel)
            _observe_blocked(jobs_db)
            left, right = first.result(timeout=20), second.result(timeout=20)
    finally:
        jobs_db.query("DROP TRIGGER IF EXISTS synthetic_hold_cancel ON speech_job_cancellations")
        jobs_db.query("DROP FUNCTION IF EXISTS synthetic_hold_cancel()")

    assert jobs_db.count("speech_job_cancellations") == 1
    assert jobs_db.count("speech_job_cancellation_fences") == 1
    assert left.receipt.requested_at_us == right.receipt.requested_at_us
    assert left.receipt.request_id == right.receipt.request_id
    assert {left.accepted_now, right.accepted_now} == {True, False}
    assert left.job.state == right.job.state == "cancelling"
