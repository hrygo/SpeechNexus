"""`stop_evidence_debt` on the real database: it must name the held class and no other.

The dispatch gate holds a `running` Job whose gateway reported failed/aborted without a
verified stop, and nothing settles it automatically. This probe is the only enumerable
surface for that class, so the test pins both directions: the held shape is named, and
every neighbouring class -- provable non-dispatch, a recorded verified stop, a `cancelling`
Job and a released lease -- is not, because each has a different disposition.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from tests.integration.test_cancel_failure_slot_pg import (
    DIGEST,
    PASSWORD,
    VARIABLE,
    WORKSPACE,
    _migrate,
    _seed_execution,
    _seed_foundation,
)
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_debt_reader_grants,
)
from speechnexus_backend.speech.infrastructure.readiness import StopEvidenceDebtProbe

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
INSTANCE = "instance-test"
LEASE = "lease-test-stage-main"


@dataclass
class DebtSandbox:
    schema: str
    role: str
    job_id: str
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)

    def probe(self) -> str | None:
        probe = StopEvidenceDebtProbe(
            make_conninfo(os.environ[VARIABLE], user=self.role, password=PASSWORD),
            schema=self.schema,
            timeout_seconds=3,
            grace_seconds=0.001,
        )
        return asyncio.run(probe.check())


@contextmanager
def _sandbox(
    *,
    job_state: str = "running",
    execution: tuple[str, str] | None = ("failed", "complete"),
    stop_evidence: tuple[str, int, int] | None = None,
    dispatch_identity: bool = True,
    lease_state: str = "reserved",
) -> Iterator[DebtSandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_gate_" + suffix, "sn_gate_role_" + suffix
    job_id = "job-" + uuid.uuid4().hex
    _migrate(url, schema)
    admin = portable_pg_connect(url, autocommit=True, row_factory=dict_row)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    try:
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in cancellation_debt_reader_grants(schema, role):
            admin.execute(grant)
        _seed_foundation(admin, job_id, (LEASE,))
        if execution is not None:
            _seed_execution(
                admin,
                job_id,
                LEASE,
                execution,
                identity="submit-" + job_id.removeprefix("job-"),
            )
            if not dispatch_identity:
                admin.execute(
                    "UPDATE submit_intents SET dispatched_at_us = NULL, engine_request_id = NULL "
                    "WHERE lease_id = %s",
                    (LEASE,),
                )
        if stop_evidence is not None:
            admin.execute(
                "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, "
                "evidence_kind, evidence_digest, observed_at_us, execution_stopped, "
                "resources_released) VALUES (%s, %s, %s, %s, 1, %s, %s)",
                (
                    "evidence-" + job_id,
                    "submit-" + job_id.removeprefix("job-"),
                    stop_evidence[0],
                    DIGEST,
                    stop_evidence[1],
                    stop_evidence[2],
                ),
            )
        admin.execute(
            "UPDATE admission_leases SET lease_state = %s WHERE operation_id = %s",
            (lease_state, job_id),
        )
        admin.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "state, status_version, created_at_us, updated_at_us) VALUES (%s, %s, 'tts', "
            "'tts-quality', %s, %s, 2, 1, 1)",
            (job_id, WORKSPACE, DIGEST, job_state),
        )
        admin.execute(
            "INSERT INTO speech_job_submissions (job_id, request_id, client_reference, "
            "effective_configuration_json, execution_revision, progress_unit, "
            "progress_total, submitted_at_us) VALUES (%s, 'request-917', NULL, %s, '1', "
            "'text_codepoints', 1, 1)",
            (job_id, json.dumps({})),
        )
        yield DebtSandbox(schema=schema, role=role, job_id=job_id, admin=admin)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def test_a_running_job_without_stop_evidence_is_named() -> None:
    with _sandbox() as box:
        detail = box.probe()

    assert detail is not None
    assert detail.startswith("stop_evidence_debt: 1 running Job(s) hold capacity")
    assert box.job_id in detail
    assert "execution=failed" in detail
    assert "cleanup=complete" in detail
    assert "evidence=none" in detail


def test_a_recorded_verified_stop_is_not_this_debt() -> None:
    """A held lease that outlived its own verified stop is the other class."""
    with _sandbox(
        stop_evidence=("instance_terminated_by_controller", 1, 1), lease_state="reserved"
    ) as box:
        detail = box.probe()

    assert detail is None


def test_a_provable_non_dispatch_is_not_this_debt() -> None:
    """The dispatcher settles that shape itself; holding it would be a bug."""
    with _sandbox(execution=("failed", "not_required"), dispatch_identity=False) as box:
        box.admin.execute("UPDATE submit_intents SET cleanup_state = 'not_required'")
        detail = box.probe()

    assert detail is None


def test_a_cancelling_job_belongs_to_the_cancellation_debt() -> None:
    with _sandbox(job_state="cancelling") as box:
        assert box.probe() is None


def test_a_returned_lease_is_not_debt() -> None:
    with _sandbox(lease_state="released") as box:
        assert box.probe() is None


def test_an_unprivileged_identity_reports_the_category_instead_of_failing() -> None:
    with _sandbox() as box:
        box.admin.execute(f'REVOKE ALL ON "{box.schema}".submit_intents FROM "{box.role}"')
        detail = box.probe()

    assert detail == "stop_evidence_debt: unavailable (permission)"
