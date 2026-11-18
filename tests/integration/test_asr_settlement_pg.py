"""Real-PostgreSQL settlement of a synchronous ASR admission lease.

The ``--once`` body is exercised through ``run_once`` with only the external
gateway stubbed; the pool, release transaction and accounting are real.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase

# Importing the fixture registers it; importing helpers reuses the real recipe.
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_transcription_admission_pg import (
    _audio,
    _identity,
    _plan,
    _recipe,
)

from speechnexus_backend.asr_settlement_worker import (
    AsrSettlementWorkerSettings,
    run_once,
)
from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.infrastructure.asr_settlement_schema import (
    asr_settlement_worker_grants,
    asr_settlement_writer_grants,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_sql import (
    ASR_SETTLEMENT_BACKFILL,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.transcription_admission import (
    PostgresTranscriptionAdmission,
    PostgresTranscriptionAdmissionFacts,
)
from speechnexus_backend.speech.infrastructure.transcription_admission_postgres import (
    PostgresTranscriptionAdmissionTransactions,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
DIGEST = "sha256:" + "c" * 64


def _grant_writer(admission: AdmissionDatabase) -> None:
    db = admission.runtime.foundation.database
    role = admission.runtime.foundation.speech_role
    for grant in asr_settlement_writer_grants(db.schema, role):
        db.query(grant)


def _adapter(admission: AdmissionDatabase, recipe: Any) -> tuple[Any, Any]:
    pool = admission.runtime.pool
    assert pool is not None
    port = PostgresTranscriptionAdmissionTransactions(
        pool,
        max_command_bytes=1_048_576,
        timeout_seconds=10,
    )
    facts = PostgresTranscriptionAdmissionFacts(pool, timeout_seconds=3)
    return PostgresTranscriptionAdmission(facts, AdmissionService(port), recipe), port


def _admitted(admission: AdmissionDatabase, request_id: str) -> Any:
    recipe = _recipe(admission)
    plan = _plan(request_id)
    adapter, _ = _adapter(admission, recipe)
    return adapter.admit(plan, submit_intent_id="submit-" + request_id, audio=_audio(plan))


def _synthetic(
    db: Any,
    *,
    kind: str | None,
    lease_state: str,
    request_document: str | None = None,
) -> str:
    token = uuid.uuid4().hex[:16]
    operation_id = ("operation-" if kind is not None else "job-") + token
    attempt_id, lease_id, stage_id = "attempt-" + token, "lease-" + token, "stage-" + token
    instance = db.query(
        "SELECT instance_id, instance_generation, host_node_id, pool_id, gateway_id "
        "FROM engine_instances LIMIT 1"
    )[0]
    document = json.dumps({"kind": kind}, separators=(",", ":")) if kind else request_document
    redacted = document is None
    db.query(
        "INSERT INTO admission_operations (operation_id,workspace_id,request_digest,"
        "request_document,initial_attempt_id,current_attempt_id,recorded_at_us,"
        "command_redacted_at_us,command_deletion_job_id,command_deletion_evidence_digest) "
        "VALUES (%s,'workspace-test',%s,%s,%s,%s,1,%s,%s,%s)",
        (
            operation_id,
            DIGEST,
            document,
            attempt_id,
            attempt_id,
            2 if redacted else None,
            "deletion-job" if redacted else None,
            DIGEST if redacted else None,
        ),
    )
    db.query(
        "INSERT INTO admission_attempts (operation_id,attempt_id,workspace_id,"
        "binding_revision_id,manifest_id,recipe_digest,policy_revision_id,service_class,"
        "request_digest,required_stage_ids) VALUES (%s,%s,'workspace-test',"
        "'binding-test','manifest-test',%s,'policy-test','interactive',%s,%s)",
        (operation_id, attempt_id, DIGEST, DIGEST, json.dumps([stage_id])),
    )
    db.query(
        "INSERT INTO admission_leases (lease_id,operation_id,attempt_id,stage_id,"
        "host_node_id,instance_id,instance_generation,pool_id,gateway_id,fencing_token,"
        "reserved_slots,lease_state,cleanup_state,deadline_at_us) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,7,1,%s,'pending',9000000000000000000)",
        (
            lease_id,
            operation_id,
            attempt_id,
            stage_id,
            instance["host_node_id"],
            instance["instance_id"],
            instance["instance_generation"],
            instance["pool_id"],
            instance["gateway_id"],
            lease_state,
        ),
    )
    return lease_id


def test_admission_writes_the_root_in_the_same_transaction(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _grant_writer(admission)
    result = _admitted(admission, "request-asr-root")
    roots = db.query("SELECT * FROM speech_asr_settlement WHERE lease_id=%s", (result.lease_id,))
    assert len(roots) == 1
    root = roots[0]
    assert (
        root["operation_id"],
        root["attempt_id"],
        root["gateway_id"],
        root["host_node_id"],
    ) == (result.operation_id, result.attempt_id, result.gateway_id, result.host_node_id)
    assert root["next_check_at_us"] == 0
    assert root["settled_at_us"] is None
    lease = db.query(
        "SELECT lease_state FROM admission_leases WHERE lease_id=%s", (result.lease_id,)
    )
    assert lease[0]["lease_state"] == "reserved"


def test_late_admission_failure_leaves_no_root(
    admission_database: AdmissionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _grant_writer(admission)
    recipe = _recipe(admission)
    plan = _plan("request-asr-rollback")
    adapter, port = _adapter(admission, recipe)
    roots_before = db.query("SELECT count(*) AS n FROM speech_asr_settlement")[0]["n"]
    leases_before = db.query("SELECT count(*) AS n FROM admission_leases")[0]["n"]

    def boom(cursor: Any, prepared: Any) -> None:
        raise RuntimeError("synthetic late admission failure")

    monkeypatch.setattr(port, "_recheck_before_commit", boom)
    with pytest.raises(RuntimeError):
        adapter.admit(plan, submit_intent_id="submit-rollback", audio=_audio(plan))
    assert db.query("SELECT count(*) AS n FROM speech_asr_settlement")[0]["n"] == roots_before
    assert db.query("SELECT count(*) AS n FROM admission_leases")[0]["n"] == leases_before
    operation_id, _, _ = _identity(plan.request_id)
    assert (
        db.query(
            "SELECT count(*) AS n FROM admission_operations WHERE operation_id=%s", (operation_id,)
        )[0]["n"]
        == 0
    )


def test_backfill_adopts_only_held_asr_operations(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _grant_writer(admission)
    held = _admitted(admission, "request-asr-held")
    db.query("DELETE FROM speech_asr_settlement WHERE lease_id=%s", (held.lease_id,))
    tts = _synthetic(db, kind="tts_file", lease_state="reserved")
    public = _synthetic(db, kind=None, lease_state="reserved", request_document="not-json")
    released = _synthetic(db, kind="asr_file", lease_state="released")

    db.query(ASR_SETTLEMENT_BACKFILL)
    rows = {
        row["lease_id"]: row["next_check_at_us"]
        for row in db.query("SELECT lease_id, next_check_at_us FROM speech_asr_settlement")
    }
    assert rows.get(held.lease_id) == 0
    assert tts not in rows
    assert public not in rows
    assert released not in rows

    db.query(ASR_SETTLEMENT_BACKFILL)
    assert db.query("SELECT count(*) AS n FROM speech_asr_settlement")[0]["n"] == len(rows)


async def _settle_once(
    db: Any,
    *,
    lease_id: str,
    gateway_id: str,
    host_node_id: str,
) -> tuple[int, int, int]:
    role = "sn_asr_settle_" + uuid.uuid4().hex[:12]
    password = "synthetic_asr_settlement"
    db.query(f'CREATE ROLE "{role}" LOGIN PASSWORD {password!r}')
    try:
        for grant in (
            *disable_settlement_grants(db.schema, role),
            *asr_settlement_worker_grants(db.schema, role),
        ):
            db.query(grant)
        resolved = AsrSettlementWorkerSettings(
            database_url=make_conninfo(db.url, user=role, password=password),
            schema=db.schema,
            gateway_id=gateway_id,
            host_node_id=host_node_id,
        )

        class Gateway:
            async def query(self, submit_intent_id: str) -> object:
                return {"execution_state": "succeeded"}

        return await run_once(resolved, gateway=Gateway())
    finally:
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')


@pytest.mark.asyncio
async def test_succeeded_without_engine_terminal_response_stays_occupied(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _grant_writer(admission)
    result = _admitted(admission, "request-asr-no-proof")
    now = db.query("SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS n")[
        0
    ]["n"]
    deadline = db.query(
        "SELECT deadline_at_us FROM admission_leases WHERE lease_id=%s", (result.lease_id,)
    )[0]["deadline_at_us"]
    db.query(
        "INSERT INTO submit_intents (submit_intent_id,lease_id,operation_id,attempt_id,"
        "command_digest,request_digest,request_document,deadline_at_us,recorded_at_us,"
        "updated_at_us,execution_state,cleanup_state,terminated_at_us) "
        "VALUES ('submit-asr-no-proof',%s,%s,%s,%s,%s,'{}',%s,%s,%s,'succeeded','complete',%s)",
        (
            result.lease_id,
            result.operation_id,
            result.attempt_id,
            DIGEST,
            DIGEST,
            deadline,
            now,
            now,
            now,
        ),
    )
    db.query(
        "INSERT INTO execution_stop_evidence (evidence_id,submit_intent_id,evidence_kind,"
        "evidence_digest,observed_at_us,execution_stopped,resources_released) "
        "VALUES ('evidence-asr-no-proof','submit-asr-no-proof','engine_abort_ack',%s,%s,1,1)",
        (DIGEST, now - 1),
    )
    outcome = await _settle_once(
        db,
        lease_id=result.lease_id,
        gateway_id=result.gateway_id,
        host_node_id=result.host_node_id,
    )
    assert outcome == (1, 0, 1)
    assert (
        db.query("SELECT lease_state FROM admission_leases WHERE lease_id=%s", (result.lease_id,))[
            0
        ]["lease_state"]
        == "reserved"
    )
    assert (
        db.query(
            "SELECT settled_at_us FROM speech_asr_settlement WHERE lease_id=%s",
            (result.lease_id,),
        )[0]["settled_at_us"]
        is None
    )


@pytest.mark.asyncio
async def test_once_settles_the_held_asr_lease_without_double_accounting(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _grant_writer(admission)
    quota_before = db.query(
        "SELECT held_attempts FROM workspace_quotas WHERE workspace_id='workspace-test'"
    )[0]["held_attempts"]
    result = _admitted(admission, "request-asr-once")
    instance_before = (
        db.query(
            "SELECT held_slots FROM engine_instances WHERE instance_id=%s "
            "AND instance_generation=%s",
            (result.instance_id, result.instance_generation),
        )[0]["held_slots"]
        - 1
    )
    now = db.query("SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS n")[
        0
    ]["n"]
    deadline = db.query(
        "SELECT deadline_at_us FROM admission_leases WHERE lease_id=%s", (result.lease_id,)
    )[0]["deadline_at_us"]
    db.query(
        "INSERT INTO submit_intents (submit_intent_id,lease_id,operation_id,attempt_id,"
        "command_digest,request_digest,request_document,deadline_at_us,recorded_at_us,"
        "updated_at_us,execution_state,cleanup_state,terminated_at_us) "
        "VALUES (%s,%s,%s,%s,%s,%s,'{}',%s,%s,%s,'succeeded','complete',%s)",
        (
            "submit-asr-once",
            result.lease_id,
            result.operation_id,
            result.attempt_id,
            DIGEST,
            DIGEST,
            deadline,
            now,
            now,
            now,
        ),
    )
    db.query(
        "INSERT INTO execution_stop_evidence (evidence_id,submit_intent_id,evidence_kind,"
        "evidence_digest,observed_at_us,execution_stopped,resources_released) "
        "VALUES ('evidence-asr-once','submit-asr-once','engine_terminal_response',%s,%s,1,1)",
        (DIGEST, now - 1),
    )
    assert (
        db.query(
            "SELECT held_slots FROM engine_instances WHERE instance_id=%s "
            "AND instance_generation=%s",
            (result.instance_id, result.instance_generation),
        )[0]["held_slots"]
        == instance_before + 1
    )
    assert (
        db.query(
            "SELECT held_attempts FROM workspace_quotas WHERE workspace_id=%s",
            (result.workspace_id,),
        )[0]["held_attempts"]
        == quota_before + 1
    )

    first = await _settle_once(
        db,
        lease_id=result.lease_id,
        gateway_id=result.gateway_id,
        host_node_id=result.host_node_id,
    )
    assert first == (1, 1, 0)
    lease = db.query(
        "SELECT lease_state, budget_released_at_us FROM admission_leases WHERE lease_id=%s",
        (result.lease_id,),
    )[0]
    assert lease["lease_state"] == "released"
    assert lease["budget_released_at_us"] is not None
    released_at = lease["budget_released_at_us"]
    assert (
        db.query(
            "SELECT held_slots FROM engine_instances WHERE instance_id=%s "
            "AND instance_generation=%s",
            (result.instance_id, result.instance_generation),
        )[0]["held_slots"]
        == instance_before
    )
    assert (
        db.query(
            "SELECT held_attempts FROM workspace_quotas WHERE workspace_id=%s",
            (result.workspace_id,),
        )[0]["held_attempts"]
        == quota_before
    )
    root = db.query(
        "SELECT settled_at_us FROM speech_asr_settlement WHERE lease_id=%s",
        (result.lease_id,),
    )[0]
    assert root["settled_at_us"] is not None
    settled_at = root["settled_at_us"]

    second = await _settle_once(
        db,
        lease_id=result.lease_id,
        gateway_id=result.gateway_id,
        host_node_id=result.host_node_id,
    )
    assert second == (0, 0, 0)
    assert (
        db.query(
            "SELECT budget_released_at_us FROM admission_leases WHERE lease_id=%s",
            (result.lease_id,),
        )[0]["budget_released_at_us"]
        == released_at
    )
    assert (
        db.query(
            "SELECT settled_at_us FROM speech_asr_settlement WHERE lease_id=%s",
            (result.lease_id,),
        )[0]["settled_at_us"]
        == settled_at
    )
