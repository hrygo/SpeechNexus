"""Kill a real process between cancellation settlement and capacity release.

The accepted cancellation has two commits: the Job settlement transaction writes
``cancelled`` plus its event, and the capacity-release transaction returns the
admission lease. Everything written before this probe rebuilt the pool in the
same process, so the process boundary was never real. Here a *child interpreter*
runs the production reconciliation pass and calls ``os._exit(9)`` from the
capacity port, which is the first instruction of the release path: the settlement
has committed and the release has not started. Nothing is timed, so the window is
deterministic.

A second child interpreter then runs the normal bounded pass and must converge on
``cancelled`` + released capacity without settling the same cancellation twice.

Scope: this is the reconciliation *worker's* recovery path. It needs
``SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED=true``, and the default
deployment starts no progressor, so this proves what happens once a pass is enabled --
not that a default deployment heals itself. It also pins a code position, not signal
delivery: ``os._exit`` cannot land inside the release transaction, so a kill whose
commit outcome is unknown is out of scope.

Counterexample (run manually, never committed): in
``job_cancellation_runtime.py``, replace ``_PublicJobsSettlement.settle_cancelled``'s
``if job.state == "cancelled": return self._release(job_id)`` with ``return True``.
The replay then skips the unreleased lease and this test fails on the capacity
assertion after recovery -- the recovery logic is load-bearing, not incidental.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_public_job_cancellation_pg import (
    _create_running,
    _held_rows,
    _lease_row,
    _seed_reserved_lease,
)
from tests.integration.test_public_jobs_http import PASSWORD, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)

pytestmark = pytest.mark.integration
PROBE = Path(__file__).resolve().parents[1] / "support" / "cancellation_settlement_crash.py"
KILLED_EXIT = 9


@pytest.fixture
def crash_db(jobs_db: JobSandbox) -> JobSandbox:
    """Extend the Job sandbox role with the capacity writes the release needs.

    The reference deployment releases under its API identity while the Job writer
    deliberately cannot write capacity, so this fixture applies that declaration to
    the sandbox role rather than widening the shared Job writer fixture.
    """
    for grant in coordination_runtime_grants(
        jobs_db.schema, speech_role=jobs_db.role, gateway_role=jobs_db.role + "_unused"
    ):
        if f'"{jobs_db.role}"' in grant:
            jobs_db.admin.execute(grant)
    return jobs_db


def _probe_env(db: JobSandbox, job_id: str, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "SN1032_DATABASE_URL": db.url,
        "SN1032_SCHEMA": db.schema,
        "SN1032_ROLE": db.role,
        "SN1032_PASSWORD": PASSWORD,
        "SN1032_JOB_ID": job_id,
    }
    env.update(extra)
    return env


def _run_probe(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE)], env=env, capture_output=True, text=True, timeout=120
    )


def _last_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = completed.stdout.strip().splitlines()
    assert lines, completed.stderr
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


def _count_settlement_statements(db: JobSandbox, schema: str) -> None:
    """Count the settlement UPDATE at statement level, so a no-op replay still counts.

    The settlement UPDATE is guarded by ``AND j.state = 'cancelling'``, so a second
    settlement of an already ``cancelled`` Job would update zero rows and leave no trace
    at all -- no ``updated_at_us`` change, no event row. A row-level counter cannot see
    that either, because it never fires for zero rows. A statement-level trigger does,
    which turns "the same cancellation was not settled twice" from a claim about visible
    effects into a count of the statements that ran.
    """

    db.admin.execute(f'CREATE TABLE "{schema}".settlement_statements (count INTEGER NOT NULL)')
    db.admin.execute(f'INSERT INTO "{schema}".settlement_statements VALUES (0)')
    db.admin.execute(
        f'CREATE FUNCTION "{schema}".count_settlement() RETURNS trigger '
        f"LANGUAGE plpgsql AS $$ BEGIN "
        f'UPDATE "{schema}".settlement_statements SET count = count + 1; RETURN NULL; END $$'
    )
    db.admin.execute(
        f'CREATE TRIGGER count_settlement AFTER UPDATE ON "{schema}".speech_jobs '
        f"FOR EACH STATEMENT EXECUTE FUNCTION "
        f'"{schema}".count_settlement()'
    )
    # The settlement runs as the Job writer, so it must be allowed to run the counter:
    # without these the trigger would turn a legitimate settlement into a permission
    # error and the pass would retry instead of settling.
    db.admin.execute(f'GRANT SELECT, UPDATE ON "{schema}".settlement_statements TO "{db.role}"')
    db.admin.execute(f'GRANT EXECUTE ON FUNCTION "{schema}".count_settlement() TO "{db.role}"')


def _settlement_statements(db: JobSandbox, schema: str) -> int:
    rows = db.query(f'SELECT count FROM "{schema}".settlement_statements')
    assert len(rows) == 1, rows
    return int(rows[0]["count"])


def _ledger(db: JobSandbox, job_id: str) -> dict[str, Any]:
    rows = db.query(
        "SELECT attempts, next_check_at_us, last_outcome, settled_at_us "
        "FROM speech_job_cancellation_reconciliations WHERE job_id = %s",
        (job_id,),
    )
    assert len(rows) == 1, rows
    return rows[0]


async def test_a_kill_between_settlement_and_release_converges_in_a_new_process(
    crash_db: JobSandbox,
) -> None:
    job_id = await _create_running(crash_db, key="synthetic-crash-create-1")
    assert crash_db.jobs.cancel(
        job_id, request_id="req-crash-1", idempotency_key="synthetic-crash-key-1"
    ).accepted_now
    _seed_reserved_lease(crash_db, job_id)
    _count_settlement_statements(crash_db, crash_db.schema)
    assert crash_db.count("speech_job_cancellations") == 1
    assert crash_db.count("speech_job_cancellation_fences") == 1
    assert _lease_row(crash_db, job_id) == {"lease_state": "reserved", "cleanup_state": "pending"}
    started_us = time.time_ns() // 1_000

    # 1. A separate interpreter commits the settlement and dies at the release
    # call. It reads the committed fact back through its own connection first, so
    # the printed snapshot is the state at the kill instant, not the parent's.
    crash = _run_probe(_probe_env(crash_db, job_id, SN1032_CRASH_AT="before-release"))
    assert crash.returncode == KILLED_EXIT, crash.stderr
    killed = _last_json(crash)
    assert killed.pop("pid") != os.getpid()
    assert killed == {
        "kill_point": "before-release",
        "job_state": "cancelled",
        "job_status_version": 4,
        "lease_state": "reserved",
        "lease_cleanup_state": "pending",
        "budget_released_at_us": None,
    }

    # 2. The parent observes the real post-crash state: settlement committed,
    # capacity still held by a process that no longer exists.
    job = crash_db.query(
        "SELECT state, status_version, error_code, updated_at_us "
        "FROM speech_jobs WHERE job_id = %s",
        (job_id,),
    )
    assert len(job) == 1
    assert job[0]["state"] == "cancelled"
    assert job[0]["status_version"] == 4
    assert job[0]["error_code"] is None
    assert crash_db.query(
        "SELECT state FROM speech_job_events WHERE job_id = %s ORDER BY event_seq", (job_id,)
    ) == [
        {"state": "pending"},
        {"state": "running"},
        {"state": "cancelling"},
        {"state": "cancelled"},
    ]
    crashed_ledger = _ledger(crash_db, job_id)
    assert crashed_ledger["attempts"] == 1
    assert crashed_ledger["next_check_at_us"] > 0
    assert crashed_ledger["last_outcome"] is None
    assert crashed_ledger["settled_at_us"] is None
    assert _lease_row(crash_db, job_id) == {"lease_state": "reserved", "cleanup_state": "pending"}
    assert crash_db.query(
        "SELECT budget_released_at_us FROM admission_leases WHERE operation_id = %s", (job_id,)
    ) == [{"budget_released_at_us": None}]
    assert _held_rows(crash_db) == ([{"held_slots": 1}], [{"held_attempts": 1}])

    # 3. A second interpreter (a new process, not a reopened pool) runs the normal
    # bounded pass past the crashed claim's lease. It must return the capacity.
    recovered = _run_probe(_probe_env(crash_db, job_id, SN1032_NOW_US=str(started_us + 61_000_000)))
    assert recovered.returncode == 0, recovered.stderr
    assert _last_json(recovered) == {
        "claimed": 1,
        "settled": 1,
        "retried": 0,
        "exhausted": 0,
        "unavailable": 0,
        "blocked": [],
    }
    assert _lease_row(crash_db, job_id) == {"lease_state": "released", "cleanup_state": "complete"}
    assert crash_db.query(
        "SELECT budget_released_at_us IS NOT NULL AS released "
        "FROM admission_leases WHERE operation_id = %s",
        (job_id,),
    ) == [{"released": True}]
    assert _held_rows(crash_db) == ([{"held_slots": 0}], [{"held_attempts": 0}])
    settled = _ledger(crash_db, job_id)
    assert settled["attempts"] == 2
    assert settled["last_outcome"] == "settled"
    assert settled["settled_at_us"] is not None

    # The settlement is not repeated: the already committed terminal row keeps its
    # version, timestamp and single event, and the acceptance is still one row.
    assert crash_db.query(
        "SELECT state, status_version, updated_at_us FROM speech_jobs WHERE job_id = %s",
        (job_id,),
    ) == [{"state": "cancelled", "status_version": 4, "updated_at_us": job[0]["updated_at_us"]}]
    assert crash_db.query(
        "SELECT count(*) AS n FROM speech_job_events WHERE job_id = %s AND state = 'cancelled'",
        (job_id,),
    ) == [{"n": 1}]
    assert crash_db.count("speech_job_cancellations") == 1
    assert crash_db.count("speech_job_cancellation_fences") == 1
    # One settlement statement for the whole sequence: the recovery process returned
    # the capacity without issuing a settlement UPDATE at all, which the row-level
    # assertions above cannot show (a zero-row UPDATE leaves no trace).
    assert _settlement_statements(crash_db, crash_db.schema) == 1

    # 4. One more fresh process has nothing left to claim: the replay is a no-op,
    # not a second settlement or a second release.
    replay = _run_probe(_probe_env(crash_db, job_id, SN1032_NOW_US=str(started_us + 121_000_000)))
    assert replay.returncode == 0, replay.stderr
    assert _last_json(replay)["claimed"] == 0
    assert _ledger(crash_db, job_id)["settled_at_us"] == settled["settled_at_us"]
    assert _lease_row(crash_db, job_id) == {"lease_state": "released", "cleanup_state": "complete"}
    assert _held_rows(crash_db) == ([{"held_slots": 0}], [{"held_attempts": 0}])
    assert _settlement_statements(crash_db, crash_db.schema) == 1
