"""Child process for the cancellation settlement crash probe (test support only).

The probe needs a *separate interpreter* that dies at one exact point: after the
cancellation settlement transaction commits and before the capacity-release
transaction starts. Nothing here is product code or reachable from it; the child
assembles the same objects the cancellation reconciliation runtime assembles and
runs the same bounded pass.

Invocation::

    python tests/support/cancellation_settlement_crash.py

Environment (all required unless noted):

* ``SN1032_DATABASE_URL``, ``SN1032_SCHEMA``, ``SN1032_ROLE``,
  ``SN1032_PASSWORD`` -- the isolated PostgreSQL sandbox.
* ``SN1032_JOB_ID`` -- the Job whose accepted cancellation is being reconciled.
* ``SN1032_CRASH_AT`` -- optional. The only accepted value is ``before-release``:
  the capacity port verifies the committed settlement through an independent
  connection, prints that snapshot, and calls ``os._exit(9)`` instead of
  releasing. Unset means a normal recovery pass.
* ``SN1032_NOW_US`` -- optional claim timestamp; defaults to the wall clock.

The gateway is the one boundary this probe does not exercise: the child installs
a no-execution document for the stable submit intent, which is the gateway fact
the confirmer consumes. Everything after that fact is the production path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping
from typing import Any, cast

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from speechnexus_backend.job_cancellation_runtime import _PublicJobsSettlement
from speechnexus_backend.speech.application.job_cancellation import CancellationCapacity
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    JobCancellationReconciler,
    Judgment,
)
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.infrastructure.gateway_cancellation_confirmation import (
    GatewayCancellationConfirmer,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    PostgresCancellationReconciliationStore,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresLeaseSettler
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from tests.support.portable_pg import connect as portable_pg_connect

CRASH_AT = "before-release"
KILLED_EXIT = 9
PRECONDITION_EXIT = 97


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required by the cancellation crash probe")
    return value


class _NoExecutionGateway:
    """One gateway answer: this stable submit intent never became an execution."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document

    async def __aenter__(self) -> _NoExecutionGateway:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def query_document(self, submit_intent_id: str) -> Mapping[str, Any]:
        if submit_intent_id != self._document["submit_intent_id"]:
            raise AssertionError("the probe queried an unexpected submit intent")
        return self._document


class _Unjudged:
    """The bounded pass must not reach the judgment branch in this window."""

    def settle(self, job_id: str) -> Judgment:
        del job_id
        return Judgment(settled=False, outcome="unavailable")


class _DiesBeforeRelease(CancellationCapacity):
    """Capacity port that proves the boundary and kills its own process.

    ``CancellationCapacity.release`` is called by ``_PublicJobsSettlement`` only
    after ``PublicJobs.settle_cancelled`` returned, so the settlement transaction
    has committed. This override reads that fact through a *new* connection and
    exits the interpreter instead of starting the release transaction.
    """

    def __init__(self, job_id: str, dsn: str, schema: str) -> None:
        super().__init__(self._unreachable)
        self._job_id, self._dsn, self._schema = job_id, dsn, schema

    @staticmethod
    def _unreachable(job_id: str) -> bool:
        raise AssertionError(f"the crash port must not release {job_id}")

    def release(self, job_id: str) -> bool:
        if job_id != self._job_id:
            raise AssertionError("the probe released an unexpected Job")
        connection = portable_pg_connect(self._dsn, autocommit=True, row_factory=dict_row)
        try:
            connection.execute("SELECT set_config('search_path', %s, false)", (self._schema,))
            job = connection.execute(
                "SELECT state, status_version FROM speech_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
            leases = connection.execute(
                "SELECT lease_state, cleanup_state, budget_released_at_us "
                "FROM admission_leases WHERE operation_id = %s",
                (job_id,),
            ).fetchall()
        finally:
            connection.close()
        # The probe refuses to die anywhere but the intended point: the settlement must
        # have committed *and* the lease must still be held, otherwise the crash would
        # prove nothing about the gap between the two transactions.
        unheld = (
            len(leases) != 1
            or leases[0]["lease_state"] != "reserved"
            or leases[0]["budget_released_at_us"] is not None
        )
        if job is None or job["state"] != "cancelled" or unheld:
            print(
                json.dumps(
                    {
                        "kill_point": CRASH_AT,
                        "error": "the settlement did not commit before the release call",
                        "job": job,
                        "leases": leases,
                    },
                    default=str,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            sys.stderr.flush()
            os._exit(PRECONDITION_EXIT)
        lease = leases[0]
        print(
            json.dumps(
                {
                    "kill_point": CRASH_AT,
                    "pid": os.getpid(),
                    "job_state": job["state"],
                    "job_status_version": job["status_version"],
                    "lease_state": lease["lease_state"],
                    "lease_cleanup_state": lease["cleanup_state"],
                    "budget_released_at_us": lease["budget_released_at_us"],
                },
                default=str,
                sort_keys=True,
            )
        )
        sys.stdout.flush()
        os._exit(KILLED_EXIT)


def _gateway_document(job_id: str) -> dict[str, Any]:
    return {
        "submit_intent_id": submit_intent_identity(job_id),
        "operation_id": job_id,
        "attempt_id": job_id + "-a1",
        "execution_state": "failed",
        "cleanup_state": "not_required",
        "error_code": "gateway_barrier_fenced",
        "stop_evidence_kind": "none",
        "dispatched_at": None,
        "engine_request_id": None,
    }


def main() -> int:
    dsn = make_conninfo(
        _env("SN1032_DATABASE_URL"),
        user=_env("SN1032_ROLE"),
        password=_env("SN1032_PASSWORD"),
    )
    schema, job_id = _env("SN1032_SCHEMA"), _env("SN1032_JOB_ID")
    crash_at = os.environ.get("SN1032_CRASH_AT", "")
    now_us = int(os.environ.get("SN1032_NOW_US") or time.time_ns() // 1_000)
    if crash_at not in ("", CRASH_AT):
        raise SystemExit(f"unknown SN1032_CRASH_AT value: {crash_at}")

    document = _gateway_document(job_id)
    GatewayExecutionClient.from_settings = classmethod(  # type: ignore[assignment]
        lambda cls, settings: _NoExecutionGateway(document)
    )
    pool = speech_pool(dsn, schema=schema, max_size=2, name="sn1032-crash-probe")
    pool.open()
    try:
        adapter = PostgresPublicJobs(pool, timeout_seconds=3)
        jobs = PublicJobs(adapter, adapter)
        settler = PostgresLeaseSettler(pool, timeout_seconds=3)
        capacity: CancellationCapacity
        if crash_at == CRASH_AT:
            capacity = _DiesBeforeRelease(job_id, dsn, schema)
        else:
            capacity = CancellationCapacity(settler.release_sync)
        settlement = _PublicJobsSettlement(jobs, capacity)
        confirmer = GatewayCancellationConfirmer(cast(Any, None), settlement)
        reconciler = JobCancellationReconciler(
            store=PostgresCancellationReconciliationStore(pool),
            confirmer=confirmer,
            unconfirmed=_Unjudged(),
            min_age_seconds=0,
        )
        batch = asyncio.run(reconciler.run_once(now_us=now_us))
        print(
            json.dumps(
                {
                    "claimed": batch.claimed,
                    "settled": batch.settled,
                    "retried": batch.retried,
                    "exhausted": batch.exhausted,
                    "unavailable": batch.unavailable,
                    "blocked": [item.job_id for item in batch.blocked],
                },
                sort_keys=True,
            )
        )
        sys.stdout.flush()
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
