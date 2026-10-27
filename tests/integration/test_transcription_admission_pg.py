"""Real PostgreSQL admission for synchronous transcription; no fake transaction claim."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import pytest
from tests.fake.admission import APPROVED
from tests.fake.runtime_intents import REQUEST
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database

from speechnexus_backend.speech.application.admission import (
    AdmissionService,
    AdmissionTransactions,
)
from speechnexus_backend.speech.application.transcription import (
    StagedAudio,
    TranscriptionUnavailableError,
)
from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionResult,
    AdmitRecipe,
    unknown,
)
from speechnexus_backend.speech.domain.transcription import (
    TranscriptFormat,
    TranscriptionPlan,
)
from speechnexus_backend.speech.infrastructure.transcription_admission import (
    LiveTranscriptionFacts,
    PostgresTranscriptionAdmission,
    PostgresTranscriptionAdmissionFacts,
    TranscriptionAdmissionFacts,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    AsrParameters,
    TranscriptionRecipe,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

AUDIO = b"\x00" * 1024
WORKSPACE = "workspace-test"
POOL = "tts-realtime"
HOST = "host-test"
GATEWAY = "gateway-test"
OWNER = "scheduler-test"
FENCING = 7


class FrozenFacts:
    """The live snapshot captured before a competing writer changes the row."""

    def __init__(self, facts: LiveTranscriptionFacts) -> None:
        self._facts = facts

    def read(self, plan: TranscriptionPlan, recipe: TranscriptionRecipe) -> LiveTranscriptionFacts:
        return self._facts


class AmbiguousAdmission(AdmissionService):
    """Commit ambiguity followed by the real original-identity query."""

    def __init__(self, transactions: AdmissionTransactions) -> None:
        super().__init__(transactions)
        self.admit_calls = 0
        self.query_calls = 0

    def admit(self, command: AdmitRecipe) -> AdmissionResult:
        self.admit_calls += 1
        return unknown(command.request.identity)

    def query(
        self, identity: AdmissionIdentity, *, request_digest: str | None = None
    ) -> AdmissionResult:
        self.query_calls += 1
        return super().query(identity, request_digest=request_digest)


def _plan(request_id: str) -> TranscriptionPlan:
    return TranscriptionPlan(
        request_id=request_id,
        workspace_id=WORKSPACE,
        model="asr-quality",
        binding_publication_id="binding-test",
        audio=AUDIO,
        audio_media_type="audio/wav",
        audio_sample_rate_hz=16_000,
        audio_channels=1,
        response_format=TranscriptFormat.JSON,
        language="auto",
        timeout_seconds=30,
    )


def _audio(plan: TranscriptionPlan) -> StagedAudio:
    return StagedAudio(
        "transcription-input-pg",
        plan.workspace_id,
        "transcription-inputs",
        "private/transcription-inputs/pg.audio",
        "sha256:" + hashlib.sha256(plan.audio).hexdigest(),
        len(plan.audio),
    )


def _recipe(admission: AdmissionDatabase) -> TranscriptionRecipe:
    return TranscriptionRecipe(
        model_id="asr-quality",
        binding_publication_id="binding-test",
        stage_id="stage-main",
        instance_id=REQUEST.instance_id,
        reserved_slots=1,
        asr=AsrParameters(400, 1_000, 0.0, 5_000, 1_000),
        approved=replace(
            APPROVED,
            admission_expires_at_us=admission.runtime.now() + 60_000_000,
        ),
    )


def _facts(admission: AdmissionDatabase) -> PostgresTranscriptionAdmissionFacts:
    pool = admission.runtime.pool
    assert pool is not None, "PostgreSQL admission requires the runtime pool"
    return PostgresTranscriptionAdmissionFacts(pool, timeout_seconds=3)


def _adapter(
    admission: AdmissionDatabase,
    recipe: TranscriptionRecipe,
    *,
    facts: TranscriptionAdmissionFacts | None = None,
    service: AdmissionService | None = None,
) -> PostgresTranscriptionAdmission:
    return PostgresTranscriptionAdmission(
        facts if facts is not None else _facts(admission),
        service if service is not None else AdmissionService(admission.port),
        recipe,
    )


def _counts(db: Any) -> tuple[int, int, int]:
    return (
        db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"],
        db.query("SELECT count(*) AS n FROM admission_leases")[0]["n"],
        db.query("SELECT count(*) AS n FROM coordination_jobs")[0]["n"],
    )


def _identity(request_id: str) -> tuple[str, str, str]:
    source = ("synchronous-transcription:" + request_id).encode()
    hexdigest = hashlib.sha256(source).hexdigest()[:48]
    return "operation-" + hexdigest, "attempt-" + hexdigest, "job-" + hexdigest


def test_live_postgres_admission_writes_one_matching_job_and_lease(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    recipe = _recipe(admission)
    plan = _plan("request-pg-admission")
    operation_id, attempt_id, job_id = _identity(plan.request_id)
    quota_before = db.query(
        "SELECT version, held_attempts FROM workspace_quotas WHERE workspace_id = %s",
        (WORKSPACE,),
    )[0]
    pool_before = db.query("SELECT owner_id FROM resource_pools WHERE pool_id = %s", (POOL,))[0]

    result = _adapter(admission, recipe).admit(
        plan, submit_intent_id="submit-pg-admission", audio=_audio(plan)
    )

    assert pool_before == {"owner_id": OWNER}
    assert (result.operation_id, result.attempt_id) == (operation_id, attempt_id)
    assert result.workspace_id == plan.workspace_id == WORKSPACE
    assert (result.pool_id, result.host_node_id, result.gateway_id) == (
        POOL,
        HOST,
        GATEWAY,
    )
    assert (result.instance_id, result.instance_generation) == (REQUEST.instance_id, 1)
    assert result.fencing_token == FENCING

    operations = db.query(
        "SELECT * FROM admission_operations WHERE operation_id = %s", (operation_id,)
    )
    assert len(operations) == 1
    assert operations[0]["workspace_id"] == WORKSPACE
    assert operations[0]["initial_attempt_id"] == attempt_id
    assert operations[0]["current_attempt_id"] == attempt_id

    leases = db.query("SELECT * FROM admission_leases WHERE lease_id = %s", (result.lease_id,))
    assert len(leases) == 1
    lease = leases[0]
    assert (lease["operation_id"], lease["attempt_id"]) == (operation_id, attempt_id)
    assert lease["pool_id"] == result.pool_id == POOL
    assert lease["host_node_id"] == result.host_node_id == HOST
    assert lease["gateway_id"] == result.gateway_id == GATEWAY
    assert lease["instance_id"] == result.instance_id == REQUEST.instance_id
    assert lease["instance_generation"] == result.instance_generation == 1
    assert lease["fencing_token"] == result.fencing_token == FENCING
    assert lease["deadline_at_us"] == result.deadline_at_us

    jobs = db.query("SELECT * FROM coordination_jobs WHERE operation_id = %s", (operation_id,))
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job_id
    assert jobs[0]["operation_id"] == result.operation_id
    assert jobs[0]["attempt_id"] == result.attempt_id
    assert jobs[0]["request_digest"] == operations[0]["request_digest"]

    quota_after = db.query(
        "SELECT version, held_attempts FROM workspace_quotas WHERE workspace_id = %s",
        (WORKSPACE,),
    )[0]
    assert quota_after["version"] == quota_before["version"] + 1
    assert quota_after["held_attempts"] == quota_before["held_attempts"] + 1


def test_stale_pool_owner_is_refused_with_stale_fencing_token(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    recipe = _recipe(admission)
    plan = _plan("request-pg-fencing")
    stale = _facts(admission).read(plan, recipe)

    db.query(
        "UPDATE resource_pools SET owner_id = %s, fencing_token = fencing_token + 1, "
        "version = version + 1 WHERE pool_id = %s",
        ("scheduler-rotated", POOL),
    )
    before = _counts(db)

    adapter = _adapter(admission, recipe, facts=FrozenFacts(stale))
    with pytest.raises(TranscriptionUnavailableError, match="stale_fencing_token"):
        adapter.admit(plan, submit_intent_id="submit-pg-fencing", audio=_audio(plan))

    assert _counts(db) == before


def test_stale_instance_generation_is_refused_before_any_execution(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    recipe = _recipe(admission)
    plan = _plan("request-pg-generation")
    stale = _facts(admission).read(plan, recipe)

    instance = db.query(
        "SELECT * FROM engine_instances WHERE instance_id = %s AND instance_generation = 1",
        (REQUEST.instance_id,),
    )[0]
    columns = list(instance)
    advanced = {
        **instance,
        "instance_generation": 2,
        "version": int(instance["version"]) + 1,
    }
    db.query(
        f"INSERT INTO engine_instances ({', '.join(columns)}) "
        f"VALUES ({', '.join('%s' for _ in columns)})",
        tuple(advanced[column] for column in columns),
    )
    db.query(
        "UPDATE runtime_instance_heads SET current_generation = 2, "
        "generation_high_water = 2, version = version + 1 WHERE instance_id = %s",
        (REQUEST.instance_id,),
    )
    before = _counts(db)

    adapter = _adapter(admission, recipe, facts=FrozenFacts(stale))
    with pytest.raises(TranscriptionUnavailableError, match="recipe_mismatch"):
        adapter.admit(plan, submit_intent_id="submit-pg-generation", audio=_audio(plan))

    assert _counts(db) == before
    assert db.query(
        "SELECT current_generation FROM runtime_instance_heads WHERE instance_id = %s",
        (REQUEST.instance_id,),
    ) == [{"current_generation": 2}]


def test_unknown_outcome_queries_the_original_identity_without_new_execution(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    recipe = _recipe(admission)
    plan = _plan("request-pg-unknown")
    audio = _audio(plan)
    original = _adapter(admission, recipe).admit(
        plan, submit_intent_id="submit-pg-unknown", audio=audio
    )
    before = _counts(db)

    service = AmbiguousAdmission(admission.port)
    recovered = _adapter(admission, recipe, service=service).admit(
        plan, submit_intent_id="submit-pg-unknown", audio=audio
    )

    assert (service.admit_calls, service.query_calls) == (1, 1)
    assert _counts(db) == before
    assert recovered.submit_intent_id == original.submit_intent_id == "submit-pg-unknown"
    assert recovered.operation_id == original.operation_id
    assert recovered.attempt_id == original.attempt_id
    assert recovered.lease_id == original.lease_id
    assert recovered.instance_generation == original.instance_generation == 1
    assert recovered.fencing_token == original.fencing_token == FENCING
