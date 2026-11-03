"""Offline tests for persisted TTS association evidence."""

import hashlib
import importlib.util
import io
import json
import sys
import wave
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/dev-env/verify-file-tts-association.py"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tts_association", PATH)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def snapshot() -> dict[str, list[dict[str, str]]]:
    return {
        "operations": [{"operation_id": "operation-old"}],
        "attempts": [{"operation_id": "operation-old", "attempt_id": "attempt-old"}],
        "leases": [{"lease_id": "lease-old"}],
        "submits": [{"submit_intent_id": "submit-old"}],
    }


def test_sorted_json_roundtrip_and_unique_new_identity() -> None:
    m = module()
    before = snapshot()
    restored = json.loads(json.dumps(before, sort_keys=True))
    after = snapshot()
    after["operations"].append({"operation_id": "operation-new"})
    after["attempts"].append({"operation_id": "operation-new", "attempt_id": "attempt-new"})
    after["leases"].append({"lease_id": "lease-new"})
    after["submits"].append({"submit_intent_id": "submit-new"})
    assert m.delta(restored, after)["attempts"] == ("operation-new", "attempt-new")


@pytest.mark.parametrize(
    "change", ["deleted", "duplicate", "multiple", "wrong_column", "no_change"]
)
def test_snapshot_delta_rejects_ambiguous_or_invalid_rows(change: str) -> None:
    m = module()
    before = snapshot()
    after = snapshot()
    for key, row in {
        "operations": {"operation_id": "operation-new"},
        "attempts": {"operation_id": "operation-new", "attempt_id": "attempt-new"},
        "leases": {"lease_id": "lease-new"},
        "submits": {"submit_intent_id": "submit-new"},
    }.items():
        after[key].append(row)
    if change == "deleted":
        after["leases"].pop(0)
    elif change == "duplicate":
        after["leases"].append(after["leases"][0])
    elif change == "multiple":
        after["leases"].append({"lease_id": "lease-extra"})
    elif change == "wrong_column":
        after["attempts"][0]["unexpected"] = "x"
    else:
        after["submits"].pop()
    with pytest.raises(ValueError):
        m.delta(before, after)


def test_private_output_is_idempotent_but_never_overwrites(tmp_path: Path) -> None:
    m = module()
    target = tmp_path / "evidence.json"
    m.write(target, {"z": 1, "a": 2})
    first = target.read_bytes()
    m.write(target, {"a": 2, "z": 1})
    assert target.read_bytes() == first and target.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        m.write(target, {"a": 3})
    assert target.read_bytes() == first


def test_pcm_and_stop_evidence_reject_inconsistent() -> None:
    m = module()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(bytes([1, 0]) * 10)
    assert len(m.pcm(output.getvalue())) == 20
    with pytest.raises((ValueError, wave.Error, EOFError)):
        m.pcm(output.getvalue()[:20])
    odd = bytearray(output.getvalue())
    odd[40:44] = (21).to_bytes(4, "little")
    odd.extend(b"\x00\x00")
    odd[4:8] = (len(odd) - 8).to_bytes(4, "little")
    with pytest.raises(ValueError):
        m.pcm(bytes(odd))
    evidence = [{"execution_stopped": 1, "resources_released": 1, "observed_at_us": 11}]
    m.validate_stop(evidence, 10)
    for bad in ([], [{**evidence[0], "resources_released": 0}], evidence * 2):
        with pytest.raises(ValueError):
            m.validate_stop(bad, 10)
    with pytest.raises(ValueError):
        m.validate_stop(evidence, 12)


