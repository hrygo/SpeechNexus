"""Private qualification admission on real PostgreSQL; all facts are synthetic."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, cast

import pytest
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_transcription_disable_barrier_pg import _seed
from tests.integration.test_tts_durable_pg import database as database
from tests.unit.backend.test_tts_durable_intent import plan
from tests.unit.test_file_tts_artifacts import tts_inputs as tts_inputs

from speechnexus_backend.speech.application.speech_synthesis import SpeechUnavailableError
from speechnexus_backend.speech.domain.admission import AdmitRecipe, rejected
from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat
from speechnexus_backend.speech.infrastructure.admission_codec import (
    business_command_from_value,
    prepare_admission,
)
from speechnexus_backend.speech.infrastructure.qualification_admission_grants import (
    qualification_admission_grants,
)
from speechnexus_backend.speech.infrastructure.qualification_admission_postgres import (
    PostgresQualificationAdmission,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import command_digest
from speechnexus_backend.speech.infrastructure.tts_admission import (
    PostgresOriginalTtsCommands,
    PostgresTtsAdmission,
    PostgresTtsFacts,
    TtsRecipe,
)

pytestmark = pytest.mark.integration


class Capture:
    command: AdmitRecipe

    def admit(self, command: AdmitRecipe) -> Any:
        self.command = command
        return rejected(command.request.identity, "policy_or_resource_disabled")


def _command(admission: AdmissionDatabase) -> AdmitRecipe:
    pool = admission.runtime.pool
    assert pool is not None
    capture = Capture()
    recipe = TtsRecipe(
        "model-test",
        "stage-main",
        "instance-new",
        1,
        replace(
            admission.command.approved_recipe,
            admission_expires_at_us=admission.runtime.now() + 60_000_000,
        ),
    )
    adapter = PostgresTtsAdmission(
        PostgresTtsFacts(pool), cast(Any, capture), PostgresOriginalTtsCommands(pool), recipe
    )
    probe = replace(
        plan(),
        text="SpeechNexus qualification probe.",
        response_format=SpeechFormat.WAV,
        max_audio_ms=10000,
    )
    with pytest.raises(SpeechUnavailableError):
        adapter.admit(probe)
    body = json.loads(capture.command.business_command.document)
    body["kind"] = "builtin_voice_qualification_probe"
    business = business_command_from_value(body, max_command_bytes=1_048_576)
    job = replace(capture.command.request.job, request_digest=business.request_digest)
    request = replace(capture.command.request, job=job)
    return AdmitRecipe(request, business, capture.command.approved_recipe)


@pytest.mark.parametrize("expiry_case", ["none", "source", "authorization"])
def test_private_authorized_probe_uses_regular_admission_and_replays(
    admission_database: AdmissionDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tts_inputs: dict[str, Any],
    expiry_case: str,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _seed(admission)
    pool = admission.runtime.pool
    assert pool is not None
    command = _command(admission)
    prepared = prepare_admission(command, max_command_bytes=1_048_576)
    gateway = json.loads(command.business_command.document)["gateway_request"]
    stage = command.request.stages[0]
    instance = db.query(
        "SELECT approved_template_id,config_digest FROM engine_instances "
        "WHERE instance_id=%s AND instance_generation=%s",
        (stage.instance_id, stage.instance_generation),
    )[0]
    digest = "sha256:" + "1" * 64
    import hashlib

    from speechnexus_backend.qualification_probe_artifacts import verify_local_tts_artifacts

    pin_bytes = tts_inputs["pin_path"].read_bytes()
    artifacts = verify_local_tts_artifacts(
        pin_path=tts_inputs["pin_path"],
        model_root=tts_inputs["model_root"],
        repository_root=tts_inputs["repository_root"],
        expected_pin_digest="sha256:" + hashlib.sha256(pin_bytes).hexdigest(),
    )
    for grant in qualification_admission_grants(
        db.schema, admission.runtime.foundation.speech_role
    ):
        db.query(grant)
    port = PostgresQualificationAdmission(
        pool, authorization_id="auth-test", max_command_bytes=1_048_576, timeout_seconds=10
    )
    assert port.admit_recipe(command).outcome == "rejected"
    db.query(
        "INSERT INTO speech_builtin_voice_source_approvals "
        "(source_approval_id,voice_id,source_revision,source_digest,native_speaker_id,expires_at) "
        "VALUES ('source-test','voice-test','rev-test',%s,'Vivian',"
        "clock_timestamp()+interval '1 hour')",
        (digest,),
    )
    db.query(
        "UPDATE speech_binding_publication_evidence "
        "SET capacity_manifest_digest=%s, qualification_report_digest=%s "
        "WHERE object_id='asr-quality'",
        (digest, digest),
    )
    observation_row = db.query(
        "SELECT r.observation_document FROM runtime_instance_heads h "
        "JOIN runtime_instance_intents r ON r.instance_id=h.instance_id "
        "AND r.instance_generation=h.current_generation "
        "AND r.intent_id=h.current_prepare_intent_id WHERE h.instance_id=%s",
        (stage.instance_id,),
    )[0]
    observation = json.loads(observation_row["observation_document"])
    observation["native_runtime_evidence"] = {
        "image_digest": artifacts.expected_image_digest,
        "template_digest": "sha256:" + "2" * 64,
        "controller_artifact_digest": "sha256:" + "3" * 64,
        "native_auth_kind": "vllm_api_key_v1",
        "observed_at": observation["updated_at"],
    }
    db.query(
        "UPDATE runtime_instance_intents SET observation_document=%s "
        "WHERE instance_id=%s AND instance_generation=%s AND action='prepare'",
        (json.dumps(observation), stage.instance_id, stage.instance_generation),
    )
    auth = {
        "database": db.query("SELECT current_database() AS name")[0]["name"],
        "schema": db.schema,
        "installation_id": str(
            db.query("SELECT installation_id FROM speech_publication_installation")[0][
                "installation_id"
            ]
        ),
        "purpose": "builtin_voice_qualification",
        "authorization_id": "auth-test",
        "source_approval_id": "source-test",
        "voice_id": "voice-test",
        "voice_revision": "2",
        "voice_revision_id": "voice-revision-test",
        "native_speaker_id": "Vivian",
        "binding_id": "asr-quality",
        "binding_revision_id": command.request.binding_revision_id,
        "manifest_id": command.request.manifest_id,
        "policy_revision_id": command.request.policy_revision_id,
        "gateway_policy_digest": "sha256:" + "a" * 64,
        "host_node_id": stage.host_node_id,
        "gateway_id": stage.gateway_id,
        "instance_id": stage.instance_id,
        "instance_generation": stage.instance_generation,
        "operation_id": command.request.identity.operation_id,
        "attempt_id": command.request.identity.attempt_id,
        "command_digest": gateway["command_digest"],
        "gateway_request_digest": command_digest(gateway),
        "admission_request_digest": prepared.request_digest,
        "business_request_digest": command.business_command.request_digest,
        "expires_at_us": admission.runtime.now() + 60_000_000,
        "manifest_digest": digest,
        "report_id": "qualification-test",
        "report_digest": digest,
        "model_artifact_digest": artifacts.model_artifact_digest,
        "image_digest": artifacts.expected_image_digest,
        "template_id": instance["approved_template_id"],
        "template_config_digest": instance["config_digest"],
        "template_digest": "sha256:" + "2" * 64,
        "controller_artifact_digest": "sha256:" + "3" * 64,
        "controller_revision": "controller-test",
        "native_authority": True,
    }
    bundle = _real_tts_bundle(admission, auth, tts_inputs, artifacts)
    _issue_real_authorization(admission, auth, observation, tts_inputs, bundle, monkeypatch)
    missing_native = dict(observation)
    missing_native.pop("native_runtime_evidence")
    db.query(
        "UPDATE runtime_instance_intents SET observation_document=%s "
        "WHERE instance_id=%s AND instance_generation=%s AND action='prepare'",
        (json.dumps(missing_native), stage.instance_id, stage.instance_generation),
    )
    assert port.admit_recipe(command).outcome == "rejected"
    assert not db.query("SELECT 1 FROM admission_operations")
    db.query(
        "UPDATE runtime_instance_intents SET observation_document=%s "
        "WHERE instance_id=%s AND instance_generation=%s AND action='prepare'",
        (json.dumps(observation), stage.instance_id, stage.instance_generation),
    )
    for patch in (
        {"gateway_request_digest": "sha256:" + "0" * 64},
        {"instance_generation": stage.instance_generation + 1},
        {"native_authority": False},
        {"expires_at_us": 1},
        {"installation_id": "wrong-installation"},
        {"image_digest": "sha256:" + "0" * 64},
        {"template_digest": "sha256:" + "0" * 64},
        {"controller_artifact_digest": "sha256:" + "0" * 64},
    ):
        db.query(
            "UPDATE speech_builtin_voice_probe_authorizations SET intent=%s::jsonb "
            "WHERE authorization_id='auth-test'",
            (json.dumps(auth | patch),),
        )
        assert port.admit_recipe(command).outcome == "rejected"
        assert not db.query("SELECT 1 FROM speech_builtin_voice_probe_admissions")
        assert not db.query("SELECT 1 FROM admission_operations")
    db.query(
        "UPDATE speech_builtin_voice_probe_authorizations SET intent=%s::jsonb "
        "WHERE authorization_id='auth-test'",
        (json.dumps(auth),),
    )
    if expiry_case != "none":
        _assert_final_lock_expiry_rolls_back(admission, port, command, expiry_case)
        return
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(port.admit_recipe, command) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
        outcomes = sorted(result.outcome for result in results)
    assert outcomes == ["committed", "replayed"], results
    assert not db.query("SELECT 1 FROM speech_publications WHERE kind='voice'")
    assert len(db.query("SELECT 1 FROM speech_builtin_voice_probe_admissions")) == 1
    evidence = _exercise_original_gateway_recovery(admission, port, command)
    _exercise_approval_transaction(admission, auth, evidence, artifacts, bundle)
    db.query(
        "INSERT INTO speech_builtin_voice_source_revocations (source_approval_id,reason) "
        "VALUES ('source-test','revoked')"
    )
    assert port.admit_recipe(command).outcome == "replayed"
    assert port.query_admission(command.request.identity).outcome == "replayed"
    _exercise_approval_replay(admission, auth, evidence, artifacts, bundle)


def _assert_final_lock_expiry_rolls_back(
    admission: AdmissionDatabase,
    port: PostgresQualificationAdmission,
    command: AdmitRecipe,
    expiry_case: str,
) -> None:
    import time

    db = admission.runtime.foundation.database
    if expiry_case == "source":
        db.query(
            "UPDATE speech_builtin_voice_source_approvals SET expires_at="
            "clock_timestamp()+interval '3 seconds' WHERE source_approval_id='source-test'"
        )
        expired_sql = (
            "SELECT expires_at<=clock_timestamp() AS expired "
            "FROM speech_builtin_voice_source_approvals WHERE source_approval_id='source-test'"
        )
    else:
        db.query(
            "UPDATE speech_builtin_voice_probe_authorizations SET intent=jsonb_set(intent,"
            "'{expires_at_us}',to_jsonb((extract(epoch from clock_timestamp()+"
            "interval '3 seconds')*1000000)::bigint)) WHERE authorization_id='auth-test'"
        )
        expired_sql = (
            "SELECT (intent->>'expires_at_us')::bigint <= "
            "floor(extract(epoch from clock_timestamp())*1000000)::bigint AS expired "
            "FROM speech_builtin_voice_probe_authorizations WHERE authorization_id='auth-test'"
        )
    before = admission.snapshot()
    pools_before = db.query("SELECT * FROM resource_pools ORDER BY pool_id")
    blocker = db.blocker()
    try:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('speechnexus:tts-cancel:' "
            "|| current_schema() || ':' || %s, 0))",
            (command.request.identity.operation_id,),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(port.admit_recipe, command)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if db.query(
                    "SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (blocker.info.backend_pid,),
                ):
                    break
                if pending.done():
                    pytest.fail(f"admission did not wait: {pending.result()}")
                time.sleep(0.01)
            else:
                pytest.fail("admission did not wait on settlement lock")
            while not db.query(expired_sql)[0]["expired"]:
                if time.monotonic() >= deadline:
                    pytest.fail("authority did not expire while blocked")
                time.sleep(0.02)
            blocker.commit()
            assert pending.result(timeout=10).outcome == "rejected"
        assert admission.snapshot() == before
        assert db.query("SELECT * FROM resource_pools ORDER BY pool_id") == pools_before
        assert not db.query("SELECT 1 FROM speech_builtin_voice_probe_admissions")
        assert not db.query("SELECT 1 FROM speech_tts_settlement")
    finally:
        blocker.close()


def _exercise_original_gateway_recovery(
    admission: AdmissionDatabase, port: PostgresQualificationAdmission, command: AdmitRecipe
) -> Any:
    """Synthetic gateway transport; admission and terminal ledger remain real PostgreSQL."""
    import hashlib
    import io
    import wave

    from speechnexus_contracts.execution_canonical import effective_execution_request

    from speechnexus_backend.qualification_probe_execution import (
        ProbeExecutionError,
        run_original_probe,
    )
    from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
        GatewayRejectedError,
    )

    db = admission.runtime.foundation.database
    request = json.loads(command.business_command.document)["gateway_request"]
    pcm = b"\x00\x00" * 240
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(pcm)
    wav = buffer.getvalue()
    digest = "sha256:" + hashlib.sha256(wav).hexdigest()
    now = admission.runtime.now()
    db.query(
        "INSERT INTO submit_intents (submit_intent_id,lease_id,operation_id,attempt_id,"
        "command_digest,request_digest,request_document,deadline_at_us,recorded_at_us,"
        "updated_at_us,execution_state,cleanup_state,output_stream_id,sample_rate_hz,"
        "hard_output_ceiling_samples,generated_pcm_samples,voice_revision_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'succeeded','released',"
        "'stream-probe',24000,240000,240,%s)",
        (
            request["submit_intent_id"],
            request["lease_id"],
            request["operation_id"],
            request["attempt_id"],
            request["command_digest"],
            digest,
            "{}",
            now + 60_000_000,
            now,
            now,
            request["voice_revision_id"],
        ),
    )
    db.query(
        "INSERT INTO execution_stop_evidence (evidence_id,submit_intent_id,evidence_kind,"
        "evidence_digest,observed_at_us,execution_stopped,resources_released) "
        "VALUES ('evidence-probe',%s,'engine_terminal_response',%s,%s,1,1)",
        (request["submit_intent_id"], digest, now),
    )

    class Gateway:
        submitted = False
        submits = 0

        async def query_tts_configuration_evidence(self) -> dict[str, Any]:
            return {
                "gateway_policy_digest": "sha256:" + "a" * 64,
                "native_speaker_id": "Vivian",
                "voice_id": "voice-test",
                "voice_revision": "2",
                "voice_revision_id": request["voice_revision_id"],
                "policy_revision_id": request["policy_revision_id"],
                "binding_revision_id": request["binding_revision_id"],
                "node_id": request["host_node_id"],
                "gateway_id": request["gateway_id"],
                "instance_id": request["instance_id"],
                "instance_generation": request["instance_generation"],
            }

        async def query(self, _: str) -> dict[str, Any]:
            if not self.submitted:
                raise GatewayRejectedError("missing", request_may_have_run=False, status_code=404)
            return {**request, "execution_state": "succeeded"}

        async def submit(self, value: dict[str, Any]) -> dict[str, Any]:
            assert value == request
            self.submitted = True
            self.submits += 1
            return {**request, "execution_state": "succeeded"}

        async def audio_staging_result(self, _: str) -> dict[str, Any]:
            request_digest = command_digest(effective_execution_request(request))
            return {
                "submit_intent_id": request["submit_intent_id"],
                "request_digest": request_digest,
                "staging_result": {
                    "request_digest": request_digest,
                    "media_type": "audio/wav",
                    "reference": {"workspace_id": request["workspace_id"], "byte_length": len(wav)},
                    "storage_version": "version-test",
                    "audio": {
                        "sample_rate_hz": 24000,
                        "channels": 1,
                        "source_pcm_samples": len(pcm) // 2,
                        "duration_ms": 10,
                        "source_pcm_digest": "sha256:" + hashlib.sha256(pcm).hexdigest(),
                    },
                },
            }

        async def aclose(self) -> None:
            pass

    class Objects:
        def read(self, reference: Any, storage_version: str) -> bytes:
            assert reference["byte_length"] == len(wav) and storage_version == "version-test"
            return wav

        def close(self) -> None:
            pass

    recipe = TtsRecipe("model-test", "stage-main", "instance-new", 1, command.approved_recipe)
    assert admission.runtime.pool is not None
    gateway = Gateway()
    evidence = run_original_probe(
        pool=admission.runtime.pool,
        port=port,
        recipe=recipe,
        request_id="request-test",
        gateway=cast(Any, gateway),
        objects=Objects(),
        timeout_seconds=3,
    )
    assert (
        evidence.audio_sha256 == digest
        and evidence.generated_samples == 240
        and gateway.submits == 1
    )
    db.query(
        "UPDATE execution_stop_evidence SET evidence_kind='abort_requested' "
        "WHERE evidence_id='evidence-probe'"
    )
    with pytest.raises(ProbeExecutionError, match="probe_terminal_evidence_unavailable"):
        run_original_probe(
            pool=admission.runtime.pool,
            port=port,
            recipe=recipe,
            request_id="request-test",
            gateway=cast(Any, gateway),
            objects=Objects(),
            timeout_seconds=3,
        )
    assert gateway.submits == 1
    db.query(
        "UPDATE execution_stop_evidence SET evidence_kind='engine_terminal_response' "
        "WHERE evidence_id='evidence-probe'"
    )
    return evidence


def _approval_call(
    admission: AdmissionDatabase,
    auth: dict[str, Any],
    evidence: Any,
    artifacts: Any,
    bundle: dict[str, bytes],
) -> str:
    from speechnexus_backend.qualification_probe_approval import _commit_verified_probe
    from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

    assert admission.runtime.pool is not None
    return _commit_verified_probe(
        PostgresPublications(admission.runtime.pool, timeout_seconds=10),
        authorization_id="auth-test",
        approval_id="approval-probe",
        probe=evidence,
        artifacts=artifacts,
        **bundle,
        original_identity=_original_identity(auth),
    )


def _original_identity(auth: dict[str, Any]) -> dict[str, str]:
    return {
        **{
            key: auth[key]
            for key in (
                "operation_id",
                "attempt_id",
                "admission_request_digest",
                "business_request_digest",
                "gateway_request_digest",
            )
        },
        "intent_file_sha256": "sha256:" + "4" * 64,
        "recipe_file_sha256": "sha256:" + "5" * 64,
    }


def _exercise_approval_transaction(
    admission: AdmissionDatabase,
    auth: dict[str, Any],
    evidence: Any,
    artifacts: Any,
    bundle: dict[str, bytes],
) -> None:
    """Synthetic raw packet; actual release verifier and PG authority are used."""

    from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
    from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
        qualification_writer_grants,
    )
    from speechnexus_backend.speech.infrastructure.qualification_admission_grants import (
        qualification_authorizer_grants,
    )

    db = admission.runtime.foundation.database
    role = admission.runtime.foundation.speech_role
    for grant in (
        *qualification_writer_grants(db.schema, role),
        *qualification_authorizer_grants(db.schema, role),
    ):
        db.query(grant)
    with pytest.raises(PublicationUnavailableError):
        _approval_call(
            admission, auth, evidence, artifacts, bundle | {"report": bundle["report"] + b"x"}
        )
    assert not db.query("SELECT 1 FROM speech_builtin_voice_approvals")

    def commit_then_lose_reply() -> None:
        assert _approval_call(admission, auth, evidence, artifacts, bundle) == "committed"
        raise ConnectionError("synthetic lost commit reply")

    with pytest.raises(ConnectionError, match="synthetic lost commit reply"):
        commit_then_lose_reply()
    assert (
        len(
            db.query(
                "SELECT 1 FROM speech_builtin_voice_approvals WHERE approval_id='approval-probe'"
            )
        )
        == 1
    )


def _exercise_approval_replay(
    admission: AdmissionDatabase,
    auth: dict[str, Any],
    evidence: Any,
    artifacts: Any,
    bundle: dict[str, bytes],
) -> None:
    from speechnexus_backend.qualification_probe_approval import (
        ProbeApprovalError,
        find_probe_approval,
        result_request,
    )
    from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

    assert _approval_call(admission, auth, evidence, artifacts, bundle) == "replayed"
    assert admission.runtime.pool is not None
    store = PostgresPublications(admission.runtime.pool, timeout_seconds=10)
    request = result_request(
        approval_id="approval-probe",
        authorization_id="auth-test",
        **bundle,
        original_identity=_original_identity(auth),
    )
    # Simulates loss of the commit response: recovery reads only PostgreSQL and
    # therefore works even if the gateway and model are unavailable.
    assert find_probe_approval(store, request)
    with pytest.raises(ProbeApprovalError, match="probe_approval_conflict"):
        find_probe_approval(store, request | {"report_sha256": "sha256:" + "0" * 64})
    with pytest.raises(ProbeApprovalError, match="probe_approval_conflict"):
        find_probe_approval(store, request | {"recipe_file_sha256": "sha256:" + "0" * 64})
    with pytest.raises(ProbeApprovalError, match="probe_approval_conflict"):
        find_probe_approval(store, request | {"intent_file_sha256": "sha256:" + "0" * 64})


def _real_tts_bundle(
    admission: AdmissionDatabase, auth: dict[str, Any], inputs: dict[str, Any], artifacts: Any
) -> dict[str, bytes]:
    import hashlib
    from datetime import UTC, datetime, timedelta

    import rfc8785
    from tests.unit.backend.test_binding_release_evidence import packet

    def canonical(value: Any) -> str:
        return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()

    db = admission.runtime.foundation.database
    bundle = packet()
    manifest, report, definition = bundle["manifest"], bundle["report"], bundle["definition"]
    pin = inputs["pin"]
    manifest["binding_revision_id"] = report["binding_revision_id"] = auth["binding_revision_id"]
    manifest["manifest_id"] = report["capacity_manifest_id"] = auth["manifest_id"]
    report["report_id"] = manifest["qualification"]["qualification_report_id"] = auth["report_id"]
    revision = int(
        db.query(
            "SELECT revision FROM speech_binding_revisions WHERE binding_id=%s AND revision_id=%s",
            (auth["binding_id"], auth["binding_revision_id"]),
        )[0]["revision"]
    )
    report["binding"] = {"id": auth["binding_id"], "revision": str(revision)}
    manifest["supported_tasks"] = definition["tasks"] = ["tts_file"]
    definition["execution_recipe"]["capacity_manifest_id"] = auth["manifest_id"]
    definition["execution_recipe"]["stage_roles"] = ["tts_connector"]
    stage = manifest["stage_graph"][0]
    stage["stage_role"] = "tts_connector"
    stage["model_revision_digest"] = artifacts.model_artifact_digest
    report["software"]["engine_components"][stage["stage_id"]]["image_digest"] = (
        artifacts.expected_image_digest
    )
    expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    report["expires_at"] = manifest["qualification"]["expires_at"] = expiry.isoformat().replace(
        "+00:00", "Z"
    )
    definition["parameter_domains"]["tts"] = {
        "languages": ["en"],
        "audio_formats": ["wav"],
        "sample_rates_hz": [24000],
        "modes": ["complete"],
        "audio_delivery_modes": ["complete"],
        "max_input_codepoints": 400,
    }
    manifest["qualification"]["config_bundle_digest"] = canonical(
        {"artifact_pin": pin, "binding_definition": definition}
    )
    scenarios = json.dumps(bundle["evidence"]).encode()
    report["evidence_digest"] = "sha256:" + hashlib.sha256(scenarios).hexdigest()
    raw_manifest, raw_report = json.dumps(manifest).encode(), json.dumps(report).encode()
    auth["manifest_digest"] = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
    auth["report_digest"] = "sha256:" + hashlib.sha256(raw_report).hexdigest()
    db.query(
        "UPDATE speech_binding_revisions SET definition_json=%s WHERE binding_id=%s "
        "AND revision_id=%s",
        (json.dumps(definition), auth["binding_id"], auth["binding_revision_id"]),
    )
    db.query(
        "UPDATE speech_binding_publication_evidence SET capacity_manifest_id=%s,"
        "qualification_report_id=%s,capacity_manifest_digest=%s,qualification_report_digest=%s "
        "WHERE object_id=%s",
        (
            auth["manifest_id"],
            auth["report_id"],
            auth["manifest_digest"],
            auth["report_digest"],
            auth["binding_id"],
        ),
    )
    return {
        "artifact_pin": inputs["pin_path"].read_bytes(),
        "manifest": raw_manifest,
        "report": raw_report,
        "scenario_evidence": scenarios,
    }


def _issue_real_authorization(
    admission: AdmissionDatabase,
    auth: dict[str, Any],
    observation: dict[str, Any],
    inputs: dict[str, Any],
    bundle: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from speechnexus_backend.qualification_probe_authorizer import (
        ProbeAuthorizationError,
        _canonical_digest,
        issue_probe_authorization,
    )
    from speechnexus_backend.settings import Settings
    from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
    from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
    from speechnexus_backend.speech.infrastructure.qualification_admission_grants import (
        qualification_authorizer_grants,
    )

    db = admission.runtime.foundation.database
    role = admission.runtime.foundation.speech_role
    for grant in qualification_authorizer_grants(db.schema, role):
        db.query(grant)
    template = {
        "format": "speechnexus-template-v1",
        "template": {
            "template_id": auth["template_id"],
            "image_digest": auth["image_digest"],
            "engine_auth": "vllm_api_key_v1",
        },
    }
    controller = {
        "format": "speechnexus-controller-python-v1",
        "files": [["synthetic.py", "a" * 64]],
    }
    auth["template_digest"] = _canonical_digest(template)
    auth["controller_artifact_digest"] = _canonical_digest(controller)
    observation["native_runtime_evidence"]["template_digest"] = auth["template_digest"]
    observation["native_runtime_evidence"]["controller_artifact_digest"] = auth[
        "controller_artifact_digest"
    ]
    db.query(
        "UPDATE runtime_instance_intents SET observation_document=%s WHERE instance_id=%s "
        "AND instance_generation=%s AND action='prepare'",
        (json.dumps(observation), auth["instance_id"], auth["instance_generation"]),
    )

    class Gateway:
        async def query_tts_configuration_evidence(self) -> dict[str, Any]:
            return {
                **auth,
                "node_id": auth["host_node_id"],
                "observed_at": datetime.now(UTC).isoformat(),
            }

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        "speechnexus_backend.qualification_probe_authorizer.GatewayExecutionClient.from_settings",
        lambda _: Gateway(),
    )
    assert admission.runtime.pool is not None
    store = PostgresPublications(admission.runtime.pool, timeout_seconds=10)
    kwargs = {
        "settings": Settings.model_construct(),
        "intent": auth,
        "template_document": json.dumps(template).encode(),
        "controller_inventory": json.dumps(controller).encode(),
        "pin_path": inputs["pin_path"],
        "model_root": inputs["model_root"],
        **bundle,
    }
    with pytest.raises(PublicationUnavailableError):
        issue_probe_authorization(store, **(kwargs | {"report": bundle["report"] + b"x"}))
    with pytest.raises(ProbeAuthorizationError, match="probe_authorization_unverified"):
        issue_probe_authorization(
            store, **(kwargs | {"intent": auth | {"source_approval_id": "unknown-source"}})
        )
    assert not db.query("SELECT 1 FROM speech_builtin_voice_probe_authorizations")
    assert issue_probe_authorization(store, **kwargs) == "committed"
    assert (
        db.query("SELECT approved_by FROM speech_builtin_voice_probe_authorizations")[0][
            "approved_by"
        ]
        == role
    )
    assert issue_probe_authorization(store, **kwargs) == "replayed"
