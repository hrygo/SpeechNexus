"""Real PostgreSQL registration through the operator command and admission."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import rfc8785
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database

from speechnexus_backend.engine_registration import main as register_main
from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.transcription import AdmittedExecution, StagedAudio
from speechnexus_backend.speech.domain.runtime_observations import VerifiedRuntimeReadiness
from speechnexus_backend.speech.domain.transcription import (
    TranscriptFormat,
    TranscriptionPlan,
)
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_to_document,
)
from speechnexus_backend.speech.infrastructure.transcription_admission import (
    PostgresTranscriptionAdmission,
    PostgresTranscriptionAdmissionFacts,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    load_transcription_recipe,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

WORKSPACE = "workspace-default"
DIGEST = "sha256:" + "a" * 64
AUDIO = b"\x00" * 1024


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


@pytest.fixture
def engine_origin() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _recipe_document(
    *,
    instance_id: str = "instance-reg-1",
    model_id: str = "asr-quality",
    node_id: str = "node-reg-1",
    max_slots: int = 2,
) -> dict[str, Any]:
    peak = {
        "unified_physical_memory_bytes": 0,
        "device_memory_bytes": 4_000_000_000,
        "host_memory_bytes": 1_000_000_000,
        "cpu_decode_millicores": 1_000,
        "host_pcm_buffer_bytes": 0,
        "scratch_disk_bytes": 0,
    }
    per_slot = {
        "unified_physical_memory_bytes": 0,
        "device_memory_bytes": 500_000_000,
        "host_memory_bytes": 250_000_000,
        "cpu_decode_millicores": 100,
        "host_pcm_buffer_bytes": 0,
        "scratch_disk_bytes": 0,
    }
    document: dict[str, Any] = {
        "protocol": "transcription_admission_v1",
        "model_id": model_id,
        "binding_publication_id": "binding-publication-1",
        "binding_revision_id": "binding-revision-1",
        "manifest_id": "manifest-1",
        "policy_revision_id": "policy-revision-1",
        "service_class": "batch",
        "admission_expires_at_us": 4_000_000_000_000_000,
        "quota_dimensions": ["concurrent_attempts"],
        "stage": {
            "stage_id": "stage-asr-1",
            "instance_id": instance_id,
            "minimum_slots": 1,
            "reserved_slots": 1,
        },
        "runtime_plan": {
            "binding": {
                "target_node_id": node_id,
                "pool_id": "asr-batch",
                "gateway_id": "gateway-reg-1",
                "approved_template_id": "template-1",
                "binding_revision_id": "binding-revision-1",
                "manifest_id": "manifest-1",
                "config_digest": DIGEST,
            },
            "topology_digest": DIGEST,
            "qualification_digest": DIGEST,
            "memory_architecture": "discrete_device_memory",
            "service_class": "batch",
            "loading_policy": "resident",
            "max_slots": max_slots,
            "realtime_reserved_slots": 0,
            "peak": peak,
            "steady": peak,
            "per_slot": per_slot,
        },
        "asr": {
            "silence_duration_ms": 400,
            "preview_interval_ms": 1_000,
            "temperature": 0.0,
            "final_deadline_ms": 1_000,
            "auxiliary_deadline_ms": 5_000,
        },
    }
    body = rfc8785.dumps(cast(Any, document))
    document["recipe_digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
    return document


def _write_recipe(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)
    return path


def _register_args(
    database: JobDatabase,
    recipe: Path,
    engine_origin: str,
    *,
    generation: int = 1,
) -> list[str]:
    return [
        "--database-url",
        database.url,
        "--schema",
        database.schema,
        "--recipe-file",
        str(recipe),
        "--engine-url",
        engine_origin,
        "--generation",
        str(generation),
        "--workspace",
        WORKSPACE,
        "--max-attempts",
        "8",
        "--owner-id",
        "speechnexus-operator",
        "--transaction-timeout-seconds",
        "10",
    ]


def _run_registration(capsys: pytest.CaptureFixture[str], args: list[str]) -> dict[str, Any]:
    register_main(args)
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast(dict[str, Any], json.loads(captured.out))


def _admit(
    database: JobDatabase,
    recipe: Any,
    *,
    request_id: str,
    submit_intent_id: str,
    asset_id: str,
) -> AdmittedExecution:
    pool = speech_pool(database.url, schema=database.schema, max_size=2)
    pool.open()
    try:
        admission = PostgresTranscriptionAdmission(
            PostgresTranscriptionAdmissionFacts(pool, timeout_seconds=3),
            AdmissionService(PostgresAdmissionTransactions(pool, max_command_bytes=65_536)),
            recipe,
            max_command_bytes=65_536,
        )
        plan = TranscriptionPlan(
            request_id=request_id,
            workspace_id=WORKSPACE,
            model=recipe.model_id,
            binding_publication_id=recipe.binding_publication_id,
            audio=AUDIO,
            audio_media_type="audio/wav",
            audio_sample_rate_hz=16_000,
            audio_channels=1,
            response_format=TranscriptFormat.JSON,
            language="zh",
            timeout_seconds=30,
        )
        staged = StagedAudio(
            asset_id,
            WORKSPACE,
            "transcription-inputs",
            f"private/transcription-inputs/{asset_id}.audio",
            "sha256:" + hashlib.sha256(AUDIO).hexdigest(),
            len(AUDIO),
        )
        return admission.admit(plan, submit_intent_id=submit_intent_id, audio=staged)
    finally:
        pool.close()


def test_command_registers_ready_instance_and_real_admission_succeeds(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    recipe = load_transcription_recipe(recipe_path)

    result = _run_registration(capsys, _register_args(database, recipe_path, engine_origin))

    assert result["outcome"] == "registered"
    assert result["instance_id"] == recipe.instance_id
    assert result["instance_generation"] == 1
    assert result["pool_id"] == "asr-batch"
    assert result["readiness"]["evidence_digest"].startswith("sha256:")
    rows = result["rows"]
    assert isinstance(rows, dict)
    instance = rows["engine_instances"]
    head = rows["runtime_instance_heads"]
    intent = rows["runtime_instance_intents"]
    assert instance["instance_state"] == "ready"
    assert instance["base_budget_kind"] == "steady"
    assert instance["ready_at_us"] is not None
    assert head["current_generation"] == 1
    assert head["current_prepare_intent_id"] == intent["intent_id"]
    assert intent["intent_state"] == "succeeded"
    assert intent["has_observation"] is True and intent["has_readiness"] is True
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1

    admitted = _admit(
        database,
        recipe,
        request_id="request-registration-1",
        submit_intent_id="submit-registration-1",
        asset_id="asset-registration-1",
    )

    assert admitted.instance_id == recipe.instance_id
    assert admitted.instance_generation == 1
    assert admitted.host_node_id == "node-reg-1"
    leases = database.query(
        "SELECT instance_id, instance_generation, pool_id, gateway_id, fencing_token "
        "FROM admission_leases WHERE lease_id = %s",
        (admitted.lease_id,),
    )
    assert leases == [
        {
            "instance_id": recipe.instance_id,
            "instance_generation": 1,
            "pool_id": "asr-batch",
            "gateway_id": "gateway-reg-1",
            "fencing_token": admitted.fencing_token,
        }
    ]


def test_repeat_registration_is_idempotent_and_different_plan_is_rejected(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    args = _register_args(database, recipe_path, engine_origin)
    first = _run_registration(capsys, args)
    _admit(
        database,
        load_transcription_recipe(recipe_path),
        request_id="request-repeat-1",
        submit_intent_id="submit-repeat-1",
        asset_id="asset-repeat-1",
    )
    second = _run_registration(capsys, args)

    assert first["outcome"] == "registered"
    assert second["outcome"] == "already_ready"
    assert second["intent_id"] == first["intent_id"]
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1
    assert database.query("SELECT count(*) AS n FROM runtime_instance_heads")[0]["n"] == 1
    assert database.query("SELECT count(*) AS n FROM runtime_instance_intents")[0]["n"] == 1

    changed = _write_recipe(tmp_path / "changed-recipe.json", _recipe_document(max_slots=1))
    with pytest.raises(SystemExit) as exit_info:
        register_main(_register_args(database, changed, engine_origin))
    assert exit_info.value.code == 4
    error = capsys.readouterr().err
    assert "instance_config_conflict" in error
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1


def test_generation_rollback_is_rejected(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    _run_registration(capsys, _register_args(database, recipe_path, engine_origin))
    database.query(
        "UPDATE runtime_instance_heads SET generation_high_water = 2 WHERE instance_id = %s",
        ("instance-reg-1",),
    )

    with pytest.raises(SystemExit) as exit_info:
        register_main(_register_args(database, recipe_path, engine_origin, generation=2))
    assert exit_info.value.code == 4
    error = capsys.readouterr().err
    assert "stale_instance_generation" in error
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1


def test_expired_readiness_is_refreshed_from_a_new_probe(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    recipe = load_transcription_recipe(recipe_path)
    plan = recipe.approved.stages[0].runtime_plan
    first = _run_registration(capsys, _register_args(database, recipe_path, engine_origin))

    expired = VerifiedRuntimeReadiness(
        plan.binding,
        recipe.instance_id,
        1,
        1,
        plan.topology_digest,
        plan.qualification_digest,
        "sha256:" + "b" * 64,
        1,
        2,
    )
    database.query(
        "UPDATE runtime_instance_intents SET readiness_document = %s, "
        "version = version + 1 WHERE target_node_id = %s AND intent_id = %s",
        (readiness_to_document(expired), "node-reg-1", first["intent_id"]),
    )
    database.query(
        "UPDATE engine_instances SET ready_at_us = 1, version = version + 1 "
        "WHERE instance_id = %s AND instance_generation = 1",
        (recipe.instance_id,),
    )

    refreshed = _run_registration(
        capsys,
        [*_register_args(database, recipe_path, engine_origin), "--readiness-ttl-seconds", "3600"],
    )

    assert refreshed["outcome"] == "already_ready"
    readiness = refreshed["readiness"]
    assert readiness["verified_at_us"] > 2
    assert readiness["expires_at_us"] > readiness["verified_at_us"]
    assert readiness["evidence_digest"].startswith("sha256:")
    instance = refreshed["rows"]["engine_instances"]
    assert instance["ready_at_us"] == readiness["verified_at_us"]
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1
    admitted = _admit(
        database,
        recipe,
        request_id="request-refresh-1",
        submit_intent_id="submit-refresh-1",
        asset_id="asset-refresh-1",
    )
    assert admitted.instance_id == recipe.instance_id