def test_submission_requires_original_identity_and_command_digest() -> None:
    m = module()
    request: dict[str, Any] = {
        "operation_id": "operation",
        "attempt_id": "attempt",
        "lease_id": "lease",
        "submit_intent_id": "submit",
        "voice_revision_id": "voice",
        "command": {"kind": "tts", "text": "synthetic"},
    }
    request["command_digest"] = m.command_digest(request["command"])
    submission = {key: value for key, value in request.items() if key != "command"}
    m.validate_submission(request, submission)
    with pytest.raises(ValueError):
        m.validate_submission(request, {**submission, "lease_id": "other"})
    with pytest.raises(ValueError):
        m.validate_submission({**request, "command": {"kind": "tts", "text": "other"}}, submission)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "generation",
        "readiness",
        "body",
        "plan",
        "command",
        "stop",
        "pcm",
        "gateway",
        "status",
    ],
)
def test_full_after_uses_pinned_identity_and_original_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    m = module()
    from speechnexus_backend.speech.application.transcription import AdmittedExecution
    from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat, SpeechPlan
    from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    evidence = tmp_path / "sdk-evidence"
    evidence.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    identity = {"database": "database", "schema": "schema", "installation_id": "installation"}
    readiness = {
        "instance_id": "instance-test",
        "instance_generation": 2,
        "verified_at_us": 1,
        "expires_at_us": 2,
    }
    request_id = "request-test"
    ident, _, _, _, submit = m._identities(request_id)
    plan = SpeechPlan(
        request_id=request_id,
        model_id="binding-test",
        workspace_id="workspace-test",
        binding_revision_id="binding-revision",
        workspace_policy_revision=1,
        text="A short English voice test.",
        voice_id="voice-test",
        voice_revision_id="voice-revision",
        voice_revision=1,
        response_format=SpeechFormat.WAV,
        sample_rate_hz=24000,
        channels=1,
        speed=1.0,
        language="en",
    )
    admitted = AdmittedExecution(
        submit,
        "lease-test",
        ident.operation_id,
        ident.attempt_id,
        "stage-test",
        "workspace-test",
        "tts-batch",
        "node-test",
        "gateway-test",
        "instance-test",
        2,
        "binding-revision",
        "policy-test",
        1,
        1792756800000000,
    )
    request: Any = build_tts_submit(plan, admitted)
    if fault == "command":
        request["command"]["text"] = "different"
        request["command_digest"] = m.command_digest(request["command"])
    business: dict[str, Any] = {
        "kind": "tts_file",
        "plan": asdict(plan),
        "gateway_request": request,
    }
    if fault == "plan":
        business["plan"]["text"] = "different"
    raw = json.dumps(business)
    digest = m.command_digest(m.effective_execution_request(request))
    current = {
        "operations": [{"operation_id": ident.operation_id}],
        "attempts": [{"operation_id": ident.operation_id, "attempt_id": ident.attempt_id}],
        "leases": [{"lease_id": "lease-test"}],
        "submits": [{"submit_intent_id": submit}],
    }
    row = {
        "initial_attempt_id": ident.attempt_id,
        "request_document": raw,
        "request_digest": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
    }
    allocation = {
        key: request[key]
        for key in (
            "operation_id",
            "attempt_id",
            "lease_id",
            "stage_id",
            "instance_id",
            "instance_generation",
            "gateway_id",
            "pool_id",
            "host_node_id",
            "fencing_token",
        )
    }
    submission: dict[str, Any] = {
        key: request[key]
        for key in (
            "operation_id",
            "attempt_id",
            "lease_id",
            "submit_intent_id",
            "command_digest",
            "voice_revision_id",
        )
    }
    submission.update(request_digest=digest, dispatched_at_us=10)
    stops = [{"execution_stopped": 1, "resources_released": 1, "observed_at_us": 11}]
    if fault == "generation":
        readiness["instance_generation"] = 3
    if fault == "stop":
        stops[0]["resources_released"] = 0

    class Result:
        def __init__(self, value: Any):
            self.value = value

        def fetchone(self) -> Any:
            return self.value

        def fetchall(self) -> Any:
            return self.value

    class Connection:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def execute(self, query: str, params: Any) -> Any:
            if "FROM admission_operations" in query:
                return Result(row)
            if "FROM admission_leases" in query:
                return Result(allocation)
            if "FROM submit_intents" in query:
                return Result(submission)
            return Result(stops)

    def save(path: Path, value: Any) -> None:
        path.write_text(json.dumps(value, sort_keys=True))
        path.chmod(0o600)

    save(private / "installation.json", identity)
    save(private / "connections.json", {"owner_dsn": "synthetic"})
    save(
        private / "profile-identities.json",
        {
            "instance_id": "instance-test",
            "voice_revision_id": "voice-revision",
            "binding_revision_id": "binding-revision",
        },
    )
    save(private / "binding-created.json", {"binding_id": "binding-test"})
    save(private / "public-phase-readiness.json", readiness)
    save(
        private / "api-context.json",
        {
            "gateway_component_token": "synthetic",
            "object_storage_endpoint": "http://localhost",
            "object_storage_bucket": "synthetic",
            "object_access_key": "synthetic",
            "object_secret_key": "synthetic",
        },
    )
    save(tmp_path / "gateway-port-2.json", {"port": 12345})
    save(
        private / "sdk-association-before.json",
        {
            "identity": identity,
            "snapshot": {key: [] for key in current},
            "readiness": readiness,
            "readiness_digest": "sha256:"
            + hashlib.sha256((private / "public-phase-readiness.json").read_bytes()).hexdigest(),
        },
    )
    if fault == "readiness":
        save(private / "public-phase-readiness.json", {**readiness, "expires_at_us": 3})
    body = {
        "model": "binding-test",
        "input": plan.text,
        "voice": "voice-test",
        "response_format": "wav",
        "speed": 1.0,
        "nexus": {
            "workspace_id": "workspace-test",
            "voice_revision": "1",
            "client_reference": "reference",
            "overrides": {"tts": {"language": "en"}},
        },
    }
    started = {
        "binding_id": "binding-test",
        "voice_id": "voice-test",
        "voice_revision": "1",
        "workspace_id": "workspace-test",
        "client_reference": "reference",
        "request_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest(),
    }
    if fault == "body":
        started["request_sha256"] = "sha256:" + "0" * 64
    save(evidence / "started.json", started)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(bytes([1, 0]) * 10)
    sound = audio.getvalue()
    (evidence / "audio.wav").write_bytes(sound)
    (evidence / "audio.wav").chmod(0o600)
    save(
        evidence / "result.json",
        {
            "outcome": "success",
            "status": 503 if fault == "status" else 200,
            "sha256": "sha256:" + hashlib.sha256(sound).hexdigest(),
        },
    )

    class Gateway:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def query(self, submit_id: str) -> Any:
            return {
                **request,
                "execution_state": "succeeded",
                "instance_generation": 1 if fault == "gateway" else 2,
            }

        async def audio_staging_result(self, submit_id: str) -> Any:
            return {
                "submit_intent_id": submit,
                "request_digest": digest,
                "staging_result": {
                    "request_digest": digest,
                    "reference": {"workspace_id": "workspace-test"},
                    "storage_version": "version-test",
                },
            }

        async def aclose(self) -> None:
            pass

    class Objects:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def read(self, reference: Any, version: str) -> bytes:
            return sound if fault != "pcm" else sound[:-1]

        def close(self) -> None:
            pass

    monkeypatch.setattr(m, "connect", lambda *args: Connection())
    monkeypatch.setattr(m, "snapshot", lambda conn: current)
    monkeypatch.setattr(m, "GatewayExecutionClient", Gateway)
    monkeypatch.setattr(m, "MinioExecutionResultObjects", Objects)
    monkeypatch.setattr(sys, "argv", ["probe", "--phase", "after", "--task-root", str(tmp_path)])
    if fault == "none":
        m.main()
        assert (private / "sdk-association-after.json").exists()
    else:
        with pytest.raises(SystemExit):
            m.main()
        assert not (private / "sdk-association-after.json").exists()
