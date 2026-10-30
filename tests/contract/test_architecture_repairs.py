"""Executable negative cases for the architecture audit; no business services."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, cast

import httpx2
import pytest
import rfc8785
from jsonschema.exceptions import ValidationError
from openai import APIStatusError, OpenAI

from tests.contract.test_p0_public_contracts import (
    _CONTRACTS,
    _component_validator,
    _load_json,
    _resolved_yaml,
    _validator,
)


def _command() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _load_json(_CONTRACTS / "internal/execution-command.schema.json")["examples"][0],
    )


def _receipt() -> dict[str, Any]:
    return {
        "receipt_id": "receipt-demo",
        "requested": {"language": "en"},
        "effective": {"language": "en", "seed": 7},
        "observed": {"sample_rate_hz": 24000},
        "missing": [],
        "unknown": [],
    }


@pytest.mark.parametrize("section", ["requested", "effective", "observed"])
@pytest.mark.parametrize(
    "field",
    [
        "input_text",
        "internal_endpoint",
        "component_credential",
        "vendor",
        "planner",
        "turn_detection",
    ],
)
def test_receipt_rejects_nested_private_data(section: str, field: str) -> None:
    receipt = _receipt()
    receipt[section][field] = {"nested": ["synthetic-private-value"]}
    rest = _component_validator(
        _resolved_yaml(_CONTRACTS / "speech-openapi.yaml"), "ExecutionReceipt"
    )
    realtime = _load_json(_CONTRACTS / "realtime-events.schema.json")
    for validator in (rest, _validator({"$ref": "#/$defs/Receipt", "$defs": realtime["$defs"]})):
        with pytest.raises(ValidationError):
            validator.validate(receipt)


def test_receipt_rejects_private_reference_and_arbitrary_missing_paths() -> None:
    schema = _load_json(_CONTRACTS / "shared/receipts.schema.json")
    validator = _validator({"$ref": "#/$defs/ExecutionReceipt", "$defs": schema["$defs"]})
    receipt = _receipt()
    receipt["effective"]["voice"] = {"id": "voice-demo", "revision": "1", "object_key": "private"}
    with pytest.raises(ValidationError):
        validator.validate(receipt)
    receipt = _receipt()
    receipt["missing"] = ["synthetic private content"]
    with pytest.raises(ValidationError):
        validator.validate(receipt)


@pytest.mark.parametrize("code", ["operation_in_progress", "response_not_replayable"])
def test_sync_recovery_requires_original_identity_and_success_receipt(code: str) -> None:
    schema = _load_json(_CONTRACTS / "shared/errors.schema.json")
    validator = _validator({"$ref": "#/$defs/ErrorResponse", "$defs": schema["$defs"]})
    response: dict[str, Any] = {
        "error": {
            "code": code,
            "type": "api_error",
            "message": "Query original execution.",
            "param": None,
            "request_id": "req-demo",
            "retryable": False,
        }
    }
    with pytest.raises(ValidationError):
        validator.validate(response)
    response["error"]["nexus"] = {"operation_id": "op-original"}
    if code == "response_not_replayable":
        with pytest.raises(ValidationError):
            validator.validate(response)
        response["error"]["nexus"]["receipt"] = _receipt()
    validator.validate(response)
    # An SDK consumer that lost the first response recovers identity from 409.
    calls: list[str] = []

    def transport(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        assert request.headers["Idempotency-Key"] == "synthetic-key"
        return httpx2.Response(409, json=response)

    with (
        OpenAI(
            api_key="synthetic-test",
            base_url="http://mock.invalid/v1",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
        ) as client,
        pytest.raises(APIStatusError) as caught,
    ):
        client.audio.speech.create(
            model="tts",
            input="Synthetic.",
            voice="alloy",
            extra_headers={"Idempotency-Key": "synthetic-key"},
        )
    body = cast(dict[str, Any], caught.value.body)
    assert body["nexus"]["operation_id"] == "op-original"
    assert calls == ["/v1/audio/speech"]
    if code == "response_not_replayable":
        assert body["nexus"]["receipt"] == _receipt()


def test_canonical_command_survives_fake_worker_gateway_adapter_boundaries() -> None:
    command = _command()
    command["parameters"]["instructions"] = "s" * 4096  # Standard TTS must not narrow to 2000.
    validator = _validator(_load_json(_CONTRACTS / "internal/execution-command.schema.json"))
    validator.validate(command)
    digest = "sha256:" + hashlib.sha256(rfc8785.dumps(command)).hexdigest()
    worker_wire = json.dumps({"command": command, "command_digest": digest})
    gateway = json.loads(worker_wire)
    validator.validate(gateway["command"])
    assert "sha256:" + hashlib.sha256(rfc8785.dumps(gateway["command"])).hexdigest() == digest
    adapter = json.loads(json.dumps(gateway["command"]))
    assert adapter["text"] == "Synthetic narration."
    assert adapter["parameters"] == command["parameters"]
    assert adapter["resources"] == command["resources"]
    # Object ordering is irrelevant; any change of content or effective option conflicts.
    assert rfc8785.dumps(dict(reversed(list(command.items())))) == rfc8785.dumps(command)
    gateway["command"]["parameters"]["speed"] = 2
    assert "sha256:" + hashlib.sha256(rfc8785.dumps(gateway["command"])).hexdigest() != digest


@pytest.mark.parametrize(
    "mutation",
    ["no_input", "no_parameter", "extra_parameter", "wrong_stage", "two_inputs", "no_voice"],
)
def test_canonical_command_rejects_incomplete_or_ambiguous_input(mutation: str) -> None:
    command = _command()
    if mutation == "no_input":
        command["text"] = None
    elif mutation == "no_parameter":
        del command["parameters"]["speed"]
    elif mutation == "extra_parameter":
        command["parameters"]["vendor_options"] = {}
    elif mutation == "no_voice":
        command["resources"].update(voice=None, voice_kind=None)
    elif mutation == "wrong_stage":
        command["stage_role"] = "asr_batch"
    else:
        command["text_stream"] = {"stream_id": "stream-demo", "stream_epoch": 1}
    with pytest.raises(ValidationError):
        _validator(_load_json(_CONTRACTS / "internal/execution-command.schema.json")).validate(
            command
        )


def _deletion_validator(name: str) -> Any:
    schema = _load_json(_CONTRACTS / "shared/deletion.schema.json")
    return _validator({"$ref": "#/$defs/" + name, "$defs": schema["$defs"]})


def test_deletion_pins_owner_revision_and_cascade_scope() -> None:
    target = {
        "owner": "speech",
        "kind": "artifact",
        "resource": {"id": "artifact-demo", "revision": "1"},
    }
    request: dict[str, Any] = {
        "plan_id": "plan-demo",
        "expected_plan_revision": "1",
        "plan_digest": "sha256:" + "a" * 64,
        "confirm_targets": [target],
    }
    validator = _deletion_validator("DeletionCreateRequest")
    validator.validate(request)
    for field in request:
        missing = copy.deepcopy(request)
        del missing[field]
        with pytest.raises(ValidationError):
            validator.validate(missing)
    request["confirm_targets"][0]["owner"] = "workbench"
    with pytest.raises(ValidationError):
        validator.validate(request)
    dependency = {
        "target": {
            "owner": "workbench",
            "kind": "saved_asset",
            "resource": {"id": "saved-demo", "revision": "1"},
        },
        "relation": "independent_copy",
        "disposition": "cascade",
    }
    with pytest.raises(ValidationError):
        _deletion_validator("Dependency").validate(dependency)
    dependency["disposition"] = "retain"
    _deletion_validator("Dependency").validate(dependency)


def test_deletion_cannot_claim_success_before_cleanup_and_ledger() -> None:
    deletion: dict[str, Any] = {
        "id": "delete-demo",
        "object": "nexus.deletion",
        "plan_id": "plan-demo",
        "plan_digest": "sha256:" + "a" * 64,
        "status": "queued",
        "cleanup_state": "pending",
        "created_at": "2026-10-23T00:00:00Z",
        "updated_at": "2026-10-23T00:00:00Z",
        "result": None,
        "error": None,
    }
    validator = _deletion_validator("Deletion")
    validator.validate(deletion)
    deletion["status"] = "succeeded"
    with pytest.raises(ValidationError):
        validator.validate(deletion)
    deletion["cleanup_state"] = "complete"
    deletion["result"] = {
        "deleted": [
            {"owner": "speech", "kind": "asset", "resource": {"id": "asset-demo", "revision": "1"}}
        ],
        "retained_independent_copies": [],
        "ledger_id": "ledger-demo",
        "completed_at": "2026-10-23T00:01:00Z",
    }
    validator.validate(deletion)
    deletion["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        validator.validate(deletion)


class _TextProtocolPeer:
    """Contract consumer oracle: retained acks and committed adapter prefixes."""

    def __init__(self, credit: int = 10) -> None:
        self.credit = credit
        self.text = ""
        self.committed = 0
        self.finished = False
        self.seen: dict[int, tuple[bytes, dict[str, Any]]] = {}
        self.adapter_inputs: list[str] = []
        self.validator = _validator(
            _load_json(_CONTRACTS / "internal/execution-stream.schema.json")
        )

    def accept(self, event: dict[str, Any]) -> dict[str, Any]:
        self.validator.validate(event)
        if (
            event["stream_id"],
            event["stream_epoch"],
            event["operation_id"],
            event["attempt_id"],
        ) != ("stream-demo", 1, "op-demo", "att-demo"):
            raise ValueError("stale_epoch")
        payload = {
            key: value
            for key, value in event.items()
            if key
            in {
                "type",
                "text_seq",
                "offset_codepoints",
                "text",
                "through_codepoint",
                "final_codepoint",
            }
        }
        frozen = rfc8785.dumps(payload)  # Also rejects unpaired Unicode surrogates.
        sequence = event["text_seq"]
        if sequence in self.seen:
            previous, ack = self.seen[sequence]
            if previous != frozen:
                raise ValueError("text_conflict")
            return copy.deepcopy(ack)
        if self.finished:
            raise ValueError("text_finished")
        if sequence != len(self.seen):
            raise ValueError("text_gap")
        kind = event["type"]
        if kind == "stream.text.append":
            if event["offset_codepoints"] != len(self.text):
                raise ValueError("text_gap")
            if len(self.text) + len(event["text"]) > self.credit:
                raise ValueError("credit_exceeded")
            self.text += event["text"]
        else:
            end = event.get("through_codepoint", event.get("final_codepoint"))
            assert isinstance(end, int)
            if end < self.committed or end > len(self.text):
                raise ValueError("text_commit_invalid")
            if kind == "stream.text.finish" and end != len(self.text):
                raise ValueError("text_commit_invalid")
            if end > self.committed:
                self.adapter_inputs.append(self.text[self.committed : end])
            self.committed = end
            self.finished = kind == "stream.text.finish"
        ack = {
            "schema_version": "1.1.0",
            "type": "stream.text.ack",
            "stream_id": "stream-demo",
            "stream_epoch": 1,
            "seq": sequence,
            "sent_at": "2026-10-23T00:00:00Z",
            "operation_id": "op-demo",
            "attempt_id": "att-demo",
            "accepted_through_text_seq": sequence,
            "accepted_through_codepoint": len(self.text),
            "committed_through_codepoint": self.committed,
            "credit_through_codepoint": self.credit,
            "finished": self.finished,
        }
        self.validator.validate(ack)
        self.seen[sequence] = frozen, ack
        return copy.deepcopy(ack)


def _text_event(sequence: int, kind: str, **payload: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "type": "stream.text." + kind,
        "stream_id": "stream-demo",
        "stream_epoch": 1,
        "seq": sequence,
        "sent_at": "2026-10-23T00:00:00Z",
        "operation_id": "op-demo",
        "attempt_id": "att-demo",
        "text_seq": sequence,
        **payload,
    }


def test_internal_tts_text_commit_retry_and_finish_sequence() -> None:
    peer = _TextProtocolPeer()
    append = _text_event(0, "append", offset_codepoints=0, text="A😀")
    ack = peer.accept(append)
    assert ack["accepted_through_codepoint"] == 2
    assert peer.adapter_inputs == []
    assert peer.accept(append) == ack
    peer.accept(_text_event(1, "commit", through_codepoint=1))
    peer.accept(_text_event(2, "append", offset_codepoints=2, text="B"))
    finish = _text_event(3, "finish", final_codepoint=3)
    assert peer.accept(finish)["finished"] is True
    assert peer.accept(finish)["finished"] is True
    assert peer.accept(append) == ack
    assert peer.adapter_inputs == ["A", "😀B"]
    changed = copy.deepcopy(append)
    changed["text"] = "changed"
    with pytest.raises(ValueError, match="text_conflict"):
        peer.accept(changed)
    with pytest.raises(ValueError, match="text_finished"):
        peer.accept(_text_event(4, "append", offset_codepoints=3, text="C"))


@pytest.mark.parametrize(
    "event,error",
    [
        (_text_event(1, "append", offset_codepoints=0, text="A"), "text_gap"),
        (_text_event(0, "append", offset_codepoints=1, text="A"), "text_gap"),
        (_text_event(0, "append", offset_codepoints=0, text="12345678901"), "credit_exceeded"),
        (_text_event(0, "commit", through_codepoint=1), "text_commit_invalid"),
        (_text_event(0, "finish", final_codepoint=1), "text_commit_invalid"),
        (
            {**_text_event(0, "append", offset_codepoints=0, text="A"), "stream_epoch": 2},
            "stale_epoch",
        ),
    ],
)
def test_internal_tts_text_sequence_rejects_invalid_transitions(
    event: dict[str, Any], error: str
) -> None:
    peer = _TextProtocolPeer()
    with pytest.raises(ValueError, match=error):
        peer.accept(event)
    assert peer.text == ""
    assert peer.adapter_inputs == []
    assert not peer.seen


def _plan_digest(plan: dict[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            rfc8785.dumps({key: value for key, value in plan.items() if key != "plan_digest"})
        ).hexdigest()
    )


def _confirm_plan(
    plan: dict[str, Any], request: dict[str, Any], dependencies: list[dict[str, Any]], *, now: str
) -> None:
    _deletion_validator("DeletionPlan").validate(plan)
    _deletion_validator("DeletionCreateRequest").validate(request)
    if (
        request["plan_id"] != plan["id"]
        or request["expected_plan_revision"] != plan["plan_revision"]
        or request["plan_digest"] != plan["plan_digest"]
        or _plan_digest(plan) != plan["plan_digest"]
    ):
        raise ValueError("deletion_plan_changed")
    if now >= plan["expires_at"]:
        raise ValueError("deletion_plan_expired")
    if dependencies != plan["dependencies"]:
        raise ValueError("deletion_plan_changed")
    if not plan["eligible"] or any(item["disposition"] == "blocks" for item in dependencies):
        raise ValueError("deletion_dependency_conflict")
    cascades = [item["target"] for item in dependencies if item["disposition"] == "cascade"]
    if plan["mode"] == "ordinary" and cascades:
        raise ValueError("deletion_dependency_conflict")
    if any(target["owner"] != plan["target"]["owner"] for target in cascades):
        raise ValueError("deletion_owner_mismatch")
    expected = {rfc8785.dumps(target) for target in [plan["target"], *cascades]}
    if {rfc8785.dumps(target) for target in request["confirm_targets"]} != expected:
        raise ValueError("deletion_confirmation_mismatch")


def _cascade_plan() -> tuple[dict[str, Any], dict[str, Any]]:
    target = {"owner": "speech", "kind": "asset", "resource": {"id": "asset-demo", "revision": "1"}}
    voice = {
        "owner": "speech",
        "kind": "voice_revision",
        "resource": {"id": "voice-demo", "revision": "1"},
    }
    saved = {
        "owner": "workbench",
        "kind": "saved_asset",
        "resource": {"id": "saved-demo", "revision": "1"},
    }
    plan = {
        "id": "plan-demo",
        "object": "nexus.deletion_plan",
        "target": target,
        "mode": "cascade",
        "plan_revision": "1",
        "created_at": "2026-10-23T00:00:00Z",
        "expires_at": "2026-10-23T00:05:00Z",
        "dependencies": [
            {"target": voice, "relation": "persistent_reference", "disposition": "cascade"},
            {"target": saved, "relation": "independent_copy", "disposition": "retain"},
        ],
        "eligible": True,
    }
    plan["plan_digest"] = _plan_digest(plan)
    request: dict[str, Any] = {
        "plan_id": "plan-demo",
        "expected_plan_revision": "1",
        "plan_digest": plan["plan_digest"],
        "confirm_targets": [target, voice],
    }
    return plan, request


def test_deletion_snapshot_confirmation_and_independent_copy_sequence() -> None:
    plan, request = _cascade_plan()
    _confirm_plan(plan, request, plan["dependencies"], now="2026-10-23T00:01:00Z")
    for key, value in [("plan_digest", "sha256:" + "b" * 64), ("expected_plan_revision", "2")]:
        changed = copy.deepcopy(request)
        changed[key] = value
        with pytest.raises(ValueError, match="deletion_plan_changed"):
            _confirm_plan(plan, changed, plan["dependencies"], now="2026-10-23T00:01:00Z")
    with pytest.raises(ValueError, match="deletion_plan_expired"):
        _confirm_plan(plan, request, plan["dependencies"], now="2026-10-23T00:05:00Z")
    with pytest.raises(ValueError, match="deletion_plan_changed"):
        _confirm_plan(plan, request, [], now="2026-10-23T00:01:00Z")
    for targets in (
        [plan["target"]],
        [*request["confirm_targets"], plan["dependencies"][1]["target"]],
    ):
        with pytest.raises(ValueError, match="deletion_confirmation_mismatch"):
            _confirm_plan(
                plan,
                {**request, "confirm_targets": targets},
                plan["dependencies"],
                now="2026-10-23T00:01:00Z",
            )
    # Ordinary deletion cannot silently chase the published voice dependency.
    ordinary = copy.deepcopy(plan)
    ordinary["mode"] = "ordinary"
    ordinary["dependencies"][0]["disposition"] = "blocks"
    ordinary["eligible"] = False
    ordinary["plan_digest"] = _plan_digest(ordinary)
    with pytest.raises(ValueError, match="deletion_dependency_conflict"):
        _confirm_plan(
            ordinary,
            {**request, "plan_digest": ordinary["plan_digest"]},
            ordinary["dependencies"],
            now="2026-10-23T00:01:00Z",
        )
    # Restoration replays tombstones idempotently before publishing content.
    tombstones: set[bytes] = set()
    for _ in range(2):
        tombstones.update(rfc8785.dumps(target) for target in request["confirm_targets"])
    assert len(tombstones) == 2
    assert rfc8785.dumps(plan["dependencies"][1]["target"]) not in tombstones


def test_all_canonical_command_variants_have_valid_typed_examples() -> None:
    schema = _load_json(_CONTRACTS / "internal/execution-command.schema.json")
    validator = _validator(schema)
    for example in schema["examples"]:
        validator.validate(example)
        rfc8785.dumps(example)
    assert {example["kind"] for example in schema["examples"]} == {
        "asr",
        "tts",
        "voicedesign",
        "voiceclone",
        "voicevalidation",
        "alignment",
        "diarization",
        "vad",
        "codec",
    }


def test_internal_text_negotiation_cannot_accept_pcm_input() -> None:
    schema = _load_json(_CONTRACTS / "internal/execution-stream.schema.json")
    validator = _validator(schema)
    opened = copy.deepcopy(
        next(item for item in schema["examples"] if item["type"] == "stream.open")
    )
    opened.update(input_kind="text", input=None, input_credit_samples=0, input_credit_codepoints=10)
    validator.validate(opened)
    command = _command()
    command.update(
        text=None,
        text_stream={"stream_id": opened["stream_id"], "stream_epoch": opened["stream_epoch"]},
    )
    _validator(_load_json(_CONTRACTS / "internal/execution-command.schema.json")).validate(command)
    for field, value in (
        ("input", {"encoding": "pcm_s16le", "sample_rate_hz": 24000, "channels": 1}),
        ("input_credit_samples", 1),
    ):
        wrong = copy.deepcopy(opened)
        wrong[field] = value
        with pytest.raises(ValidationError):
            validator.validate(wrong)


def test_accepted_deletion_replays_after_plan_expiry_without_new_cleanup() -> None:
    plan, request = _cascade_plan()
    accepted: dict[str, tuple[bytes, str]] = {}

    def accept(key: str, intent: dict[str, Any], now: str) -> str:
        digest = rfc8785.dumps(intent)
        if key in accepted:
            previous, identity = accepted[key]
            if previous != digest:
                raise ValueError("idempotency_conflict")
            return identity
        _confirm_plan(plan, intent, plan["dependencies"], now=now)
        accepted[key] = digest, "deletion-original"
        return "deletion-original"

    first = accept("synthetic-key", request, "2026-10-23T00:01:00Z")
    assert accept("synthetic-key", request, "2026-10-23T01:00:00Z") == first
    with pytest.raises(ValueError, match="idempotency_conflict"):
        accept("synthetic-key", {**request, "expected_plan_revision": "2"}, "2026-10-23T01:00:00Z")
    assert len(accepted) == 1


def test_lost_sync_response_recovers_same_operation_through_completion() -> None:
    from openai import APITimeoutError

    speech = _resolved_yaml(_CONTRACTS / "speech-openapi.yaml")
    errors = _component_validator(speech, "ErrorResponse")
    operations = _component_validator(speech, "Operation")
    operation: dict[str, Any] = {
        "id": "op-original",
        "object": "nexus.operation",
        "kind": "tts",
        "status": "running",
        "cleanup_state": "pending",
        "execution_revision": "1",
        "receipt": None,
        "error": None,
        "created_at": "2026-10-23T00:00:00Z",
        "updated_at": "2026-10-23T00:00:00Z",
    }
    accepted: set[str] = set()
    attempts = 0

    def transport(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        if request.method == "GET":
            assert request.url.path == "/v1/nexus/operations/op-original"
            operations.validate(operation)
            return httpx2.Response(200, json=operation)
        attempts += 1
        key = request.headers["Idempotency-Key"]
        if key not in accepted:
            accepted.add(key)
            raise httpx2.ReadTimeout("Synthetic response loss after acceptance", request=request)
        recovery: dict[str, Any] = {"operation_id": operation["id"]}
        code = "operation_in_progress"
        if operation["status"] == "succeeded":
            code = "response_not_replayable"
            recovery["receipt"] = operation["receipt"]
        body = {
            "error": {
                "code": code,
                "type": "api_error",
                "message": "Query original execution.",
                "param": None,
                "request_id": "req-demo",
                "retryable": False,
                "nexus": recovery,
            }
        }
        errors.validate(body)
        return httpx2.Response(409, json=body)

    with OpenAI(
        api_key="synthetic-test",
        base_url="http://mock.invalid/v1",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
    ) as client:

        def submit() -> None:
            client.audio.speech.create(
                model="tts",
                input="Synthetic.",
                voice="alloy",
                extra_headers={"Idempotency-Key": "synthetic-key"},
            )

        with pytest.raises(APITimeoutError):
            submit()
        with pytest.raises(APIStatusError) as progress:
            submit()
        assert cast(dict[str, Any], progress.value.body)["nexus"]["operation_id"] == "op-original"
        in_progress = client.get("/nexus/operations/op-original", cast_to=dict[str, Any])
        assert in_progress["status"] == "running"
        operation.update(status="succeeded", cleanup_state="complete", receipt=_receipt())
        with pytest.raises(APIStatusError) as terminal:
            submit()
        terminal_error = cast(dict[str, Any], terminal.value.body)
        assert terminal_error["nexus"]["receipt"] == _receipt()
        completed = client.get("/nexus/operations/op-original", cast_to=dict[str, Any])
        assert completed["id"] == in_progress["id"] == "op-original"
        assert completed["receipt"] == _receipt()
    assert len(accepted) == 1
    assert attempts == 3


def test_internal_command_domains_remain_subsets_of_shared_authorities() -> None:
    command = _load_json(_CONTRACTS / "internal/execution-command.schema.json")
    shared = _load_json(_CONTRACTS / "shared/capability-domains.schema.json")["$defs"]
    tasks: set[str] = set()
    for name, schema in command["$defs"].items():
        if name == "ExecutionCommand" or not name.endswith("Command"):
            continue
        properties = schema["properties"]
        assert set(properties["task_kind"]["enum"]) <= set(shared["SpeechTaskKind"]["enum"])
        assert set(properties["stage_role"]["enum"]) <= set(shared["StageRole"]["enum"])
        tasks.update(properties["task_kind"]["enum"])
    assert tasks == set(shared["SpeechTaskKind"]["enum"])
