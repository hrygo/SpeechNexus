"""The admission report observes what a create would answer, on the real assembly.

#845: after the engine readiness receipt elapses, every ``POST /v1/nexus/jobs``
answers ``503 engine_not_ready`` while ``/readyz``, the container healthcheck and
``docker ps`` stay green. Nothing a monitor polled answered "would a create be
admitted?", so the only way to find out was to attempt one.

These cases drive the production HTTP assembly -- the writer pool, the admission
pool, the real receipt in ``runtime_instance_intents`` and the real quota row --
and assert the report and a create agree, in both directions:

* seeded state: the report is ``200 admitting`` and the same body is created;
* receipt rewritten to elapsed: the report is ``200 degraded`` with
  ``engine_receipt_expired``, ``/readyz`` is still ``200``, and the create answers
  ``503 engine_not_ready``; rewriting the receipt back -- the transaction the
  renewal entry runs -- restores ``admitting`` and admits the same body under the
  same key;
* quota fully held: ``capacity_exhausted`` and a create answers ``429
  job_capacity_exhausted`` with ``Retry-After``, not a storage answer;
* the admission role loses ``SELECT`` on ``workspace_quotas``: the report says
  ``storage_unavailable`` alone and a create answers ``503 job_unavailable``.

Boundaries -- what this module does not claim:

* The readiness receipt is rewritten in place instead of letting 24 h elapse. The
  state is the one admission checks, but the clock passage that produces it, and a
  real ``/health`` probe behind the renewal, are not exercised here; #828's suite
  covers the renewal transaction.
* ``/readyz`` is assembled with the production PostgreSQL probe against the real
  database and a stubbed object-storage probe. The claim under test is that receipt
  expiry moves *no* process-health check; object-storage reachability is that
  probe's own concern and is not re-qualified here.
* The storage-fault case revokes one grant on one table, which models a failed read
  of the quota row. It is not a network partition or failover qualification.
* Report latency under load, the container wiring and the poller's own behaviour
  are not asserted here: they belong to
  ``tests/ci/test_reference_deploy_job_admission_watch.py`` and
  ``tests/unit/backend/test_admission_watch.py``.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from tests.fake.readiness import StaticProbe
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_public_job_atomic_admission_pg import _seed, _store
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.infrastructure.readiness import PostgresReadinessProbe

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

PREFIX = "/v1/nexus/jobs"
REPORT = f"{PREFIX}/admission-readiness"
WORKSPACE = "workspace-test"
BINDING = "binding-test"
VOICE = "voice-vivian"
TEXT = "synthetic admission report"
DAY_US = 86_400_000_000


def _app(admission: AdmissionDatabase) -> Any:
    """The production public surface, with a real DB readiness probe.

    Object storage is stubbed healthy on purpose: the case asserts that the receipt
    window moves no process-health check, not that MinIO is reachable.
    """
    db = admission.runtime.foundation.database
    settings = _settings().model_copy(
        update={
            "database_schema": db.schema,
            "job_writer_database_url": SecretStr(db.url),
        }
    )
    readiness = ReadinessService(
        [
            PostgresReadinessProbe(db.url, timeout_seconds=5),
            StaticProbe("object_storage"),
        ],
        timeout_seconds=5,
    )
    store = _store(admission)
    return create_app(
        settings=settings, readiness=readiness, jobs=PublicJobs(store, store, readiness=store)
    )


def _body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "kind": "tts",
        "model": BINDING,
        "input": {"text": TEXT},
        "voice": {"id": VOICE, "revision": "2"},
        "client_reference": "synthetic-admission-report",
    }
    body.update(changes)
    return body


def _now_us(db: JobDatabase) -> int:
    """The coordination clock: the same source admission compares receipts against."""
    rows = db.query("SELECT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000000)::bigint AS us")
    return cast(int, rows[0]["us"])


def _receipt(db: JobDatabase) -> dict[str, Any]:
    rows = db.query(
        "SELECT readiness_document FROM runtime_instance_intents WHERE intent_id = 'runtime-new'"
    )
    assert len(rows) == 1 and rows[0]["readiness_document"], rows
    return cast(dict[str, Any], json.loads(cast(str, rows[0]["readiness_document"])))


def _rewrite_receipt(db: JobDatabase, *, verified_at_us: int, expires_at_us: int) -> None:
    """Re-create the receipt the renewal transaction writes, without an engine probe.

    ``speechnexus-register-engine --renew-readiness`` sets the receipt and
    ``engine_instances.ready_at_us`` to the probe time in one transaction, because
    admission requires ``verified_at_us <= ready_at_us <= now``. Writing only the
    document would test a state the product cannot produce.
    """
    document = _receipt(db)
    document["verified_at_us"] = verified_at_us
    document["expires_at_us"] = expires_at_us
    db.query(
        "UPDATE runtime_instance_intents SET readiness_document = %s "
        "WHERE intent_id = 'runtime-new'",
        (json.dumps(document, separators=(",", ":")),),
    )
    db.query(
        "UPDATE engine_instances SET ready_at_us = %s "
        "WHERE instance_id = 'instance-new' AND instance_generation = 1",
        (verified_at_us,),
    )


def _error(response: Response) -> tuple[str, bool]:
    body = response.json()["error"]
    return cast(str, body["code"]), cast(bool, body["retryable"])


async def _report(client: AsyncClient) -> dict[str, Any]:
    # The report defaults to `workspace-default`, so a deployment that creates
    # elsewhere passes its workspace; the fixture's workspace is `workspace-test`.
    response = await client.get(REPORT, params={"workspace_id": WORKSPACE})
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _causes(report: dict[str, Any]) -> list[str]:
    return cast(list[str], report["causes"])


def _state_of(report: dict[str, Any], instance_id: str) -> str:
    rows = [row for row in report["instances"] if row["instance_id"] == instance_id]
    assert len(rows) == 1, (instance_id, report["instances"])
    return cast(str, rows[0]["readiness_state"])


def _usable(report: dict[str, Any]) -> int:
    return sum(1 for row in report["instances"] if row["readiness_state"] in {"valid", "expiring"})


async def test_the_report_admits_exactly_while_a_create_is_admitted(
    admission_database: AdmissionDatabase,
) -> None:
    """The seeded deployment reports ``admitting`` and creates the body.

    This is the calibration case: if the report claimed health while the same
    deployment refused a create, the surface would be worse than nothing.
    """
    db = admission_database.runtime.foundation.database
    _seed(admission_database)
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        report = await _report(client)
        assert report["status"] == "admitting", report
        assert report["causes"] == [], report
        assert report["workspace_id"] == WORKSPACE
        assert report["detail"] is None
        # The instance rows and the quota row are the same facts admission reads.
        # The fixture registers more than one ready instance and only one needs a
        # usable receipt, so the case asserts that instance, not the row count.
        assert _state_of(report, "instance-new") in {"valid", "expiring"}
        assert _usable(report) >= 1
        assert report["capacity"] is not None
        created = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-report-admits"}
        )
        assert created.status_code == 202, created.text
        # The report's counters are the ones admission moved: one attempt is held
        # after the create, and the report sees it on the next read.
        before_held = report["capacity"]["held_attempts"]
        held = db.query("SELECT held_attempts FROM workspace_quotas")[0]["held_attempts"]
        assert held == before_held + 1
        after = await _report(client)
        assert after["capacity"]["held_attempts"] == held


async def test_an_elapsed_receipt_degrades_the_report_while_readyz_stays_200(
    admission_database: AdmissionDatabase,
) -> None:
    """The defect and its observation, on one deployment, in order.

    Before the fix the middle state was indistinguishable from a healthy one: the
    create refused while every health surface was green. Now the create still
    refuses, ``/readyz`` still answers ``200`` -- because the process is fine -- and
    the report names the class.
    """
    db = admission_database.runtime.foundation.database
    _seed(admission_database)
    now_us = _now_us(db)
    _rewrite_receipt(db, verified_at_us=now_us - 2 * DAY_US, expires_at_us=now_us - 60_000_000)
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        readyz = await client.get("/readyz")
        assert readyz.status_code == 200, (
            "process health must not follow a create-only degradation: failing /readyz "
            "would remove the whole API from a load balancer (#828 review B8)"
        )
        report = await _report(client)
        assert report["status"] == "degraded"
        assert _causes(report) == ["engine_receipt_expired"]
        assert _state_of(report, "instance-new") == "expired"
        # Every instance is listed, including one whose receipt is absent: the
        # report names what admission would check instead of only the pin.
        assert {row["readiness_state"] for row in report["instances"]} <= {
            "expired",
            "missing",
            "invalid",
            "valid",
            "expiring",
        }
        assert _usable(report) == 0
        assert "speechnexus-register-engine --renew-readiness" in cast(str, report["detail"])
        refused = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-report-expired"}
        )
        assert refused.status_code == 503, refused.text
        assert _error(refused) == ("engine_not_ready", False)
        # The refusal wrote nothing.
        assert db.query("SELECT count(*) AS n FROM speech_jobs")[0]["n"] == 0

        # Renewal: the transaction the operator runs, replayed as data.
        _rewrite_receipt(db, verified_at_us=now_us - 60_000_000, expires_at_us=now_us + DAY_US)
        recovered = await _report(client)
        assert recovered["status"] == "admitting", recovered
        assert recovered["causes"] == []
        admitted = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-report-expired"}
        )
        assert admitted.status_code == 202, (
            "the same key must be admitted once the receipt is re-signed, otherwise the "
            "report's 'admitting' would not mean 'a create succeeds'"
        )


async def test_a_fully_held_quota_is_capacity_not_a_storage_answer(
    admission_database: AdmissionDatabase,
) -> None:
    """Capacity exhaustion is its own class in the report and in the create answer."""
    db = admission_database.runtime.foundation.database
    _seed(admission_database)
    db.query("UPDATE workspace_quotas SET max_attempts = 1, held_attempts = 1")
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        report = await _report(client)
        assert report["status"] == "degraded"
        assert _causes(report) == ["capacity_exhausted"]
        assert report["capacity"]["max_attempts"] == 1
        assert report["capacity"]["held_attempts"] == 1
        refused = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-report-capacity"}
        )
        assert refused.status_code == 429, refused.text
        assert _error(refused) == ("job_capacity_exhausted", True)
        assert refused.headers["Retry-After"] == "1"
        assert db.query("SELECT count(*) AS n FROM speech_jobs")[0]["n"] == 0


async def test_a_workspace_without_a_quota_row_is_its_own_class(
    admission_database: AdmissionDatabase,
) -> None:
    """A missing quota row is deterministic, so the report must not say ``admitting``.

    The catalog row is inserted without a quota row, which is the state the asset
    migration leaves behind: the workspace exists, and only engine registration
    writes the counters admission charges (#702). A create answers 422, so a report
    that said ``admitting`` for this workspace would be wrong in the one direction
    that matters.
    """
    db = admission_database.runtime.foundation.database
    _seed(admission_database)
    db.query(
        "INSERT INTO speech_workspaces "
        "(workspace_id, display_name, workspace_state, created_at_us) "
        "VALUES ('workspace-quota-gap', 'synthetic', 'active', 1)"
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        response = await client.get(REPORT, params={"workspace_id": "workspace-quota-gap"})
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["status"] == "degraded"
        assert _causes(report) == ["workspace_not_configured"]
        assert report["capacity"] is None
        refused = await client.post(
            PREFIX,
            json=_body(workspace_id="workspace-quota-gap"),
            headers={"Idempotency-Key": "synthetic-report-no-quota"},
        )
        assert refused.status_code == 422, refused.text
        assert _error(refused) == ("workspace_not_configured", False)
        assert db.query("SELECT count(*) AS n FROM speech_jobs")[0]["n"] == 0


async def test_an_unreadable_store_is_the_storage_class_alone(
    admission_database: AdmissionDatabase,
) -> None:
    """The report explains a storage fault instead of raising it.

    Revoking the admission role's ``SELECT`` on ``workspace_quotas`` models a read
    the admit identity cannot perform: the report cannot evaluate any other fact, so
    it must not claim one, and the create keeps the storage answer.
    """
    db = admission_database.runtime.foundation.database
    _seed(admission_database)
    assert admission_database.admission_role is not None
    db.query(f"REVOKE SELECT ON workspace_quotas FROM {admission_database.admission_role}")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
        ) as client:
            report = await _report(client)
            assert report["status"] == "degraded"
            assert _causes(report) == ["storage_unavailable"]
            # Nothing else was read, so nothing else is claimed.
            assert report["instances"] == []
            assert report["capacity"] is None
            refused = await client.post(
                PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-report-storage"}
            )
            assert refused.status_code == 503, refused.text
            assert _error(refused) == ("job_unavailable", False)
    finally:
        db.query(f"GRANT SELECT ON workspace_quotas TO {admission_database.admission_role}")


async def test_a_draining_pool_answers_the_same_class_in_the_report_and_the_create(
    admission_database: AdmissionDatabase,
) -> None:
    """The load-bearing case: the report's facts are *loaded* correctly, not just classified.

    The unit equivalence matrix proves the create path and the report share one rule.
    It cannot prove the production loader puts ``pool_state`` into the facts at all --
    the earlier revision read the instance and quota rows and never loaded the pool,
    so it answered ``admitting`` while a create answered 429. This case is the same
    process, one isolated database, one measured state: flip the pool to ``draining``
    and assert the report's cause names exactly the code the create returns.
    """
    db = admission_database.runtime.foundation.database
    _seed(admission_database)
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        healthy = await _report(client)
        assert healthy["status"] == "admitting", healthy
        db.query("UPDATE resource_pools SET pool_state = 'draining'")
        report = await _report(client)
        assert report["status"] == "degraded", report
        # Measured, not assumed: a draining pool makes the instance unselectable, and
        # the create answers its capacity refusal (429) for that -- the pool rule
        # inside admission never runs. The report names the code the create returns.
        assert _causes(report) == ["capacity_exhausted"], report
        refused = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-report-draining"}
        )
        assert refused.status_code == 429, refused.text
        assert _error(refused) == ("job_capacity_exhausted", True)
        assert db.query("SELECT count(*) AS n FROM speech_jobs")[0]["n"] == 0
        # Restoring the pool restores both answers, so the class tracks the state
        # rather than a cached verdict.
        db.query("UPDATE resource_pools SET pool_state = 'active'")
        restored = await _report(client)
        assert restored["status"] == "admitting", restored
