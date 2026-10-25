"""Semantic contract tests for the P0 public, workbench, and internal schemas."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from email.message import Message
from email.parser import BytesParser
from email.policy import HTTP
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import httpx
import jsonschema
import pytest
import yaml
from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError
from openai import OpenAI, RateLimitError
from openai.resources.realtime.realtime import RealtimeConnection
from openai.types.audio import Transcription
from openai.types.audio.transcription_verbose import TranscriptionVerbose
from openai.types.realtime import (
    ConversationItemInputAudioTranscriptionDeltaEvent,
    ConversationItemInputAudioTranscriptionFailedEvent,
    InputAudioBufferClearedEvent,
    InputAudioBufferCommittedEvent,
    InputAudioBufferSpeechStartedEvent,
    InputAudioBufferSpeechStoppedEvent,
    SessionUpdatedEvent,
)
from openai.types.realtime.conversation_item_input_audio_transcription_completed_event import (
    ConversationItemInputAudioTranscriptionCompletedEvent,
)
from openai.types.realtime.realtime_error_event import RealtimeErrorEvent
from openai.types.realtime.session_created_event import SessionCreatedEvent
from openapi_spec_validator import validate
from referencing import Registry, Resource

from tests.support.safe_yaml import load_yaml

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "contracts"
_SPEECH_OPENAPI = _CONTRACTS / "speech-openapi.yaml"
_ADMIN_OPENAPI = _CONTRACTS / "admin-openapi.yaml"
_WORKBENCH_OPENAPI = _CONTRACTS / "workbench-openapi.yaml"
_REALTIME_SCHEMA = _CONTRACTS / "realtime-events.schema.json"
_SCENARIO_SCHEMA = _CONTRACTS / "scenario-profile.schema.json"
_VOICE_SCHEMA = _CONTRACTS / "voice-resource.schema.json"
_BINDING_SCHEMA = _CONTRACTS / "model-binding.schema.json"
_INSTANCE_SCHEMA = _CONTRACTS / "model-instance.schema.json"
_QUALIFICATION_SCHEMA = _CONTRACTS / "qualification-report.schema.json"
_EXECUTION_STREAM_SCHEMA = _CONTRACTS / "internal" / "execution-stream.schema.json"
_EXECUTION_CONTROL_OPENAPI = _CONTRACTS / "internal" / "execution-control.openapi.yaml"
_RUNTIME_CONTROL_OPENAPI = _CONTRACTS / "internal" / "runtime-control.openapi.yaml"
_RUNTIME_STATES_SCHEMA = _CONTRACTS / "shared" / "runtime-states.schema.json"
_SHARED_ERRORS_SCHEMA = _CONTRACTS / "shared" / "errors.schema.json"
_OPERATION_STATES_SCHEMA = _CONTRACTS / "shared" / "operation-states.schema.json"


def _load_json(path: Path) -> dict[str, Any]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    document = cast(dict[str, Any], raw)
    _resolve_local_refs(document, path)
    return document


def _load_yaml(path: Path) -> dict[str, Any]:
    raw: object = load_yaml(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(dict[str, Any], raw)


def _resolve_local_refs(node: Any, source: Path) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith(("#", "http://", "https://", "file://")):
            path_part, separator, fragment = ref.partition("#")
            if path_part:
                target = (source.parent / path_part).resolve()
                node["$ref"] = target.as_uri() + (f"#{fragment}" if separator else "")
        for value in node.values():
            _resolve_local_refs(value, source)
    elif isinstance(node, list):
        for value in node:
            _resolve_local_refs(value, source)


def _collect_refs(node: Any) -> list[str]:
    references: list[str] = []
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str):
            references.append(reference)
        for value in node.values():
            references.extend(_collect_refs(value))
    elif isinstance(node, list):
        for value in node:
            references.extend(_collect_refs(value))
    return references


def _reachable_local_definitions(
    root: Any,
    definitions: dict[str, Any],
    prefix: str,
) -> set[str]:
    reachable: set[str] = set()
    pending = _collect_refs(root)
    while pending:
        reference = pending.pop()
        if not reference.startswith(prefix):
            continue
        name = reference[len(prefix) :].split("/", 1)[0]
        if name in reachable or name not in definitions:
            continue
        reachable.add(name)
        pending.extend(_collect_refs(definitions[name]))
    return reachable


def _resolved_yaml(path: Path) -> dict[str, Any]:
    document = _load_yaml(path)
    _resolve_local_refs(document, path)
    return document


def _retrieve_contract(uri: str) -> Resource[Any]:
    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    path = Path(unquote(parsed.path))
    return Resource.from_contents(_load_json(path))


_REGISTRY: Registry[Any] = cast(Any, Registry)(retrieve=_retrieve_contract)


def _validator(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        schema,
        registry=_REGISTRY,
        format_checker=FormatChecker(),
    )


def _component_validator(document: dict[str, Any], name: str) -> jsonschema.Draft202012Validator:
    schemas = copy.deepcopy(document["components"]["schemas"])

    def rewrite(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                node["$ref"] = "#/$defs/" + ref.rsplit("/", 1)[-1]
            for value in node.values():
                rewrite(value)
        elif isinstance(node, list):
            for value in node:
                rewrite(value)

    rewrite(schemas)
    bundle: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": schemas,
    }
    jsonschema.Draft202012Validator.check_schema(bundle)
    schema: dict[str, Any] = {"$ref": f"#/$defs/{name}", "$defs": schemas}
    return _validator(schema)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_qualification_semantics(report: dict[str, Any]) -> None:
    """Executable reference rules that JSON Schema cannot express across arrays."""
    scenarios = cast(list[dict[str, Any]], report["scenarios"])
    scenario_ids = [cast(str, scenario["scenario_id"]) for scenario in scenarios]
    declared_ids = set(cast(list[str], report["test_suite"]["scenario_ids"]))
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("scenario evidence contains duplicate scenario_id values")
    if set(scenario_ids) != declared_ids or len(scenario_ids) != len(declared_ids):
        raise ValueError("scenario evidence must match test_suite.scenario_ids one-to-one")

    required = [scenario for scenario in scenarios if scenario["required"]]
    required_failed = any(scenario["status"] == "failed" for scenario in required)
    required_passed = bool(required) and all(
        scenario["status"] == "passed" for scenario in required
    )
    expected_overall = "fail" if required_failed else "pass" if required_passed else "incomplete"
    if report["required_scenarios_passed"] is not required_passed:
        raise ValueError("required_scenarios_passed does not match required scenario results")
    if report["overall_status"] != expected_overall:
        raise ValueError("overall_status does not match required scenario results")

    optional_not_passed = [
        scenario
        for scenario in scenarios
        if not scenario["required"] and scenario["status"] != "passed"
    ]
    if optional_not_passed and not report["limitations"]:
        raise ValueError("a non-passing optional scenario requires a recorded limitation")

    expires_at = report.get("expires_at")
    if expires_at is not None:
        qualified_at = _parse_timestamp(cast(str, report["qualified_at"]))
        parsed_expiry = _parse_timestamp(cast(str, expires_at))
        if qualified_at >= parsed_expiry:
            raise ValueError("qualified_at must precede expires_at")


def _assert_scenario_profile_semantics(profile: dict[str, Any]) -> None:
    """Executable parameter-domain rules that JSON Schema cannot compare by name."""
    validation = cast(dict[str, Any] | None, profile.get("validation"))
    if validation is not None and (
        validation["profile_id"] != profile["profile_id"]
        or validation["revision"] != profile["revision"]
    ):
        raise ValueError("profile validation evidence does not identify this revision")

    allowlist = set(cast(list[str], profile["override_allowlist"]))
    ranges = cast(dict[str, dict[str, Any]], profile["override_ranges"])
    unexpected_ranges = set(ranges) - allowlist
    if unexpected_ranges:
        raise ValueError(f"override ranges are not allowlisted: {sorted(unexpected_ranges)}")
    for path, override_range in ranges.items():
        minimum = override_range.get("minimum")
        maximum = override_range.get("maximum")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"override range for {path} has minimum greater than maximum")

    capabilities = cast(list[dict[str, Any]], profile["required_capabilities"])
    capability_names = [cast(str, capability["capability"]) for capability in capabilities]
    if len(capability_names) != len(set(capability_names)):
        raise ValueError("required capability names must be unique")
    for capability in capabilities:
        constraint = cast(dict[str, Any] | None, capability.get("constraint"))
        if constraint is None:
            continue
        minimum = constraint.get("minimum")
        maximum = constraint.get("maximum")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(
                f"constraint for {capability['capability']} has minimum greater than maximum"
            )

    asr = cast(dict[str, Any] | None, profile["parameters"].get("asr"))
    if asr is not None and asr["max_segment_ms"] < asr["preview_interval_ms"]:
        raise ValueError("max_segment_ms must be greater than or equal to preview_interval_ms")


def _assert_job_success_evidence(data: dict[str, Any]) -> None:
    """Executable cross-array rules for a succeeded Job or job.succeeded event."""
    artifacts = cast(list[dict[str, Any]], data["artifacts"])
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        artifact_id = cast(str, artifact["artifact_id"])
        if artifact_id in artifacts_by_id:
            raise ValueError(f"successful job evidence repeats artifact_id {artifact_id}")
        artifacts_by_id[artifact_id] = artifact

    result = cast(dict[str, Any], data["result"])
    result_kind = cast(str, result["kind"])
    expected_artifacts: list[tuple[str, str]]
    if result_kind == "asr":
        expected_artifacts = [(cast(str, result["transcript_artifact_id"]), "transcript")]
    elif result_kind == "tts":
        expected_artifacts = [(cast(str, result["audio_artifact_id"]), "audio")]
    elif result_kind in {"voice_design", "voice_clone"}:
        expected_artifacts = [
            (artifact_id, "preview")
            for artifact_id in cast(list[str], result["preview_artifact_ids"])
        ]
    elif result_kind == "voice_preview":
        expected_artifacts = [(cast(str, result["preview_artifact_id"]), "preview")]
    elif result_kind == "voice_validation":
        expected_artifacts = [
            (cast(str, result["validation_report_artifact_id"]), "validation_report")
        ]
    else:
        raise ValueError(f"unsupported successful result kind {result_kind}")

    receipt_id = cast(str, result["receipt_id"])
    for artifact_id, expected_kind in expected_artifacts:
        result_artifact = artifacts_by_id.get(artifact_id)
        if result_artifact is None:
            raise ValueError(f"result artifact {artifact_id} is not committed in artifacts")
        if result_artifact["kind"] != expected_kind:
            raise ValueError(
                f"result artifact {artifact_id} has kind {result_artifact['kind']}, "
                f"expected {expected_kind}"
            )
        if result_artifact["receipt_id"] != receipt_id:
            raise ValueError(f"result artifact {artifact_id} does not carry the result receipt")


def _assert_export_artifact_semantics(
    export_id: str,
    manifest_revision: str,
    artifact: dict[str, Any],
) -> None:
    """Executable ownership and manifest pin rules for an export artifact."""
    if artifact["export_id"] != export_id:
        raise ValueError("export artifact belongs to a different export job")
    if artifact["manifest_revision"] != manifest_revision:
        raise ValueError("export artifact manifest revision differs from its job")


def _assert_export_job_semantics(job: dict[str, Any]) -> None:
    """Executable progress and artifact-pin rules for a workbench export job."""
    completed = cast(int, job["progress_completed"])
    total = cast(int, job["progress_total"])
    if completed > total:
        raise ValueError("export progress completed exceeds progress total")
    artifact = cast(dict[str, Any] | None, job.get("artifact"))
    if job["status"] == "succeeded":
        if completed != total:
            raise ValueError("a succeeded export requires completed progress equal to total")
        if artifact is None:
            raise ValueError("a succeeded export requires an artifact")
        _assert_export_artifact_semantics(
            cast(str, job["id"]),
            cast(str, job["manifest_revision"]),
            artifact,
        )


def _assert_resource_pool_semantics(pool: dict[str, Any]) -> None:
    """Executable aggregate-count rules for a logical resource pool."""
    max_active_slots = cast(int, pool["max_active_slots"])
    if pool["realtime_reserved_slots"] > max_active_slots:
        raise ValueError("realtime reserved slots exceed the pool capacity")
    if pool["active_slots"] + pool["quarantine_slots"] > max_active_slots:
        raise ValueError("active and quarantined slots exceed the pool capacity")
    if pool["status"] == "ready" and not pool["node_ids"]:
        raise ValueError("a ready pool requires at least one contributing node")


def _assert_quarantine_release_semantics(
    request: dict[str, Any],
    release: dict[str, Any],
) -> None:
    """Executable request/response echo and temporal rules for quarantine release."""
    if (
        release["evidence_kind"] != request["evidence_kind"]
        or release["evidence_digest"] != request["evidence_digest"]
        or release["evidence_observed_at"] != request["observed_at"]
        or release["reason"] != request["reason"]
    ):
        raise ValueError("quarantine release does not echo the accepted evidence")
    if _parse_timestamp(cast(str, release["evidence_observed_at"])) > _parse_timestamp(
        cast(str, release["budget_released_at"])
    ):
        raise ValueError("quarantine evidence was observed after budget release")
    released_intents = cast(list[str], release["released_submit_intent_ids"])
    if released_intents != sorted(released_intents):
        raise ValueError("released submit intent ids are not sorted")


def _assert_runtime_memory_reservation_semantics(reservation: dict[str, Any]) -> None:
    peak = cast(dict[str, int], reservation["peak"])
    steady = cast(dict[str, int], reservation["steady"])
    for counter in ("unified_memory_bytes", "device_memory_bytes", "host_memory_bytes"):
        if peak[counter] < steady[counter]:
            raise ValueError(f"runtime memory peak is below steady for {counter}")


def _assert_node_reconcile_semantics(snapshot: dict[str, Any]) -> None:
    identities: set[tuple[str, int]] = set()
    for fact in cast(list[dict[str, Any]], snapshot["instances"]):
        identity = (cast(str, fact["instance_id"]), cast(int, fact["instance_generation"]))
        if identity in identities:
            raise ValueError(f"node reconciliation repeats instance generation {identity}")
        identities.add(identity)


def _assert_output_watermark_semantics(watermark: dict[str, Any]) -> None:
    ceiling = cast(int | None, watermark["hard_output_ceiling_samples"])
    if ceiling is not None and watermark["generated_pcm_samples"] > ceiling:
        raise ValueError("generated output exceeds the hard output ceiling")


def _assert_execution_status_semantics(status: dict[str, Any]) -> None:
    watermark = cast(dict[str, Any] | None, status.get("output_watermark"))
    if watermark is None:
        return
    _assert_output_watermark_semantics(watermark)
    if status["sample_rate_hz"] != watermark["sample_rate_hz"]:
        raise ValueError("execution and output-watermark sample rates differ")


def _assert_realtime_terminal_operations(
    session: dict[str, Any],
    operations: list[dict[str, Any]],
) -> None:
    """Executable ownership and terminal projection rules for a session."""
    operations_by_id = {cast(str, operation["id"]): operation for operation in operations}
    seen: set[str] = set()
    for reference in cast(list[dict[str, Any]], session["terminal_operations"]):
        operation_id = cast(str, reference["operation_id"])
        if operation_id in seen:
            raise ValueError(f"terminal operations repeat operation_id {operation_id}")
        seen.add(operation_id)
        operation = operations_by_id.get(operation_id)
        if operation is None:
            raise ValueError(f"terminal operation {operation_id} cannot be resolved")
        if operation["session_id"] != session["id"]:
            raise ValueError(f"terminal operation {operation_id} belongs to another session")
        for field in ("kind", "status", "cleanup_state"):
            if reference[field] != operation[field]:
                raise ValueError(f"terminal operation {operation_id} has mismatched {field}")
        if operation["status"] == "succeeded":
            receipt = cast(dict[str, Any] | None, operation["receipt"])
            if receipt is None or reference["receipt_id"] != receipt["receipt_id"]:
                raise ValueError(f"terminal operation {operation_id} has mismatched receipt")


def _assert_voice_validation_semantics(validation: dict[str, Any]) -> None:
    checks = cast(list[dict[str, Any]], validation["checks"])
    names = [cast(str, check["name"]) for check in checks]
    if len(names) != len(set(names)):
        raise ValueError("voice validation check names must be unique")
    if validation["status"] == "failed" and not any(
        check["status"] == "failed" for check in checks
    ):
        raise ValueError("failed voice validation requires a failed check")
    if validation["publication_eligible"] and not any(
        check["name"] == "reusable_tts"
        and check["required"] is True
        and check["status"] == "passed"
        for check in checks
    ):
        raise ValueError("publication-eligible voice requires a passed reusable_tts check")


def _assert_voice_publication_semantics(
    publication: dict[str, Any],
    draft: dict[str, Any],
    draft_revision: dict[str, Any],
    validation: dict[str, Any],
    voice_revision: dict[str, Any],
) -> None:
    """Executable identity chain for draft, validation, publication, and voice."""
    draft_id = cast(str, publication["draft_id"])
    draft_revision_number = cast(str, publication["draft_revision"])
    voice_id = cast(str, publication["voice_id"])
    voice_revision_number = cast(str, publication["voice_revision"])

    if draft["id"] != draft_id or draft_revision["draft_id"] != draft_id:
        raise ValueError("publication draft identity does not match the draft and revision")
    if draft["kind"] != draft_revision["kind"]:
        raise ValueError("voice draft kind differs from its revision kind")
    if draft_revision["revision"] != draft_revision_number:
        raise ValueError("publication draft revision does not identify the cited revision")
    if validation["draft_id"] != draft_id:
        raise ValueError("voice validation belongs to a different draft")
    if validation["draft_revision"] != draft_revision_number:
        raise ValueError("voice validation belongs to a different draft revision")
    if publication["validation_id"] != validation["id"]:
        raise ValueError("publication validation_id does not identify the cited validation")
    if publication["validation_job_id"] != draft_revision["validation_job_id"]:
        raise ValueError("publication and draft revision cite different validation jobs")
    if validation["status"] != "passed" or not validation["publication_eligible"]:
        raise ValueError("voice publication requires a passed publication-eligible validation")

    if draft["current_voice_id"] != voice_id:
        raise ValueError("voice draft current_voice_id does not identify the published voice")
    if draft_revision["published_voice_id"] != voice_id:
        raise ValueError("published draft revision identifies a different voice")
    if draft_revision["published_voice_revision"] != voice_revision_number:
        raise ValueError("published draft revision identifies a different voice revision")
    if (
        voice_revision["voice_id"] != voice_id
        or voice_revision["revision"] != voice_revision_number
    ):
        raise ValueError("published voice revision does not match the publication")

    source = cast(dict[str, Any], voice_revision["source"])
    expected_source_type = "designed" if draft["kind"] == "design" else "cloned"
    if source["type"] != expected_source_type:
        raise ValueError("published voice source type does not match the draft kind")
    if source["draft_id"] != draft_id or source["draft_revision"] != draft_revision_number:
        raise ValueError("published voice source does not identify the draft revision")
    if draft["kind"] == "clone" and source["source_asset_digest"] is None:
        raise ValueError("cloned voice source requires the reference asset digest")

    if voice_revision["model_binding"] != validation["model_binding"]:
        raise ValueError("published voice and validation use different model bindings")
    voice_validation = cast(dict[str, Any], voice_revision["validation"])
    if (
        voice_validation["validation_id"] != validation["id"]
        or voice_validation["status"] != "passed"
    ):
        raise ValueError("published voice validation evidence does not match the validation")
    if voice_revision["published_at"] != publication["published_at"]:
        raise ValueError("publication and voice revision published_at values differ")


def _multipart_fields(request: httpx.Request) -> dict[str, bytes]:
    content_type = request.headers["content-type"]
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    message = BytesParser(policy=HTTP).parsebytes(raw)
    assert isinstance(message, Message)
    fields: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if isinstance(name, str):
            payload = part.get_payload(decode=True)
            fields[name] = payload if isinstance(payload, bytes) else b""
    return fields


def _matches_declared_path(path: str, declared_paths: set[str]) -> bool:
    return any(fnmatch(path, re.sub(r"\{[^}]+\}", "*", template)) for template in declared_paths)


def _assert_string_enum_values_are_quoted(node: Any, pointer: str = "") -> None:
    if isinstance(node, dict):
        if node.get("type") == "string" and isinstance(node.get("enum"), list):
            invalid = [value for value in node["enum"] if not isinstance(value, str)]
            assert not invalid, f"{pointer}: string enum contains non-string values {invalid!r}"
        for key, value in node.items():
            _assert_string_enum_values_are_quoted(value, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_string_enum_values_are_quoted(value, f"{pointer}/{index}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        assert key not in result, f"duplicate JSON object key: {key!r}"
        result[key] = value
    return result


class _UniqueKeyYamlLoader(yaml.SafeLoader):
    pass


def _construct_unique_yaml_mapping(
    loader: _UniqueKeyYamlLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        assert key not in result, f"duplicate YAML object key: {key!r}"
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_yaml_mapping,
)


def test_public_specs_validate_and_declare_openai_compatible_paths() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    validate(speech)
    paths = set(speech["paths"])
    assert {
        "/v1/models",
        "/v1/models/{model}",
        "/v1/audio/transcriptions",
        "/v1/audio/speech",
        "/v1/realtime",
        "/v1/nexus/capabilities",
        "/v1/nexus/jobs",
        "/v1/nexus/jobs/{job_id}",
        "/v1/nexus/jobs/{job_id}/events",
        "/v1/nexus/assets",
        "/v1/nexus/artifacts/{artifact_id}",
        "/v1/nexus/realtime/sessions",
        "/v1/nexus/voice-drafts/{draft_id}/publications",
        "/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements",
    } <= paths

    admin = _resolved_yaml(_ADMIN_OPENAPI)
    validate(admin)
    assert {
        "/v1/nexus/workspaces",
        "/v1/nexus/admin/profiles",
        "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}",
        "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/publications",
        "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/activations",
        "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/retirements",
        "/v1/nexus/admin/model-bindings",
        "/v1/nexus/admin/model-bindings/{binding_id}/publications",
        "/v1/nexus/admin/model-instances",
        "/v1/nexus/admin/model-instances/{instance_id}/generations/{generation}",
        "/v1/nexus/admin/capacity-manifests/{manifest_id}",
        "/v1/nexus/admin/qualification-reports/{report_id}",
        "/v1/nexus/admin/resource-pools",
        "/v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases",
        "/v1/nexus/admin/policies",
        "/v1/nexus/admin/diagnostics",
        "/v1/nexus/audit-events",
    } <= set(admin["paths"])
    assert "/v1/nexus/admin/model-instances/{instance_id}" not in admin["paths"]

    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    validate(workbench)
    assert {
        "/v1/workbench/projects",
        "/v1/workbench/transcripts/{transcript_id}",
        "/v1/workbench/meetings/{meeting_id}/minutes",
        "/v1/workbench/assistant-turns/{turn_id}",
        "/v1/workbench/render-units/{unit_id}/generations",
        "/v1/workbench/books/{book_id}/exports",
        "/v1/workbench/exports/{export_id}/content",
        "/v1/workbench/projects/{project_id}/saved-assets",
    } <= set(workbench["paths"])

    for document in (speech, workbench):
        for response_name in ("TooManyRequests", "ServiceUnavailable"):
            assert "Retry-After" in document["components"]["responses"][response_name]["headers"]


def test_openai_sdk_requests_and_responses_match_declared_contract() -> None:
    """Prove the declared OpenAI subset is consumable by the official SDK.

    This is a contract-parity test with a mock transport, not a running-server
    integration test. It captures the SDK's actual request shape and validates
    it against the OpenAPI schema; a future HTTP integration test must run the
    same assertions against the implemented routes.
    """
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    captured: list[httpx.Request] = []
    transcription_payload: dict[str, Any] = {
        "text": "hello",
        "languages": [{"code": "en"}],
        "logprobs": [{"token": "hello", "bytes": [104, 101, 108, 108, 111], "logprob": -0.1}],
        "usage": {"type": "duration", "seconds": 1.0},
    }
    verbose_payload: dict[str, Any] = {
        "duration": 1.0,
        "language": "en",
        "text": "hello",
        "segments": [
            {
                "id": 0,
                "avg_logprob": -0.1,
                "compression_ratio": 1.0,
                "end": 1.0,
                "no_speech_prob": 0.01,
                "seek": 0,
                "start": 0.0,
                "temperature": 0.0,
                "text": "hello",
                "tokens": [1, 2],
            }
        ],
        "words": [{"word": "hello", "start": 0.0, "end": 1.0}],
        "usage": {"type": "duration", "seconds": 1.0},
    }
    model_payload: dict[str, Any] = {
        "object": "list",
        "data": [
            {
                "id": "asr-quality",
                "object": "model",
                "created": 1700000000,
                "owned_by": "speechnexus",
                "nexus": {
                    "logical_model": "asr-quality",
                    "binding": {"id": "asr-quality", "revision": "7"},
                    "tasks": ["asr_file", "asr_full_segment_review"],
                    "release_tier": "experimental",
                    "readiness": {
                        "configured": True,
                        "engine_ready": True,
                        "quality_qualified": False,
                        "admission_state": "queueable",
                    },
                },
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.headers["authorization"] == "Bearer test-key"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=model_payload)
        if request.url.path == "/v1/models/asr-quality":
            return httpx.Response(200, json=model_payload["data"][0])
        if request.url.path == "/v1/audio/transcriptions":
            fields = _multipart_fields(request)
            response_format = fields.get("response_format", b"json").decode()
            if response_format in {"text", "srt", "vtt"}:
                return httpx.Response(
                    200,
                    content=b"hello text",
                    headers={"content-type": "text/plain"},
                )
            if response_format == "verbose_json":
                return httpx.Response(200, json=verbose_payload)
            return httpx.Response(200, json=transcription_payload)
        if request.url.path == "/v1/audio/speech":
            body = json.loads(request.content)
            if body.get("input") == "fail":
                return httpx.Response(
                    429,
                    json={
                        "error": {
                            "code": "quota_exceeded",
                            "type": "rate_limit_error",
                            "message": "quota exceeded",
                            "param": None,
                            "request_id": "req_mock",
                            "retryable": True,
                            "retry_after_ms": 1000,
                        }
                    },
                    headers={"retry-after": "1"},
                )
            return httpx.Response(
                200,
                content=b"RIFFmock",
                headers={"content-type": "audio/wav"},
            )
        raise AssertionError(f"unexpected SDK path: {request.url.path}")

    client = OpenAI(
        base_url="http://testserver/v1",
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    models = client.models.list()
    assert models.data[0].id == "asr-quality"
    assert client.models.retrieve("asr-quality").id == "asr-quality"

    transcription = client.audio.transcriptions.create(
        file=("clip.wav", b"RIFFmock", "audio/wav"),
        model="asr-quality",
        response_format="json",
    )
    assert transcription.text == "hello"
    assert transcription.languages is not None
    assert transcription.languages[0].code == "en"
    assert transcription.logprobs is not None
    assert transcription.logprobs[0].token == "hello"

    verbose = client.audio.transcriptions.create(
        file=("clip.wav", b"RIFFmock", "audio/wav"),
        model="asr-quality",
        response_format="verbose_json",
        timestamp_granularities=["segment", "word"],
    )
    assert verbose.duration == 1.0
    assert verbose.segments is not None
    assert verbose.segments[0].text == "hello"
    TranscriptionVerbose.model_validate(verbose_payload)

    text = client.audio.transcriptions.create(
        file=("clip.wav", b"RIFFmock", "audio/wav"),
        model="asr-quality",
        response_format="text",
    )
    assert text == "hello text"

    speech_response = client.audio.speech.create(
        model="tts-interactive",
        voice="narrator",
        input="hello",
        response_format="wav",
    )
    assert speech_response.read() == b"RIFFmock"

    with pytest.raises(RateLimitError):
        client.audio.speech.create(
            model="tts-interactive",
            voice="narrator",
            input="fail",
            response_format="wav",
        )

    declared_paths = set(speech["paths"])
    assert all(_matches_declared_path(request.url.path, declared_paths) for request in captured)

    transcription_requests = [
        request for request in captured if request.url.path == "/v1/audio/transcriptions"
    ]
    assert transcription_requests
    request_schema = speech["components"]["schemas"]["TranscriptionRequest"]
    allowed_multipart = set(request_schema["properties"])
    for request in transcription_requests:
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert set(_multipart_fields(request)) <= allowed_multipart

    speech_requests = [request for request in captured if request.url.path == "/v1/audio/speech"]
    assert speech_requests
    speech_validator = _component_validator(speech, "SpeechRequest")
    for request in speech_requests:
        speech_validator.validate(json.loads(request.content))

    _component_validator(speech, "ModelList").validate(model_payload)
    _component_validator(speech, "Transcription").validate(transcription_payload)
    _component_validator(speech, "TranscriptionVerbose").validate(verbose_payload)
    token_usage_payload = copy.deepcopy(transcription_payload)
    token_usage_payload["usage"] = {
        "type": "tokens",
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
    }
    _component_validator(speech, "Transcription").validate(token_usage_payload)
    parsed_token_usage = Transcription.model_validate(token_usage_payload)
    assert parsed_token_usage.usage is not None
    assert parsed_token_usage.usage.type == "tokens"

    token_usage_verbose = copy.deepcopy(verbose_payload)
    token_usage_verbose["usage"] = {
        "type": "tokens",
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
    }
    with pytest.raises(ValidationError):
        _component_validator(speech, "TranscriptionVerbose").validate(token_usage_verbose)


def test_scenario_profile_positive_and_negative_examples() -> None:
    schema = _load_json(_SCENARIO_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = _validator(schema)
    examples = cast(list[dict[str, Any]], schema["examples"])
    for example in examples:
        validator.validate(example)
        _assert_scenario_profile_semantics(example)

    unknown_field = copy.deepcopy(examples[0])
    unknown_field["priority"] = 10
    with pytest.raises(ValidationError):
        validator.validate(unknown_field)
    missing_display_name = copy.deepcopy(examples[0])
    del missing_display_name["display_name"]
    with pytest.raises(ValidationError):
        validator.validate(missing_display_name)

    wrong_kind = copy.deepcopy(examples[0])
    wrong_kind["parameters"]["tts"] = copy.deepcopy(examples[1]["parameters"]["tts"])
    with pytest.raises(ValidationError):
        validator.validate(wrong_kind)

    experimental_production = copy.deepcopy(examples[0])
    experimental_production["release_tier"] = "production"
    with pytest.raises(ValidationError):
        validator.validate(experimental_production)

    range_outside_allowlist = copy.deepcopy(examples[0])
    range_outside_allowlist["override_ranges"]["tts.speed"] = {"minimum": 0.5, "maximum": 2.0}
    validator.validate(range_outside_allowlist)
    with pytest.raises(ValueError, match="not allowlisted"):
        _assert_scenario_profile_semantics(range_outside_allowlist)

    reversed_override_range = copy.deepcopy(examples[0])
    reversed_override_range["override_ranges"]["asr.preview_interval_ms"] = {
        "minimum": 1000,
        "maximum": 300,
    }
    validator.validate(reversed_override_range)
    with pytest.raises(ValueError, match="minimum greater than maximum"):
        _assert_scenario_profile_semantics(reversed_override_range)

    duplicate_capability = copy.deepcopy(examples[0])
    duplicate_capability["required_capabilities"].append(
        {
            "capability": duplicate_capability["required_capabilities"][0]["capability"],
            "required": False,
            "constraint": {"maximum": 1},
        }
    )
    validator.validate(duplicate_capability)
    with pytest.raises(ValueError, match="capability names must be unique"):
        _assert_scenario_profile_semantics(duplicate_capability)

    empty_capability_constraint = copy.deepcopy(examples[0])
    empty_capability_constraint["required_capabilities"][0]["constraint"] = {}
    with pytest.raises(ValidationError):
        validator.validate(empty_capability_constraint)

    reversed_capability_constraint = copy.deepcopy(examples[0])
    reversed_capability_constraint["required_capabilities"][0]["constraint"] = {
        "minimum": 10,
        "maximum": 1,
    }
    validator.validate(reversed_capability_constraint)
    with pytest.raises(ValueError, match="minimum greater than maximum"):
        _assert_scenario_profile_semantics(reversed_capability_constraint)

    inverted_segment_bounds = copy.deepcopy(examples[0])
    inverted_segment_bounds["parameters"]["asr"]["max_segment_ms"] = 1000
    inverted_segment_bounds["parameters"]["asr"]["preview_interval_ms"] = 2000
    validator.validate(inverted_segment_bounds)
    with pytest.raises(ValueError, match="max_segment_ms"):
        _assert_scenario_profile_semantics(inverted_segment_bounds)


def test_voice_resource_positive_and_negative_examples() -> None:
    schema = _load_json(_VOICE_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = _validator(schema)
    examples = cast(list[dict[str, Any]], schema["examples"])
    for example in examples:
        validator.validate(example)

    clone_schema = {"$ref": "#/$defs/CloneInput", "$defs": schema["$defs"]}
    clone_validator = _validator(clone_schema)
    clone_validator.validate(
        {
            "kind": "clone",
            "reference_audio_asset_id": "ast_reference",
            "source_use_confirmed": True,
        }
    )
    with pytest.raises(ValidationError):
        clone_validator.validate(
            {
                "kind": "clone",
                "reference_audio_asset_id": "ast_reference",
                "source_use_confirmed": False,
            }
        )

    published_without_ids = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_draft_revision")
    )
    published_without_ids["status"] = "published"
    published_without_ids["published_voice_id"] = None
    published_without_ids["published_voice_revision"] = None
    with pytest.raises(ValidationError):
        validator.validate(published_without_ids)

    candidate = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_draft_revision")
    )
    mismatched_kind = copy.deepcopy(candidate)
    mismatched_kind["kind"] = "clone"
    with pytest.raises(ValidationError):
        validator.validate(mismatched_kind)

    candidate_without_generation_job = copy.deepcopy(candidate)
    candidate_without_generation_job["generation_job_id"] = None
    with pytest.raises(ValidationError):
        validator.validate(candidate_without_generation_job)

    candidate_without_preview = copy.deepcopy(candidate)
    candidate_without_preview["preview_artifact_ids"] = []
    with pytest.raises(ValidationError):
        validator.validate(candidate_without_preview)

    generating_with_preview = copy.deepcopy(candidate)
    generating_with_preview["status"] = "generating"
    with pytest.raises(ValidationError):
        validator.validate(generating_with_preview)

    non_failed_with_failure = copy.deepcopy(candidate)
    non_failed_with_failure["failure"] = {
        "code": "generation_failed",
        "message": "synthetic failure",
        "retryable": False,
    }
    with pytest.raises(ValidationError):
        validator.validate(non_failed_with_failure)

    validating_without_job = copy.deepcopy(candidate)
    validating_without_job["status"] = "validating"
    validating_without_job["validation_job_id"] = None
    with pytest.raises(ValidationError):
        validator.validate(validating_without_job)

    validated = copy.deepcopy(candidate)
    validated["status"] = "validated"
    validated["validation_job_id"] = "job_validation_demo"
    validator.validate(validated)

    published_revision = copy.deepcopy(validated)
    published_revision["status"] = "published"
    published_revision["published_voice_id"] = "voice_demo"
    published_revision["published_voice_revision"] = "1"
    validator.validate(published_revision)
    published_without_validation = copy.deepcopy(published_revision)
    published_without_validation["validation_job_id"] = None
    with pytest.raises(ValidationError):
        validator.validate(published_without_validation)

    failed_without_detail = copy.deepcopy(candidate)
    failed_without_detail["status"] = "failed"
    failed_without_detail["failure"] = None
    with pytest.raises(ValidationError):
        validator.validate(failed_without_detail)

    failed_with_published_ids = copy.deepcopy(published_revision)
    failed_with_published_ids["status"] = "failed"
    failed_with_published_ids["failure"] = {
        "code": "publication_failed",
        "message": "synthetic failure",
        "retryable": False,
    }
    with pytest.raises(ValidationError):
        validator.validate(failed_with_published_ids)

    pending_validation = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_validation")
    )
    pending_validation["status"] = "pending"
    pending_validation["publication_eligible"] = False
    pending_validation["checks"] = []
    validator.validate(pending_validation)

    published_validation = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_validation")
    )
    validator.validate(published_validation)
    _assert_voice_validation_semantics(published_validation)
    skipped_required = copy.deepcopy(published_validation)
    skipped_required["checks"][0]["status"] = "skipped"
    with pytest.raises(ValidationError):
        validator.validate(skipped_required)
    no_required_checks = copy.deepcopy(published_validation)
    for check in no_required_checks["checks"]:
        check["required"] = False
    with pytest.raises(ValidationError):
        validator.validate(no_required_checks)

    missing_reusable_tts = copy.deepcopy(published_validation)
    missing_reusable_tts["checks"] = [
        check for check in missing_reusable_tts["checks"] if check["name"] != "reusable_tts"
    ]
    with pytest.raises(ValidationError):
        validator.validate(missing_reusable_tts)

    skipped_reusable_tts = copy.deepcopy(published_validation)
    next(check for check in skipped_reusable_tts["checks"] if check["name"] == "reusable_tts")[
        "status"
    ] = "skipped"
    with pytest.raises(ValidationError):
        validator.validate(skipped_reusable_tts)

    failed_without_failed_check = copy.deepcopy(published_validation)
    failed_without_failed_check["status"] = "failed"
    failed_without_failed_check["publication_eligible"] = False
    with pytest.raises(ValidationError):
        validator.validate(failed_without_failed_check)
    failed_without_failed_check["checks"][0]["status"] = "failed"
    validator.validate(failed_without_failed_check)
    _assert_voice_validation_semantics(failed_without_failed_check)

    duplicate_check_names = copy.deepcopy(published_validation)
    duplicate_check_names["checks"].append(
        {
            "name": duplicate_check_names["checks"][0]["name"],
            "required": False,
            "status": "passed",
            "detail": "duplicate name with different content",
        }
    )
    with pytest.raises(ValueError, match="unique"):
        _assert_voice_validation_semantics(duplicate_check_names)

    publication = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_publication")
    )
    published_validation = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_validation")
    )
    published_candidate = copy.deepcopy(candidate)
    published_candidate["status"] = "published"
    published_candidate["validation_job_id"] = publication["validation_job_id"]
    published_candidate["published_voice_id"] = publication["voice_id"]
    published_candidate["published_voice_revision"] = publication["voice_revision"]
    published_draft = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_draft")
    )
    published_draft["status"] = "published"
    published_draft["current_voice_id"] = publication["voice_id"]
    published_voice = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_revision")
    )
    validator.validate(published_candidate)
    validator.validate(publication)
    _assert_voice_publication_semantics(
        publication,
        published_draft,
        published_candidate,
        published_validation,
        published_voice,
    )

    different_validation_job = copy.deepcopy(publication)
    different_validation_job["validation_job_id"] = "job_other_validation"
    validator.validate(different_validation_job)
    with pytest.raises(ValueError, match="validation jobs"):
        _assert_voice_publication_semantics(
            different_validation_job,
            published_draft,
            published_candidate,
            published_validation,
            published_voice,
        )

    different_validation_revision = copy.deepcopy(published_validation)
    different_validation_revision["draft_revision"] = "2"
    validator.validate(different_validation_revision)
    with pytest.raises(ValueError, match="different draft revision"):
        _assert_voice_publication_semantics(
            publication,
            published_draft,
            published_candidate,
            different_validation_revision,
            published_voice,
        )

    different_voice_binding = copy.deepcopy(published_voice)
    different_voice_binding["model_binding"]["revision"] = "4"
    validator.validate(different_voice_binding)
    with pytest.raises(ValueError, match="different model bindings"):
        _assert_voice_publication_semantics(
            publication,
            published_draft,
            published_candidate,
            published_validation,
            different_voice_binding,
        )

    publication_without_job = copy.deepcopy(publication)
    del publication_without_job["validation_job_id"]
    with pytest.raises(ValidationError):
        validator.validate(publication_without_job)

    published_without_preview = copy.deepcopy(published_candidate)
    published_without_preview["preview_artifact_ids"] = []
    with pytest.raises(ValidationError):
        validator.validate(published_without_preview)

    cloned_voice_without_source_digest = copy.deepcopy(published_voice)
    cloned_voice_without_source_digest["source"]["type"] = "cloned"
    with pytest.raises(ValidationError):
        validator.validate(cloned_voice_without_source_digest)

    builtin_voice_with_draft = copy.deepcopy(published_voice)
    builtin_voice_with_draft["source"]["type"] = "builtin"
    with pytest.raises(ValidationError):
        validator.validate(builtin_voice_with_draft)

    voice_revision = copy.deepcopy(
        next(e for e in examples if e["resource_type"] == "voice_revision")
    )
    retired_voice = copy.deepcopy(voice_revision)
    retired_voice["status"] = "retired"
    retired_voice["retired_at"] = "2026-10-22T10:00:00Z"
    validator.validate(retired_voice)
    retired_voice_without_time = copy.deepcopy(retired_voice)
    retired_voice_without_time["retired_at"] = None
    with pytest.raises(ValidationError):
        validator.validate(retired_voice_without_time)
    published_voice_with_retirement_time = copy.deepcopy(voice_revision)
    published_voice_with_retirement_time["retired_at"] = "2026-10-22T10:00:00Z"
    with pytest.raises(ValidationError):
        validator.validate(published_voice_with_retirement_time)
    disabled_voice = copy.deepcopy(retired_voice)
    disabled_voice["status"] = "disabled"
    disabled_voice["disabled_reason"] = "operator emergency stop"
    validator.validate(disabled_voice)
    disabled_voice_without_reason = copy.deepcopy(disabled_voice)
    disabled_voice_without_reason["disabled_reason"] = None
    with pytest.raises(ValidationError):
        validator.validate(disabled_voice_without_reason)
    retired_voice_with_disable_reason = copy.deepcopy(retired_voice)
    retired_voice_with_disable_reason["disabled_reason"] = "not a disable"
    with pytest.raises(ValidationError):
        validator.validate(retired_voice_with_disable_reason)


def test_disable_commands_require_a_reason_on_admin_and_speech_routes() -> None:
    lifecycle = _load_json(_CONTRACTS / "shared" / "lifecycle.schema.json")
    validator = _validator({"$ref": "#/$defs/RetirementRequest", "$defs": lifecycle["$defs"]})
    validator.validate({"mode": "retire"})
    validator.validate({"mode": "retire", "reason": "scheduled rollback"})
    validator.validate({"mode": "disable", "reason": "operator emergency stop"})
    for invalid in (
        {"mode": "disable"},
        {"mode": "disable", "reason": None},
        {"mode": "disable", "reason": ""},
        {"mode": "remove"},
    ):
        with pytest.raises(ValidationError):
            validator.validate(invalid)

    speech = _resolved_yaml(_SPEECH_OPENAPI)
    admin = _resolved_yaml(_ADMIN_OPENAPI)
    _component_validator(speech, "VoiceRetirementRequest").validate(
        {"mode": "disable", "reason": "operator emergency stop"}
    )
    with pytest.raises(ValidationError):
        _component_validator(speech, "VoiceRetirementRequest").validate({"mode": "disable"})
    _component_validator(admin, "ModelBindingRetirementRequest").validate(
        {"mode": "disable", "reason": "operator emergency stop"}
    )
    with pytest.raises(ValidationError):
        _component_validator(admin, "ModelBindingRetirementRequest").validate({"mode": "disable"})


def test_realtime_events_positive_and_negative_examples() -> None:
    schema = _load_json(_REALTIME_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = _validator(schema)
    examples = cast(list[dict[str, Any]], schema["examples"])
    for example in examples:
        validator.validate(example)

    minimal_standard_update: dict[str, Any] = {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm"},
                    "transcription": {"model": "asr-quality"},
                },
            },
        },
    }
    validator.validate(minimal_standard_update)

    unsupported_include = copy.deepcopy(minimal_standard_update)
    unsupported_include["session"]["include"] = ["item.input_audio_transcription.logprobs"]
    with pytest.raises(ValidationError):
        validator.validate(unsupported_include)

    unsupported_response = copy.deepcopy(examples[0])
    unsupported_response["type"] = "response.create"
    with pytest.raises(ValidationError):
        validator.validate(unsupported_response)

    oversized_append = copy.deepcopy(
        next(e for e in examples if e["type"] == "input_audio_buffer.append")
    )
    oversized_append["audio"] = "A" * 65_540
    with pytest.raises(ValidationError):
        validator.validate(oversized_append)

    missing_event_id = copy.deepcopy(
        next(e for e in examples if e["type"] == "nexus.tts.append_text")
    )
    del missing_event_id["event_id"]
    with pytest.raises(ValidationError):
        validator.validate(missing_event_id)

    missing_content_index = {
        "type": "conversation.item.input_audio_transcription.delta",
        "event_id": "evt_delta_missing_content_index",
        "session_id": "sess_demo",
        "seq": 14,
        "stream_epoch": 1,
        "sent_at": "2026-10-22T00:00:12Z",
        "item_id": "item_1",
        "delta": "hel",
    }
    with pytest.raises(ValidationError):
        validator.validate(missing_content_index)

    operation_done = copy.deepcopy(next(e for e in examples if e["type"] == "nexus.operation.done"))
    missing_success_receipt = copy.deepcopy(operation_done)
    missing_success_receipt["receipt"] = None
    with pytest.raises(ValidationError):
        validator.validate(missing_success_receipt)
    succeeded_with_unverified_cleanup = copy.deepcopy(operation_done)
    succeeded_with_unverified_cleanup["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        validator.validate(succeeded_with_unverified_cleanup)
    operation_done["status"] = "failed"
    operation_done["error"] = None
    operation_done["reason"] = None
    with pytest.raises(ValidationError):
        validator.validate(operation_done)
    operation_done["reason"] = "cancellation_unconfirmed"
    validator.validate(operation_done)
    operation_done["status"] = "succeeded"
    operation_done["reason"] = None
    operation_done["error"] = {
        "code": "engine_failure",
        "type": "api_error",
        "message": "synthetic failure",
        "param": None,
        "request_id": "req_demo",
        "retryable": False,
    }
    with pytest.raises(ValidationError):
        validator.validate(operation_done)
    operation_done["status"] = "cancelled"
    operation_done["reason"] = None
    operation_done["error"] = None
    operation_done["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        validator.validate(operation_done)
    operation_done["cleanup_state"] = "complete"
    validator.validate(operation_done)
    operation_done["status"] = "failed"
    operation_done["reason"] = "cancellation_unconfirmed"
    operation_done["cleanup_state"] = "quarantined"
    validator.validate(operation_done)

    for example in examples:
        unknown_field = copy.deepcopy(example)
        unknown_field["unexpected"] = True
        with pytest.raises(ValidationError):
            validator.validate(unknown_field)


def test_openai_realtime_sdk_events_match_declared_contract() -> None:
    """Prove the official SDK's transcription-session event shape is accepted."""

    class CapturingWebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message: str) -> None:
            self.messages.append(message)

    schema = _load_json(_REALTIME_SCHEMA)
    validator = _validator(schema)
    websocket = CapturingWebSocket()
    connection = RealtimeConnection(cast(Any, websocket))

    connection.session.update(
        session={
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "asr-quality", "language": "zh"},
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": 900,
                    },
                }
            },
        }
    )
    connection.input_audio_buffer.append(audio="AAAAAA==")
    connection.input_audio_buffer.commit()
    connection.input_audio_buffer.clear()

    events = [json.loads(message) for message in websocket.messages]
    assert [event["type"] for event in events] == [
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "input_audio_buffer.clear",
    ]
    for event in events:
        validator.validate(event)

    session_event: dict[str, Any] = {
        "type": "session.created",
        "event_id": "evt_created",
        "session": {
            "id": "sess_demo",
            "object": "realtime.transcription_session",
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "asr-quality"},
                    "turn_detection": None,
                }
            },
            "expires_at": 1761091200,
            "nexus": {
                "limits": {
                    "max_input_chunk_ms": 100,
                    "max_message_bytes": 65536,
                    "input_credit_samples": 24000,
                    "output_credit_samples": 0,
                    "max_segment_ms": 20000,
                    "max_session_duration_ms": 3600000,
                }
            },
        },
    }
    validator.validate(session_event)
    parsed = SessionCreatedEvent.model_validate(session_event)
    assert parsed.session.type == "transcription"
    parsed_session = cast(Any, parsed.session)
    assert parsed_session.nexus == {"limits": session_event["session"]["nexus"]["limits"]}

    committed_event = {
        "type": "input_audio_buffer.committed",
        "event_id": "evt_committed",
        "item_id": "item_1",
        "previous_item_id": None,
    }
    validator.validate(committed_event)
    committed = InputAudioBufferCommittedEvent.model_validate(committed_event)
    assert committed.item_id == "item_1"

    session_updated_event = {
        **session_event,
        "type": "session.updated",
        "event_id": "evt_updated",
    }
    validator.validate(session_updated_event)
    SessionUpdatedEvent.model_validate(session_updated_event)

    standard_server_events: list[tuple[dict[str, Any], type[Any]]] = [
        (
            {
                "type": "input_audio_buffer.cleared",
                "event_id": "evt_cleared",
            },
            InputAudioBufferClearedEvent,
        ),
        (
            {
                "type": "input_audio_buffer.speech_started",
                "event_id": "evt_speech_started",
                "item_id": "item_1",
                "audio_start_ms": 100,
            },
            InputAudioBufferSpeechStartedEvent,
        ),
        (
            {
                "type": "input_audio_buffer.speech_stopped",
                "event_id": "evt_speech_stopped",
                "item_id": "item_1",
                "audio_end_ms": 900,
            },
            InputAudioBufferSpeechStoppedEvent,
        ),
        (
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "event_id": "evt_delta",
                "item_id": "item_1",
                "content_index": 0,
                "delta": "hel",
                "logprobs": [{"token": "hel", "bytes": [104, 101, 108], "logprob": -0.1}],
            },
            ConversationItemInputAudioTranscriptionDeltaEvent,
        ),
        (
            {
                "type": "conversation.item.input_audio_transcription.failed",
                "event_id": "evt_failed",
                "item_id": "item_1",
                "content_index": 0,
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_audio",
                    "message": "synthetic audio cannot be decoded",
                    "param": None,
                },
            },
            ConversationItemInputAudioTranscriptionFailedEvent,
        ),
    ]
    for payload, sdk_type in standard_server_events:
        validator.validate(payload)
        sdk_type.model_validate(payload)

    completed_event = {
        "type": "conversation.item.input_audio_transcription.completed",
        "event_id": "evt_completed",
        "item_id": "item_1",
        "content_index": 0,
        "transcript": "hello",
        "usage": {"type": "duration", "seconds": 1.0},
        "logprobs": [{"token": "hello", "bytes": [104, 101, 108, 108, 111], "logprob": -0.1}],
    }
    validator.validate(completed_event)
    completed = ConversationItemInputAudioTranscriptionCompletedEvent.model_validate(
        completed_event
    )
    assert completed.transcript == "hello"
    assert cast(Any, completed.usage).seconds == 1.0

    error_event = {
        "type": "error",
        "event_id": "evt_error",
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_event",
            "message": "unsupported event",
            "param": None,
            "request_id": "req_demo",
            "retryable": False,
        },
    }
    validator.validate(error_event)
    parsed_error = RealtimeErrorEvent.model_validate(error_event)
    assert parsed_error.error.code == "invalid_event"

    polluted_standard_event = {
        **committed_event,
        "session_id": "sess_demo",
    }
    with pytest.raises(ValidationError):
        validator.validate(polluted_standard_event)


def test_binding_instance_and_qualification_positive_and_negative_examples() -> None:
    for path in (_BINDING_SCHEMA, _INSTANCE_SCHEMA, _QUALIFICATION_SCHEMA):
        schema = _load_json(path)
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = _validator(schema)
        examples = cast(list[dict[str, Any]], schema["examples"])
        assert examples
        for example in examples:
            validator.validate(example)
            if path == _QUALIFICATION_SCHEMA:
                _assert_qualification_semantics(example)

    binding_schema = _load_json(_BINDING_SCHEMA)
    binding_validator = _validator(binding_schema)
    binding_example = copy.deepcopy(cast(list[dict[str, Any]], binding_schema["examples"])[0])
    assert binding_example["revision_id"] == "mbrev-asr-quality-7"
    production_draft = copy.deepcopy(binding_example)
    production_draft["release_tier"] = "production"
    production_draft["publication"] = {
        "status": "draft",
        "published_at": None,
        "retired_at": None,
        "capacity_manifest_id": None,
        "qualification_report_id": None,
        "disabled_reason": None,
    }
    binding_validator.validate(production_draft)
    draft_with_publish_timestamp = copy.deepcopy(production_draft)
    draft_with_publish_timestamp["publication"]["published_at"] = "2026-10-22T09:00:00Z"
    with pytest.raises(ValidationError):
        binding_validator.validate(draft_with_publish_timestamp)

    retired_binding = copy.deepcopy(binding_example)
    retired_binding["publication"]["status"] = "retired"
    retired_binding["publication"]["retired_at"] = "2026-10-22T10:00:00Z"
    binding_validator.validate(retired_binding)
    retired_without_timestamp = copy.deepcopy(retired_binding)
    retired_without_timestamp["publication"]["retired_at"] = None
    with pytest.raises(ValidationError):
        binding_validator.validate(retired_without_timestamp)

    instance_schema = _load_json(_INSTANCE_SCHEMA)
    instance_validator = _validator(instance_schema)
    ready_instance = copy.deepcopy(cast(list[dict[str, Any]], instance_schema["examples"])[0])
    assert ready_instance["binding_revision_id"] == "mbrev-asr-quality-7"
    ready_instance["readiness"]["accepting_new_work"] = False
    with pytest.raises(ValidationError):
        instance_validator.validate(ready_instance)
    healthy_with_quarantine_reason = copy.deepcopy(
        cast(list[dict[str, Any]], instance_schema["examples"])[0]
    )
    healthy_with_quarantine_reason["quarantine_reason"] = "not quarantined"
    with pytest.raises(ValidationError):
        instance_validator.validate(healthy_with_quarantine_reason)
    quarantined_instance = copy.deepcopy(cast(list[dict[str, Any]], instance_schema["examples"])[0])
    quarantined_instance["state"] = "quarantined"
    quarantined_instance["readiness"]["accepting_new_work"] = False
    quarantined_instance["quarantine_reason"] = "stop evidence unavailable"
    instance_validator.validate(quarantined_instance)
    quarantined_instance["quarantine_reason"] = None
    with pytest.raises(ValidationError):
        instance_validator.validate(quarantined_instance)
    quarantined_instance["quarantine_reason"] = "   "
    with pytest.raises(ValidationError):
        instance_validator.validate(quarantined_instance)

    qualification_schema = _load_json(_QUALIFICATION_SCHEMA)
    qualification_validator = _validator(qualification_schema)
    production = copy.deepcopy(cast(list[dict[str, Any]], qualification_schema["examples"])[0])
    production["release_tier"] = "production"
    with pytest.raises(ValidationError):
        qualification_validator.validate(production)
    completed_production = copy.deepcopy(production)
    for scenario in completed_production["scenarios"]:
        scenario["status"] = "passed"
    completed_production["required_scenarios_passed"] = True
    completed_production["overall_status"] = "pass"
    qualification_validator.validate(completed_production)
    production_without_expiry = copy.deepcopy(completed_production)
    production_without_expiry["expires_at"] = None
    with pytest.raises(ValidationError):
        qualification_validator.validate(production_without_expiry)
    no_required_scenarios = copy.deepcopy(completed_production)
    no_required_scenarios["release_tier"] = "experimental"
    for scenario in no_required_scenarios["scenarios"]:
        scenario["required"] = False
    with pytest.raises(ValidationError):
        qualification_validator.validate(no_required_scenarios)
    false_required_pass_claim = copy.deepcopy(
        cast(list[dict[str, Any]], qualification_schema["examples"])[0]
    )
    false_required_pass_claim["required_scenarios_passed"] = True
    with pytest.raises(ValidationError):
        qualification_validator.validate(false_required_pass_claim)
    pass_without_required = copy.deepcopy(completed_production)
    pass_without_required["required_scenarios_passed"] = False
    with pytest.raises(ValidationError):
        qualification_validator.validate(pass_without_required)
    fail_without_failed_required = copy.deepcopy(completed_production)
    fail_without_failed_required["overall_status"] = "fail"
    with pytest.raises(ValidationError):
        qualification_validator.validate(fail_without_failed_required)
    failed_required = copy.deepcopy(cast(list[dict[str, Any]], qualification_schema["examples"])[0])
    failed_required["scenarios"][0]["status"] = "failed"
    failed_required["required_scenarios_passed"] = False
    failed_required["overall_status"] = "fail"
    qualification_validator.validate(failed_required)
    _assert_qualification_semantics(failed_required)
    incompletely_failed_required = copy.deepcopy(failed_required)
    incompletely_failed_required["overall_status"] = "incomplete"
    with pytest.raises(ValidationError):
        qualification_validator.validate(incompletely_failed_required)
    optional_failure = copy.deepcopy(completed_production)
    optional_failure["scenarios"][0]["required"] = False
    optional_failure["scenarios"][0]["status"] = "failed"
    qualification_validator.validate(optional_failure)
    _assert_qualification_semantics(optional_failure)
    optional_failure_without_limitation = copy.deepcopy(optional_failure)
    optional_failure_without_limitation["limitations"] = []
    qualification_validator.validate(optional_failure_without_limitation)
    with pytest.raises(ValueError, match="optional scenario"):
        _assert_qualification_semantics(optional_failure_without_limitation)
    expired_before_qualification = copy.deepcopy(completed_production)
    expired_before_qualification["expires_at"] = expired_before_qualification["qualified_at"]
    qualification_validator.validate(expired_before_qualification)
    with pytest.raises(ValueError, match="qualified_at must precede"):
        _assert_qualification_semantics(expired_before_qualification)
    invalid_qualification_time = copy.deepcopy(completed_production)
    invalid_qualification_time["qualified_at"] = "not-a-date-time"
    with pytest.raises(ValidationError):
        qualification_validator.validate(invalid_qualification_time)
    shared_domains = _load_json(_CONTRACTS / "shared" / "capability-domains.schema.json")
    raw_qualification_schema = json.loads(_QUALIFICATION_SCHEMA.read_text(encoding="utf-8"))
    assert raw_qualification_schema["$defs"]["MemoryArchitecture"] == {
        "$ref": "shared/capability-domains.schema.json#/$defs/MemoryArchitecture"
    }
    assert shared_domains["$defs"]["MemoryArchitecture"]["enum"] == [
        "unified_memory",
        "discrete_device_memory",
    ]
    qualification_topology = copy.deepcopy(
        cast(list[dict[str, Any]], qualification_schema["examples"])[0]["topology"]
    )
    assert "host_node_id" in qualification_topology
    with pytest.raises(ValidationError):
        _validator({"$ref": "#/$defs/Topology", "$defs": qualification_schema["$defs"]}).validate(
            {
                **qualification_topology,
                "memory_architecture": "host_only",
            }
        )
    qualification_example = cast(list[dict[str, Any]], qualification_schema["examples"])[0]
    engine_components = cast(
        dict[str, dict[str, Any]], qualification_example["software"]["engine_components"]
    )
    assert {
        "stage-silero-vad",
        "stage-qwen3-asr-0.6b-preview",
        "stage-qwen3-asr-1.7b-review",
    } == set(engine_components)
    assert set(qualification_example["test_suite"]["scenario_ids"]) == {
        scenario["scenario_id"] for scenario in qualification_example["scenarios"]
    }
    mismatched_scenario_ids = copy.deepcopy(qualification_example)
    mismatched_scenario_ids["test_suite"]["scenario_ids"][0] = "scenario-not-recorded"
    # Structural schema validation cannot compare array values across properties.
    qualification_validator.validate(mismatched_scenario_ids)
    with pytest.raises(ValueError, match="one-to-one"):
        _assert_qualification_semantics(mismatched_scenario_ids)
    duplicate_scenario = copy.deepcopy(qualification_example)
    duplicate_scenario["scenarios"].append(copy.deepcopy(duplicate_scenario["scenarios"][0]))
    qualification_validator.validate(duplicate_scenario)
    with pytest.raises(ValueError, match="duplicate scenario_id"):
        _assert_qualification_semantics(duplicate_scenario)
    missing_engine_identity = copy.deepcopy(qualification_example)
    del missing_engine_identity["software"]["engine_components"]["stage-silero-vad"]["image_digest"]
    with pytest.raises(ValidationError):
        qualification_validator.validate(missing_engine_identity)

    stopped_instance = copy.deepcopy(
        next(
            example
            for example in cast(list[dict[str, Any]], instance_schema["examples"])
            if example["state"] == "stopped"
        )
    )
    stopped_instance["observed"]["stop_evidence"] = None
    with pytest.raises(ValidationError):
        instance_validator.validate(stopped_instance)


def test_model_binding_publication_separates_target_and_current_revision() -> None:
    admin = _resolved_yaml(_ADMIN_OPENAPI)
    validator = _component_validator(admin, "ModelBindingPublicationRequest")
    request: dict[str, Any] = {
        "expected_revision": "8",
        "expected_current_revision": "7",
        "capacity_manifest_id": "capm-asr-quality-8",
        "qualification_report_id": "qrep-asr-quality-8",
    }
    validator.validate(request)
    first_publication = copy.deepcopy(request)
    first_publication["expected_current_revision"] = None
    validator.validate(first_publication)
    missing_current = copy.deepcopy(request)
    del missing_current["expected_current_revision"]
    with pytest.raises(ValidationError):
        validator.validate(missing_current)
    invalid_current = copy.deepcopy(request)
    invalid_current["expected_current_revision"] = "0"
    with pytest.raises(ValidationError):
        validator.validate(invalid_current)
    unknown_field = copy.deepcopy(request)
    unknown_field["binding_revision_id"] = "mbrev-asr-quality-8"
    with pytest.raises(ValidationError):
        validator.validate(unknown_field)


def test_model_binding_summary_distinguishes_latest_and_current_revision() -> None:
    admin = _resolved_yaml(_ADMIN_OPENAPI)
    validator = _component_validator(admin, "ModelBindingSummary")
    draft_only = {
        "binding_id": "asr-quality",
        "latest_revision": "1",
        "current_revision": None,
        "logical_model": "asr-quality",
        "tasks": ["asr_file"],
        "release_tier": "production",
        "publication_status": "draft",
    }
    validator.validate(draft_only)

    active_with_new_draft = {
        **draft_only,
        "latest_revision": "2",
        "current_revision": "1",
        "publication_status": "draft",
    }
    validator.validate(active_with_new_draft)

    for missing_field in ("latest_revision", "current_revision"):
        missing = copy.deepcopy(active_with_new_draft)
        del missing[missing_field]
        with pytest.raises(ValidationError):
            validator.validate(missing)


def test_profile_revisions_have_explicit_admin_lifecycle() -> None:
    scenario = _load_json(_SCENARIO_SCHEMA)
    profile_validator = _validator(scenario)
    examples = cast(list[dict[str, Any]], scenario["examples"])
    published = next(
        example for example in examples if example["publication"]["status"] == "published"
    )
    draft = next(example for example in examples if example["publication"]["status"] == "draft")
    profile_validator.validate(published)
    profile_validator.validate(draft)

    production_draft = copy.deepcopy(draft)
    production_draft["release_tier"] = "production"
    profile_validator.validate(production_draft)

    production_with_experimental_validation = copy.deepcopy(published)
    production_with_experimental_validation["release_tier"] = "production"
    with pytest.raises(ValidationError):
        profile_validator.validate(production_with_experimental_validation)

    published_without_validation = copy.deepcopy(published)
    del published_without_validation["validation"]
    with pytest.raises(ValidationError):
        profile_validator.validate(published_without_validation)

    draft_with_publish_time = copy.deepcopy(draft)
    draft_with_publish_time["publication"]["published_at"] = "2026-10-22T09:00:00Z"
    with pytest.raises(ValidationError):
        profile_validator.validate(draft_with_publish_time)

    admin = _resolved_yaml(_ADMIN_OPENAPI)
    create_validator = _component_validator(admin, "ProfileRevisionCreateRequest")
    create_request = {
        key: copy.deepcopy(value)
        for key, value in draft.items()
        if key
        not in {
            "schema_version",
            "revision",
            "publication",
            "supersedes_revision",
        }
    }
    create_request["expected_latest_revision"] = None
    create_validator.validate(create_request)
    missing_cas = copy.deepcopy(create_request)
    del missing_cas["expected_latest_revision"]
    with pytest.raises(ValidationError):
        create_validator.validate(missing_cas)
    unknown_field = copy.deepcopy(create_request)
    unknown_field["priority"] = 10
    with pytest.raises(ValidationError):
        create_validator.validate(unknown_field)

    publication_validator = _component_validator(admin, "ProfilePublicationRequest")
    publication_request = {
        "expected_revision": "2",
        "expected_current_revision": "1",
        "validation": {
            **copy.deepcopy(published["validation"]),
            "profile_id": draft["profile_id"],
            "revision": draft["revision"],
        },
    }
    publication_validator.validate(publication_request)

    mismatched_validation = copy.deepcopy(published)
    mismatched_validation["validation"]["revision"] = "2"
    with pytest.raises(ValueError, match="does not identify"):
        _assert_scenario_profile_semantics(mismatched_validation)

    activation_validator = _component_validator(admin, "ProfileActivationRequest")
    activation_validator.validate({"expected_current_revision": "2"})
    activation_validator.validate({"expected_current_revision": None})


def test_revisioned_patch_requires_an_actual_change() -> None:
    validators = {
        "WorkspacePatch": _component_validator(_resolved_yaml(_ADMIN_OPENAPI), "WorkspacePatch"),
        "ProjectPatch": _component_validator(_resolved_yaml(_WORKBENCH_OPENAPI), "ProjectPatch"),
        "RenderUnitPatch": _component_validator(
            _resolved_yaml(_WORKBENCH_OPENAPI), "RenderUnitPatch"
        ),
    }
    no_op_requests = {
        "WorkspacePatch": {"expected_metadata_revision": 2},
        "ProjectPatch": {"expected_metadata_revision": 2},
        "RenderUnitPatch": {"expected_revision": "2"},
    }
    changed_requests = {
        "WorkspacePatch": {"expected_metadata_revision": 2, "status": "disabled"},
        "ProjectPatch": {"expected_metadata_revision": 2, "name": "Renamed"},
        "RenderUnitPatch": {"expected_revision": "2", "text": "Revised text"},
    }
    for name, validator in validators.items():
        with pytest.raises(ValidationError):
            validator.validate(no_op_requests[name])
        validator.validate(changed_requests[name])


def test_runtime_state_domains_are_shared_by_admin_and_internal_contracts() -> None:
    shared = _load_json(_RUNTIME_STATES_SCHEMA)
    shared_defs = cast(dict[str, dict[str, Any]], shared["$defs"])
    instance_states = cast(list[str], shared_defs["InstanceState"]["enum"])
    observed_states = cast(list[str], shared_defs["ObservedInstanceState"]["enum"])
    assert instance_states == [
        "preparing",
        "loading",
        "warming_up",
        "ready",
        "draining",
        "stopping",
        "stopped",
        "failed",
        "quarantined",
    ]
    assert observed_states == ["absent", *instance_states]

    admin = _resolved_yaml(_ADMIN_OPENAPI)
    _component_validator(admin, "InstanceState").validate("warming_up")
    with pytest.raises(ValidationError):
        _component_validator(admin, "InstanceState").validate("warming")

    instance_schema = _load_json(_INSTANCE_SCHEMA)
    instance_validator = _validator(instance_schema)
    ready_instance = copy.deepcopy(cast(list[dict[str, Any]], instance_schema["examples"])[0])
    ready_instance["state"] = "failed"
    ready_instance["readiness"]["accepting_new_work"] = False
    instance_validator.validate(ready_instance)
    ready_instance["readiness"]["accepting_new_work"] = True
    with pytest.raises(ValidationError):
        instance_validator.validate(ready_instance)
    ready_instance["readiness"]["accepting_new_work"] = False
    ready_instance["state"] = "unknown_quarantined"
    with pytest.raises(ValidationError):
        instance_validator.validate(ready_instance)

    canonical_instance = copy.deepcopy(cast(list[dict[str, Any]], instance_schema["examples"])[0])
    assert canonical_instance["host_node_id"] == "node-gb10-1"
    assert canonical_instance["gateway_id"] == "gateway-1"
    legacy_node_alias = copy.deepcopy(canonical_instance)
    legacy_node_alias["node_id"] = legacy_node_alias.pop("host_node_id")
    with pytest.raises(ValidationError):
        instance_validator.validate(legacy_node_alias)
    missing_gateway = copy.deepcopy(canonical_instance)
    del missing_gateway["gateway_id"]
    with pytest.raises(ValidationError):
        instance_validator.validate(missing_gateway)


def test_public_contract_enums_do_not_contain_yaml_boolean_typos() -> None:
    for path in sorted(_CONTRACTS.glob("**/*")):
        if path.suffix not in {".json", ".yaml", ".yml"}:
            continue
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw) if path.suffix == ".json" else load_yaml(raw)
        _assert_string_enum_values_are_quoted(document, str(path.relative_to(_ROOT)))


def test_contract_documents_do_not_contain_duplicate_keys() -> None:
    for path in sorted(_CONTRACTS.glob("**/*")):
        if path.suffix == ".json":
            json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        elif path.suffix in {".yaml", ".yml"}:
            yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=_UniqueKeyYamlLoader,
            )


def test_contract_schema_arrays_do_not_repeat_semantic_values() -> None:
    """Reject duplicated required fields, enum members, and scalar type entries."""

    checked_keywords = {"required", "enum", "type"}

    def walk(node: Any, path: Path, pointer: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in checked_keywords and isinstance(value, list):
                    encoded = [
                        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value
                    ]
                    duplicates = sorted({item for item in encoded if encoded.count(item) > 1})
                    assert not duplicates, f"{path}:{pointer}/{key}: {duplicates!r}"
                walk(value, path, f"{pointer}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path, f"{pointer}/{index}")

    for path in sorted(_CONTRACTS.glob("**/*")):
        if path.suffix == ".json":
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix in {".yaml", ".yml"}:
            raw = load_yaml(path.read_text(encoding="utf-8"))
        else:
            continue
        walk(raw, path.relative_to(_ROOT), "")


def test_contract_definitions_do_not_duplicate_inline_schema_bodies() -> None:
    """Keep cross-protocol domains in shared schemas instead of copied definitions."""

    documents = list(_CONTRACTS.rglob("*.schema.json")) + list(_CONTRACTS.rglob("*.openapi.yaml"))
    definitions: dict[str, list[str]] = {}
    for path in documents:
        raw: object
        if path.suffix == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = load_yaml(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        document = cast(dict[str, Any], raw)
        candidates = {
            **cast(dict[str, Any], document.get("$defs", {})),
            **cast(
                dict[str, Any],
                cast(dict[str, Any], document.get("components", {})).get("schemas", {}),
            ),
        }
        for name, value in candidates.items():
            if not isinstance(value, dict):
                continue
            if "$ref" in value and set(value) <= {"$ref", "description"}:
                continue
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
            definitions.setdefault(canonical, []).append(f"{path.relative_to(_ROOT)}:{name}")

    duplicates = [places for places in definitions.values() if len(places) > 1]
    assert not duplicates, "duplicate inline schemas; use a shared $ref:\n" + "\n".join(
        repr(places) for places in duplicates
    )


def test_contract_definitions_are_reachable_from_document_roots() -> None:
    """Reject orphan schema bodies that generators can mistake for live protocol."""
    for path in sorted(_CONTRACTS.rglob("*.schema.json")):
        if "shared" in path.relative_to(_CONTRACTS).parts:
            continue
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        document = cast(dict[str, Any], raw)
        definitions = cast(dict[str, Any], document.get("$defs", {}))
        root = {key: value for key, value in document.items() if key != "$defs"}
        reachable = _reachable_local_definitions(root, definitions, "#/$defs/")
        assert set(definitions) == reachable, (
            f"{path.relative_to(_ROOT)} has unreachable definitions: "
            f"{sorted(set(definitions) - reachable)}"
        )

    for path in sorted(_CONTRACTS.rglob("*.openapi.yaml")):
        openapi_raw: object = load_yaml(path.read_text(encoding="utf-8"))
        assert isinstance(openapi_raw, dict)
        document = cast(dict[str, Any], openapi_raw)
        components = cast(dict[str, Any], document.get("components", {}))
        definitions = cast(dict[str, Any], components.get("schemas", {}))
        root = copy.deepcopy(document)
        del root["components"]["schemas"]
        reachable = _reachable_local_definitions(
            root,
            definitions,
            "#/components/schemas/",
        )
        assert set(definitions) == reachable, (
            f"{path.relative_to(_ROOT)} has unreachable components: "
            f"{sorted(set(definitions) - reachable)}"
        )


def test_public_errors_share_one_openai_compatible_envelope() -> None:
    shared = _load_json(_SHARED_ERRORS_SCHEMA)
    sample = {
        "error": {
            "code": "unsupported_parameter",
            "type": "invalid_request_error",
            "message": "synthetic parameter is not supported",
            "param": "temperature",
            "request_id": "req_contract",
            "retryable": False,
            "retry_after_ms": None,
        }
    }
    _validator({"$ref": "#/$defs/ErrorResponse", "$defs": shared["$defs"]}).validate(sample)

    speech = _resolved_yaml(_SPEECH_OPENAPI)
    admin = _resolved_yaml(_ADMIN_OPENAPI)
    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    for schema, name in (
        (speech, "ErrorResponse"),
        (admin, "ErrorResponse"),
        (workbench, "ErrorResponse"),
    ):
        _component_validator(schema, name).validate(sample)

    realtime = _load_json(_REALTIME_SCHEMA)
    _validator({"$ref": "#/$defs/ErrorEnvelope", "$defs": realtime["$defs"]}).validate(
        sample["error"]
    )
    for schema, name in ((admin, "ErrorResponse"), (workbench, "ErrorResponse")):
        with pytest.raises(ValidationError):
            _component_validator(schema, name).validate(
                {
                    "error": {
                        **sample["error"],
                        "code": "Not-A-Stable-Code",
                    }
                }
            )

    service_unavailable = copy.deepcopy(sample)
    service_unavailable["error"].update(
        {
            "code": "server_is_overloaded",
            "type": "service_unavailable_error",
            "message": "synthetic capacity is temporarily unavailable",
            "retryable": True,
            "retry_after_ms": 1000,
        }
    )
    _validator({"$ref": "#/$defs/ErrorResponse", "$defs": shared["$defs"]}).validate(
        service_unavailable
    )
    _component_validator(speech, "ErrorResponse").validate(service_unavailable)


def test_standard_tts_instructions_keep_the_openai_domain() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    validator = _component_validator(speech, "SpeechRequest")
    request = {
        "model": "tts-interactive",
        "input": "hello",
        "voice": "narrator",
        "instructions": "x" * 4096,
    }
    validator.validate(request)
    with pytest.raises(ValidationError):
        validator.validate({**request, "instructions": "x" * 4097})


def test_audit_change_summary_records_field_names_without_payloads() -> None:
    admin = _resolved_yaml(_ADMIN_OPENAPI)
    validator = _component_validator(admin, "AuditEvent")
    event: dict[str, Any] = {
        "id": "audit-demo",
        "object": "nexus.audit_event",
        "actor_type": "anonymous",
        "request_id": "req-audit-demo",
        "action": "policy.update",
        "resource_type": "policy",
        "resource_id": "policy-deployment",
        "occurred_at": "2026-10-23T00:00:00Z",
        "policy_revision": "7",
        "change_summary": {
            "changed_fields": ["/quotas/max_concurrent_jobs", "/features/llm_enabled"],
            "reason": "increase admitted concurrency",
        },
    }
    validator.validate(event)

    without_summary = copy.deepcopy(event)
    without_summary["change_summary"] = None
    validator.validate(without_summary)

    payload_summary = copy.deepcopy(event)
    payload_summary["change_summary"]["before"] = "private request body"
    with pytest.raises(ValidationError):
        validator.validate(payload_summary)

    non_pointer_field = copy.deepcopy(event)
    non_pointer_field["change_summary"]["changed_fields"] = ["secret-token"]
    with pytest.raises(ValidationError):
        validator.validate(non_pointer_field)

    duplicate_fields = copy.deepcopy(event)
    duplicate_fields["change_summary"]["changed_fields"] = ["/status", "/status"]
    with pytest.raises(ValidationError):
        validator.validate(duplicate_fields)


def test_execution_receipt_is_shared_by_rest_and_realtime() -> None:
    receipt: dict[str, Any] = {
        "receipt_id": "receipt-demo",
        "request_id": None,
        "requested": {"language": "zh", "seed": 7},
        "effective": {"language": "zh", "seed": 7},
        "observed": {"sample_rate_hz": 24000},
        "missing": [],
        "unknown": [],
    }
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    _component_validator(speech, "ExecutionReceipt").validate(receipt)

    realtime = _load_json(_REALTIME_SCHEMA)
    realtime_receipt = _validator(
        {
            "$ref": "#/$defs/Receipt",
            "$defs": realtime["$defs"],
        }
    )
    realtime_receipt.validate(receipt)

    duplicate_missing = copy.deepcopy(receipt)
    duplicate_missing["missing"] = ["observed.language", "observed.language"]
    with pytest.raises(ValidationError):
        realtime_receipt.validate(duplicate_missing)

    private_receipt = copy.deepcopy(receipt)
    private_receipt["internal_endpoint"] = "http://engine.invalid"
    with pytest.raises(ValidationError):
        realtime_receipt.validate(private_receipt)


def test_speech_overrides_are_shared_with_transport_specific_narrowing() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    rest_tts = _component_validator(speech, "TtsOverrides")
    rest_asr = _component_validator(speech, "AsrOverrides")

    realtime = _load_json(_REALTIME_SCHEMA)
    realtime_tts = _validator(
        {
            "$ref": "#/$defs/TtsOverrides",
            "$defs": realtime["$defs"],
        }
    )
    realtime_asr = _validator(
        {
            "$ref": "#/$defs/AsrOverrides",
            "$defs": realtime["$defs"],
        }
    )

    tts = {
        "language": "zh",
        "text_commit_mode": "sentence",
        "audio_delivery": "chunked",
        "speed": 1.0,
        "seed": 7,
    }
    rest_tts.validate(tts)
    realtime_tts.validate(tts)

    faster = {**tts, "speed": 4.0}
    rest_tts.validate(faster)
    with pytest.raises(ValidationError):
        realtime_tts.validate(faster)

    asr = {
        "language": "zh",
        "preview_interval_ms": 500,
        "max_segment_ms": 20_000,
        "finalization": "full_segment",
        "alignment": "best_effort",
        "diarization": "off",
    }
    rest_asr.validate(asr)
    realtime_asr.validate(asr)
    for validator in (rest_asr, realtime_asr):
        with pytest.raises(ValidationError):
            validator.validate({**asr, "max_segment_ms": 100})


def test_resource_pool_is_cross_node_and_quarantine_release_requires_evidence() -> None:
    admin = _resolved_yaml(_ADMIN_OPENAPI)
    pool_validator = _component_validator(admin, "ResourcePool")
    ready_pool: dict[str, Any] = {
        "id": "asr-realtime",
        "object": "nexus.resource_pool",
        "node_ids": ["node-a", "node-b"],
        "service_class": "realtime",
        "max_active_slots": 4,
        "realtime_reserved_slots": 2,
        "active_slots": 1,
        "quarantine_slots": 0,
        "status": "ready",
    }
    pool_validator.validate(ready_pool)
    _assert_resource_pool_semantics(ready_pool)
    unavailable_pool: dict[str, Any] = {
        "id": "voice-creation",
        "object": "nexus.resource_pool",
        "node_ids": [],
        "service_class": "interactive",
        "max_active_slots": 0,
        "realtime_reserved_slots": 0,
        "active_slots": 0,
        "quarantine_slots": 0,
        "status": "unavailable",
    }
    pool_validator.validate(unavailable_pool)
    _assert_resource_pool_semantics(unavailable_pool)

    ready_without_nodes = {**ready_pool, "node_ids": []}
    with pytest.raises(ValidationError):
        pool_validator.validate(ready_without_nodes)
    ready_without_capacity = {**ready_pool, "max_active_slots": 0}
    with pytest.raises(ValidationError):
        pool_validator.validate(ready_without_capacity)

    reserved_beyond_capacity = {**ready_pool, "realtime_reserved_slots": 5}
    pool_validator.validate(reserved_beyond_capacity)
    with pytest.raises(ValueError, match="reserved slots"):
        _assert_resource_pool_semantics(reserved_beyond_capacity)

    occupied_beyond_capacity = {**ready_pool, "active_slots": 3, "quarantine_slots": 2}
    pool_validator.validate(occupied_beyond_capacity)
    with pytest.raises(ValueError, match="active and quarantined"):
        _assert_resource_pool_semantics(occupied_beyond_capacity)
    with pytest.raises(ValidationError):
        pool_validator.validate(
            {
                "id": "asr-realtime",
                "object": "nexus.resource_pool",
                "node_id": "node-a",
                "service_class": "realtime",
                "max_active_slots": 4,
                "realtime_reserved_slots": 2,
                "active_slots": 1,
                "quarantine_slots": 0,
                "status": "ready",
            }
        )

    request_validator = _component_validator(admin, "QuarantineReleaseRequest")
    release_request: dict[str, Any] = {
        "evidence_kind": "operator_verified_instance_terminated",
        "evidence_digest": "sha256:" + "1" * 64,
        "observed_at": "2026-10-22T00:00:00Z",
        "reason": "Operator verified the old container exited.",
    }
    request_validator.validate(release_request)
    with pytest.raises(ValidationError):
        request_validator.validate(
            {
                "evidence_kind": "engine_abort_ack",
                "evidence_digest": "sha256:" + "1" * 64,
                "observed_at": "2026-10-22T00:00:00Z",
                "reason": "Not an operator-verification request.",
            }
        )
    with pytest.raises(ValidationError):
        request_validator.validate(
            {
                "evidence_kind": "operator_verified_instance_terminated",
                "evidence_digest": "sha256:" + "1" * 64,
                "observed_at": "2026-10-22T00:00:00Z",
                "reason": "   ",
            }
        )

    release_validator = _component_validator(admin, "QuarantineRelease")
    release = {
        "lease_id": "lease-demo",
        "object": "nexus.quarantine_release",
        "lease_state": "released",
        "cleanup_state": "complete",
        "budget_released_at": "2026-10-22T00:01:00Z",
        "evidence_kind": "operator_verified_instance_terminated",
        "evidence_digest": "sha256:" + "1" * 64,
        "evidence_observed_at": "2026-10-22T00:00:00Z",
        "reason": "Operator verified the old container exited.",
        "idempotent_replay": False,
        "released_submit_intent_ids": ["intent-demo"],
    }
    release_validator.validate(release)
    _assert_quarantine_release_semantics(release_request, release)
    missing_observed_at = copy.deepcopy(release)
    del missing_observed_at["evidence_observed_at"]
    with pytest.raises(ValidationError):
        release_validator.validate(missing_observed_at)
    invalid_replay_flag = copy.deepcopy(release)
    invalid_replay_flag["idempotent_replay"] = "true"
    with pytest.raises(ValidationError):
        release_validator.validate(invalid_replay_flag)

    release_with_other_reason = {**release, "reason": "A different reason."}
    release_validator.validate(release_with_other_reason)
    with pytest.raises(ValueError, match="does not echo"):
        _assert_quarantine_release_semantics(release_request, release_with_other_reason)

    release_before_observation = {
        **release,
        "budget_released_at": "2026-10-21T23:59:59Z",
    }
    release_validator.validate(release_before_observation)
    with pytest.raises(ValueError, match="observed after"):
        _assert_quarantine_release_semantics(release_request, release_before_observation)

    unsorted_released_intents = {
        **release,
        "released_submit_intent_ids": ["intent-z", "intent-a"],
    }
    release_validator.validate(unsorted_released_intents)
    with pytest.raises(ValueError, match="not sorted"):
        _assert_quarantine_release_semantics(release_request, unsorted_released_intents)


def test_execution_stream_examples_and_unknown_field_rejection() -> None:
    schema = _load_json(_EXECUTION_STREAM_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = _validator(schema)
    examples = cast(list[dict[str, Any]], schema["examples"])
    for example in examples:
        validator.validate(example)
    assert "stream.error" in {example["type"] for example in examples}

    for example in examples:
        unknown = copy.deepcopy(example)
        unknown["raw_audio_base64"] = "AAAA"
        with pytest.raises(ValidationError):
            validator.validate(unknown)

    stream_open = next(example for example in examples if example["type"] == "stream.open")
    assert isinstance(stream_open["fencing_token"], int)
    wrong_fence = copy.deepcopy(stream_open)
    wrong_fence["fencing_token"] = "42"
    with pytest.raises(ValidationError):
        validator.validate(wrong_fence)
    stream_error = copy.deepcopy(
        next(example for example in examples if example["type"] == "stream.error")
    )
    assert stream_error["error"]["code"] == "stale_epoch"
    invalid_stream_error = copy.deepcopy(stream_error)
    invalid_stream_error["error"]["code"] = "unstructured_failure"
    with pytest.raises(ValidationError):
        validator.validate(invalid_stream_error)
    error_without_operation = copy.deepcopy(stream_error)
    del error_without_operation["operation_id"]
    with pytest.raises(ValidationError):
        validator.validate(error_without_operation)
    forty_eight_k = copy.deepcopy(stream_open)
    forty_eight_k["input"]["sample_rate_hz"] = 48_000
    forty_eight_k["output"]["sample_rate_hz"] = 48_000
    validator.validate(forty_eight_k)

    stream_opened = next(example for example in examples if example["type"] == "stream.opened")
    missing_open_identity = copy.deepcopy(stream_opened)
    del missing_open_identity["submit_intent_id"]
    with pytest.raises(ValidationError):
        validator.validate(missing_open_identity)
    missing_open_limit = copy.deepcopy(stream_opened)
    del missing_open_limit["max_message_bytes"]
    with pytest.raises(ValidationError):
        validator.validate(missing_open_limit)

    stream_closed = copy.deepcopy(
        next(example for example in examples if example["type"] == "stream.closed")
    )
    stream_closed["status"] = "failed"
    stream_closed["error"] = None
    with pytest.raises(ValidationError):
        validator.validate(stream_closed)
    stream_closed["cleanup_state"] = "quarantined"
    stream_closed["error"] = {
        "code": "cleanup_unknown",
        "message": "engine termination could not be confirmed",
        "retryable": False,
    }
    validator.validate(stream_closed)
    succeeded_with_quarantine = copy.deepcopy(
        next(example for example in examples if example["type"] == "stream.closed")
    )
    succeeded_with_quarantine["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        validator.validate(succeeded_with_quarantine)
    missing_closed_identity = copy.deepcopy(succeeded_with_quarantine)
    missing_closed_identity["cleanup_state"] = "complete"
    del missing_closed_identity["submit_intent_id"]
    with pytest.raises(ValidationError):
        validator.validate(missing_closed_identity)
    missing_receipt = copy.deepcopy(
        next(example for example in examples if example["type"] == "stream.closed")
    )
    missing_receipt["receipt_ref"] = None
    with pytest.raises(ValidationError):
        validator.validate(missing_receipt)

    cancelled_stream = copy.deepcopy(
        next(example for example in examples if example["type"] == "stream.closed")
    )
    cancelled_stream["status"] = "cancelled"
    cancelled_stream["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        validator.validate(cancelled_stream)
    cancelled_stream["cleanup_state"] = "complete"
    validator.validate(cancelled_stream)

    succeeded_with_error = copy.deepcopy(
        next(example for example in examples if example["type"] == "stream.closed")
    )
    succeeded_with_error["error"] = {
        "code": "adapter_failure",
        "message": "synthetic failure",
        "retryable": False,
    }
    with pytest.raises(ValidationError):
        validator.validate(succeeded_with_error)


def test_nexus_sse_event_payloads_are_discriminated_and_strict() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    job_event_validator = _component_validator(speech, "JobEvent")
    job_examples = cast(
        list[dict[str, Any]],
        speech["components"]["schemas"]["JobEvent"]["examples"],
    )
    assert {example["type"] for example in job_examples} == {
        "job.queued",
        "job.started",
        "job.progress",
        "job.retry_scheduled",
        "job.cancelling",
        "job.succeeded",
        "job.failed",
        "job.cancelled",
        "job.expired",
        "job.cleanup_updated",
        "artifact.published",
    }
    for example in job_examples:
        job_event_validator.validate(example)

    invalid_sequence = copy.deepcopy(job_examples[0])
    invalid_sequence["id"] = "job-event-1"
    with pytest.raises(ValidationError):
        job_event_validator.validate(invalid_sequence)

    succeeded_job_event = copy.deepcopy(
        next(example for example in job_examples if example["type"] == "job.succeeded")
    )
    succeeded_without_result = copy.deepcopy(succeeded_job_event)
    del succeeded_without_result["data"]["result"]
    with pytest.raises(ValidationError):
        job_event_validator.validate(succeeded_without_result)
    succeeded_without_artifacts = copy.deepcopy(succeeded_job_event)
    succeeded_without_artifacts["data"]["artifacts"] = []
    with pytest.raises(ValidationError):
        job_event_validator.validate(succeeded_without_artifacts)
    _assert_job_success_evidence(cast(dict[str, Any], succeeded_job_event["data"]))

    result_with_unknown_artifact = copy.deepcopy(succeeded_job_event)
    result_with_unknown_artifact["data"]["result"]["audio_artifact_id"] = "artifact_missing"
    job_event_validator.validate(result_with_unknown_artifact)
    with pytest.raises(ValueError, match="not committed"):
        _assert_job_success_evidence(cast(dict[str, Any], result_with_unknown_artifact["data"]))

    result_with_wrong_artifact_kind = copy.deepcopy(succeeded_job_event)
    result_with_wrong_artifact_kind["data"]["artifacts"][0]["kind"] = "transcript"
    job_event_validator.validate(result_with_wrong_artifact_kind)
    with pytest.raises(ValueError, match="expected audio"):
        _assert_job_success_evidence(cast(dict[str, Any], result_with_wrong_artifact_kind["data"]))

    result_with_different_artifact_receipt = copy.deepcopy(succeeded_job_event)
    result_with_different_artifact_receipt["data"]["artifacts"][0]["receipt_id"] = "other_receipt"
    job_event_validator.validate(result_with_different_artifact_receipt)
    with pytest.raises(ValueError, match="result receipt"):
        _assert_job_success_evidence(
            cast(dict[str, Any], result_with_different_artifact_receipt["data"])
        )

    repeated_artifact = copy.deepcopy(succeeded_job_event)
    repeated_artifact["data"]["artifacts"].append(
        copy.deepcopy(repeated_artifact["data"]["artifacts"][0])
    )
    job_event_validator.validate(repeated_artifact)
    with pytest.raises(ValueError, match="repeats artifact_id"):
        _assert_job_success_evidence(cast(dict[str, Any], repeated_artifact["data"]))

    empty_artifact = copy.deepcopy(succeeded_job_event)
    empty_artifact["data"]["artifacts"][0]["bytes"] = 0
    with pytest.raises(ValidationError):
        job_event_validator.validate(empty_artifact)

    failed_job_event: dict[str, Any] = {
        "id": "18",
        "type": "job.failed",
        "job_id": "job_demo",
        "occurred_at": "2026-10-23T00:00:00Z",
        "data": {
            "status": "failed",
            "cleanup_state": "quarantined",
            "error": {
                "code": "engine_failure",
                "message": "synthetic engine failure",
                "retryable": True,
            },
        },
    }
    job_event_validator.validate(failed_job_event)

    unknown_job_data = copy.deepcopy(failed_job_event)
    unknown_job_data["data"]["internal_endpoint"] = "http://engine.invalid"
    with pytest.raises(ValidationError):
        job_event_validator.validate(unknown_job_data)

    mismatched_job_data = copy.deepcopy(failed_job_event)
    mismatched_job_data["data"] = {"status": "queued"}
    with pytest.raises(ValidationError):
        job_event_validator.validate(mismatched_job_data)

    false_cancelled_cleanup = copy.deepcopy(
        next(example for example in job_examples if example["type"] == "job.cancelled")
    )
    false_cancelled_cleanup["data"]["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        job_event_validator.validate(false_cancelled_cleanup)

    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    export_event_validator = _component_validator(workbench, "ExportEvent")
    export_examples = cast(
        list[dict[str, Any]],
        workbench["components"]["schemas"]["ExportEvent"]["examples"],
    )
    assert {example["type"] for example in export_examples} == {
        "export.queued",
        "export.progress",
        "export.succeeded",
        "export.failed",
        "export.cancelled",
        "export.artifact_published",
    }
    for example in export_examples:
        export_event_validator.validate(example)
        artifact = example.get("data", {}).get("artifact")
        if isinstance(artifact, dict):
            _assert_export_artifact_semantics(
                cast(str, example["export_id"]),
                "3",
                cast(dict[str, Any], artifact),
            )

    succeeded_export = copy.deepcopy(
        next(example for example in export_examples if example["type"] == "export.succeeded")
    )
    export_job_validator = _component_validator(workbench, "ExportJob")
    export_job: dict[str, Any] = {
        "id": "export_demo",
        "object": "nexus.export_job",
        "book_id": "book_demo",
        "status": "succeeded",
        "format": "zip",
        "manifest_revision": "3",
        "progress_completed": 5,
        "progress_total": 5,
        "artifact": copy.deepcopy(succeeded_export["data"]["artifact"]),
        "created_at": "2026-10-23T00:00:00Z",
        "updated_at": "2026-10-23T00:00:02Z",
    }
    export_job_validator.validate(export_job)
    _assert_export_job_semantics(export_job)

    incomplete_succeeded_export = copy.deepcopy(export_job)
    incomplete_succeeded_export["progress_completed"] = 4
    export_job_validator.validate(incomplete_succeeded_export)
    with pytest.raises(ValueError, match="completed progress"):
        _assert_export_job_semantics(incomplete_succeeded_export)

    export_job_with_wrong_manifest = copy.deepcopy(export_job)
    export_job_with_wrong_manifest["artifact"]["manifest_revision"] = "4"
    export_job_validator.validate(export_job_with_wrong_manifest)
    with pytest.raises(ValueError, match="manifest revision"):
        _assert_export_job_semantics(export_job_with_wrong_manifest)

    artifact_with_wrong_owner = copy.deepcopy(succeeded_export)
    artifact_with_wrong_owner["data"]["artifact"]["export_id"] = "export_other"
    export_event_validator.validate(artifact_with_wrong_owner)
    with pytest.raises(ValueError, match="different export job"):
        _assert_export_artifact_semantics(
            cast(str, artifact_with_wrong_owner["export_id"]),
            "3",
            cast(dict[str, Any], artifact_with_wrong_owner["data"]["artifact"]),
        )

    artifact_with_wrong_manifest = copy.deepcopy(succeeded_export)
    artifact_with_wrong_manifest["data"]["artifact"]["manifest_revision"] = "4"
    export_event_validator.validate(artifact_with_wrong_manifest)
    with pytest.raises(ValueError, match="manifest revision"):
        _assert_export_artifact_semantics(
            cast(str, artifact_with_wrong_manifest["export_id"]),
            "3",
            cast(dict[str, Any], artifact_with_wrong_manifest["data"]["artifact"]),
        )

    empty_export_artifact = copy.deepcopy(succeeded_export)
    empty_export_artifact["data"]["artifact"]["bytes"] = 0
    with pytest.raises(ValidationError):
        export_event_validator.validate(empty_export_artifact)

    invalid_export_sequence = copy.deepcopy(export_examples[0])
    invalid_export_sequence["id"] = "export-event-1"
    with pytest.raises(ValidationError):
        export_event_validator.validate(invalid_export_sequence)

    progress_event: dict[str, Any] = {
        "id": "7",
        "type": "export.progress",
        "export_id": "export_demo",
        "occurred_at": "2026-10-23T00:00:00Z",
        "data": {
            "status": "running",
            "progress_completed": 2,
            "progress_total": 5,
        },
    }
    export_event_validator.validate(progress_event)

    unknown_export_data = copy.deepcopy(progress_event)
    unknown_export_data["data"]["object_key"] = "private/path.wav"
    with pytest.raises(ValidationError):
        export_event_validator.validate(unknown_export_data)

    mismatched_export_data = copy.deepcopy(progress_event)
    mismatched_export_data["data"]["status"] = "succeeded"
    with pytest.raises(ValidationError):
        export_event_validator.validate(mismatched_export_data)


def test_committed_artifacts_require_non_empty_content() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    artifact_validator = _component_validator(speech, "Artifact")
    artifact: dict[str, Any] = {
        "id": "artifact_demo",
        "object": "nexus.artifact",
        "workspace_id": "ws_demo",
        "kind": "audio",
        "status": "ready",
        "media_type": "audio/wav",
        "bytes": 1,
        "sha256": "sha256:" + "a" * 64,
        "created_at": "2026-10-23T00:00:00Z",
    }
    artifact_validator.validate(artifact)
    empty_artifact = {**artifact, "bytes": 0}
    with pytest.raises(ValidationError):
        artifact_validator.validate(empty_artifact)

    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    saved_asset_validator = _component_validator(workbench, "SavedAsset")
    saved_asset: dict[str, Any] = {
        "id": "saved_asset_demo",
        "object": "nexus.saved_asset",
        "project_id": "project_demo",
        "media_type": "audio/wav",
        "bytes": 1,
        "sha256": "sha256:" + "a" * 64,
        "source_artifact_id": "artifact_demo",
        "source_receipt_id": "receipt_demo",
        "created_at": "2026-10-23T00:00:00Z",
    }
    saved_asset_validator.validate(saved_asset)
    without_source_receipt = copy.deepcopy(saved_asset)
    del without_source_receipt["source_receipt_id"]
    with pytest.raises(ValidationError):
        saved_asset_validator.validate(without_source_receipt)
    with pytest.raises(ValidationError):
        saved_asset_validator.validate({**saved_asset, "bytes": 0})
    with pytest.raises(ValidationError):
        saved_asset_validator.validate({**saved_asset, "media_type": ""})


def test_workbench_speech_sources_pin_receipts() -> None:
    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    source_validator = _component_validator(workbench, "TranscriptSource")
    source = {
        "kind": "speech_artifact",
        "artifact_id": "artifact_demo",
        "receipt_id": "receipt_demo",
    }
    source_validator.validate(source)
    without_receipt = copy.deepcopy(source)
    del without_receipt["receipt_id"]
    with pytest.raises(ValidationError):
        source_validator.validate(without_receipt)
    source_validator.validate({"kind": "manual", "text": "Synthetic transcript."})

    transcript = {
        "id": "transcript_demo",
        "object": "nexus.transcript",
        "project_id": "project_demo",
        "display_name": "Synthetic transcript",
        "head_revision": "1",
        "source_artifact_id": "artifact_demo",
        "source_receipt_id": "receipt_demo",
        "created_at": "2026-10-23T00:00:00Z",
        "updated_at": "2026-10-23T00:00:00Z",
    }
    transcript_validator = _component_validator(workbench, "Transcript")
    transcript_validator.validate(transcript)
    transcript_without_receipt = copy.deepcopy(transcript)
    del transcript_without_receipt["source_receipt_id"]
    with pytest.raises(ValidationError):
        transcript_validator.validate(transcript_without_receipt)

    revision = {
        "id": "transcript_revision_demo",
        "object": "nexus.transcript_revision",
        "transcript_id": "transcript_demo",
        "revision": "1",
        "status": "sealed",
        "text": "Synthetic transcript.",
        "source_revision": None,
        "source_artifact_id": "artifact_demo",
        "source_receipt_id": "receipt_demo",
        "created_at": "2026-10-23T00:00:00Z",
    }
    revision_validator = _component_validator(workbench, "TranscriptRevision")
    revision_validator.validate(revision)
    revision_without_receipt = copy.deepcopy(revision)
    del revision_without_receipt["source_receipt_id"]
    with pytest.raises(ValidationError):
        revision_validator.validate(revision_without_receipt)

    saved_asset_validator = _component_validator(workbench, "SavedAssetCreateRequest")
    saved_asset_validator.validate(
        {
            "source_artifact_id": "artifact_demo",
            "source_receipt_id": "receipt_demo",
        }
    )
    with pytest.raises(ValidationError):
        saved_asset_validator.validate({"source_artifact_id": "artifact_demo"})


def test_workbench_job_errors_and_terminal_states_are_typed() -> None:
    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    error = {
        "code": "llm_unavailable",
        "message": "configured internal LLM is unavailable",
        "retryable": True,
        "param": None,
    }
    samples: list[tuple[str, dict[str, Any]]] = [
        (
            "MinutesJob",
            {
                "id": "minutes_demo",
                "object": "nexus.minutes_job",
                "meeting_id": "meeting_demo",
                "status": "failed",
                "source_revision": "1",
                "template_revision": "1",
                "llm_model": "local-llm",
                "created_at": "2026-10-23T00:00:00Z",
                "updated_at": "2026-10-23T00:01:00Z",
            },
        ),
        (
            "AssistantTurn",
            {
                "id": "turn_demo",
                "object": "nexus.assistant_turn",
                "project_id": "project_demo",
                "status": "failed",
                "input_text": "synthetic input",
                "voice": {"id": "voice_demo", "revision": "1"},
                "playback_generation": 1,
                "created_at": "2026-10-23T00:00:00Z",
                "updated_at": "2026-10-23T00:01:00Z",
            },
        ),
        (
            "ExportJob",
            {
                "id": "export_demo",
                "object": "nexus.export_job",
                "book_id": "book_demo",
                "status": "failed",
                "format": "wav",
                "manifest_revision": "1",
                "progress_completed": 0,
                "progress_total": 1,
                "created_at": "2026-10-23T00:00:00Z",
                "updated_at": "2026-10-23T00:01:00Z",
            },
        ),
    ]
    for component, sample in samples:
        validator = _component_validator(workbench, component)
        with pytest.raises(ValidationError):
            validator.validate(sample)

        failed_with_error = copy.deepcopy(sample)
        failed_with_error["error"] = error
        validator.validate(failed_with_error)

        failed_with_unknown_error = copy.deepcopy(failed_with_error)
        failed_with_unknown_error["error"]["object_key"] = "private/path.wav"
        with pytest.raises(ValidationError):
            validator.validate(failed_with_unknown_error)

        succeeded_with_error = copy.deepcopy(failed_with_error)
        succeeded_with_error["status"] = "succeeded"
        with pytest.raises(ValidationError):
            validator.validate(succeeded_with_error)

    export_job = copy.deepcopy(next(sample for name, sample in samples if name == "ExportJob"))
    export_job["status"] = "succeeded"
    export_job["error"] = None
    with pytest.raises(ValidationError):
        _component_validator(workbench, "ExportJob").validate(export_job)
    export_job["artifact"] = {
        "id": "export_artifact_demo",
        "object": "nexus.export_artifact",
        "export_id": "export_demo",
        "manifest_revision": "1",
        "media_type": "application/zip",
        "bytes": 2048,
        "sha256": "sha256:" + "a" * 64,
        "created_at": "2026-10-23T00:00:02Z",
    }
    export_job_validator = _component_validator(workbench, "ExportJob")
    export_job_validator.validate(export_job)
    artifact_without_manifest = copy.deepcopy(export_job)
    del artifact_without_manifest["artifact"]["manifest_revision"]
    with pytest.raises(ValidationError):
        export_job_validator.validate(artifact_without_manifest)
    cancelled_export_with_artifact = copy.deepcopy(export_job)
    cancelled_export_with_artifact["status"] = "cancelled"
    with pytest.raises(ValidationError):
        export_job_validator.validate(cancelled_export_with_artifact)

    export_create_validator = _component_validator(workbench, "ExportCreateRequest")
    export_create = {"expected_casting_revision": "2", "format": "zip"}
    export_create_validator.validate(export_create)
    export_create_validator.validate({**export_create, "unit_ids": ["unit_demo"]})
    with pytest.raises(ValidationError):
        export_create_validator.validate({**export_create, "unit_ids": []})

    minutes_create_validator = _component_validator(workbench, "MinutesCreateRequest")
    minutes_create_validator.validate({"template_revision": "1", "llm_model": "local-llm"})
    with pytest.raises(ValidationError):
        minutes_create_validator.validate({"template_revision": "1", "llm_model": ""})

    voice = {"id": "voice_demo", "revision": "1"}
    turn_create_validator = _component_validator(workbench, "AssistantTurnCreateRequest")
    turn_create_validator.validate(
        {
            "input_text": "Synthetic input.",
            "voice": voice,
            "model": "local-llm",
            "profile": {"id": "profile_demo", "revision": "3"},
        }
    )
    with pytest.raises(ValidationError):
        turn_create_validator.validate(
            {"input_text": "Synthetic input.", "voice": voice, "model": ""}
        )

    render_create_validator = _component_validator(workbench, "RenderUnitCreateRequest")
    render_create_validator.validate(
        {
            "ordinal": 0,
            "text": "Synthetic narration unit.",
            "voice": voice,
            "model_binding": {"id": "binding_demo", "revision": "3"},
            "model": "tts-quality",
        }
    )
    with pytest.raises(ValidationError):
        render_create_validator.validate(
            {
                "ordinal": 0,
                "text": "Synthetic narration unit.",
                "voice": voice,
                "model_binding": {"id": "binding_demo", "revision": "3"},
                "model": "",
            }
        )

    minutes_job = copy.deepcopy(next(sample for name, sample in samples if name == "MinutesJob"))
    minutes_job["status"] = "succeeded"
    minutes_job["error"] = None
    minutes_validator = _component_validator(workbench, "MinutesJob")
    with pytest.raises(ValidationError):
        minutes_validator.validate(minutes_job)
    minutes_job["minutes_text"] = "Synthetic meeting minutes."
    minutes_validator.validate(minutes_job)
    minutes_job["minutes_text"] = ""
    with pytest.raises(ValidationError):
        minutes_validator.validate(minutes_job)
    minutes_job["minutes_text"] = None
    minutes_job["artifact_id"] = "artifact-minutes-demo"
    minutes_validator.validate(minutes_job)
    failed_minutes_with_artifact = copy.deepcopy(minutes_job)
    failed_minutes_with_artifact["status"] = "failed"
    failed_minutes_with_artifact["error"] = {
        "code": "llm_unavailable",
        "message": "configured internal LLM is unavailable",
        "retryable": True,
        "param": None,
    }
    with pytest.raises(ValidationError):
        minutes_validator.validate(failed_minutes_with_artifact)
    failed_minutes_with_partial_text = copy.deepcopy(failed_minutes_with_artifact)
    failed_minutes_with_partial_text["artifact_id"] = None
    failed_minutes_with_partial_text["minutes_text"] = "Synthetic partial minutes."
    minutes_validator.validate(failed_minutes_with_partial_text)

    assistant_turn = copy.deepcopy(
        next(sample for name, sample in samples if name == "AssistantTurn")
    )
    assistant_turn["status"] = "succeeded"
    assistant_turn["error"] = None
    turn_validator = _component_validator(workbench, "AssistantTurn")
    with pytest.raises(ValidationError):
        turn_validator.validate(assistant_turn)
    assistant_turn["output_text"] = "Synthetic assistant response."
    with pytest.raises(ValidationError):
        turn_validator.validate(assistant_turn)
    assistant_turn["speech_job_id"] = "speech_job_demo"
    turn_validator.validate(assistant_turn)
    empty_input = copy.deepcopy(assistant_turn)
    empty_input["input_text"] = ""
    with pytest.raises(ValidationError):
        turn_validator.validate(empty_input)
    detached_job = copy.deepcopy(assistant_turn)
    detached_job["output_text"] = None
    with pytest.raises(ValidationError):
        turn_validator.validate(detached_job)
    assistant_turn["output_text"] = ""
    with pytest.raises(ValidationError):
        turn_validator.validate(assistant_turn)

    render_unit = {
        "id": "unit_demo",
        "object": "nexus.render_unit",
        "chapter_id": "chapter_demo",
        "ordinal": 0,
        "revision": "2",
        "text": "Synthetic narration unit.",
        "voice": {"id": "voice_demo", "revision": "1"},
        "model_binding": {"id": "binding_demo", "revision": "3"},
        "model": "tts-quality",
        "status": "generating",
        "speech_job_id": None,
        "source_artifact_id": None,
        "updated_at": "2026-10-23T00:00:00Z",
    }
    render_validator = _component_validator(workbench, "RenderUnit")
    with pytest.raises(ValidationError):
        render_validator.validate(render_unit)
    render_unit["speech_job_id"] = "speech_job_demo"
    render_validator.validate(render_unit)
    render_unit["status"] = "ready"
    with pytest.raises(ValidationError):
        render_validator.validate(render_unit)
    render_unit["source_artifact_id"] = "speech_artifact_demo"
    with pytest.raises(ValidationError):
        render_validator.validate(render_unit)
    render_unit["source_receipt_id"] = "speech_receipt_demo"
    render_validator.validate(render_unit)
    render_unit["model"] = ""
    with pytest.raises(ValidationError):
        render_validator.validate(render_unit)


def test_workbench_meeting_terminal_states_are_consistent() -> None:
    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    validator = _component_validator(workbench, "Meeting")
    meeting: dict[str, Any] = {
        "id": "meeting_demo",
        "object": "nexus.meeting",
        "project_id": "project_demo",
        "display_name": "Weekly sync",
        "status": "capturing",
        "transcript_id": "transcript_demo",
        "sealed_revision": None,
        "minutes_id": None,
        "save_audio": False,
        "started_at": "2026-10-23T00:00:00Z",
        "ended_at": None,
        "error": None,
    }
    validator.validate(meeting)
    capturing_with_seal = copy.deepcopy(meeting)
    capturing_with_seal["sealed_revision"] = "1"
    with pytest.raises(ValidationError):
        validator.validate(capturing_with_seal)

    sealed: dict[str, Any] = {
        **meeting,
        "status": "sealed",
        "sealed_revision": "1",
        "ended_at": "2026-10-23T00:30:00Z",
    }
    validator.validate(sealed)
    sealed_with_minutes = copy.deepcopy(sealed)
    sealed_with_minutes["minutes_id"] = "minutes_demo"
    with pytest.raises(ValidationError):
        validator.validate(sealed_with_minutes)

    for status in ("minutes_pending", "minutes_ready"):
        with_minutes: dict[str, Any] = {
            **sealed,
            "status": status,
            "minutes_id": "minutes_demo",
        }
        validator.validate(with_minutes)
        missing_minutes = copy.deepcopy(with_minutes)
        missing_minutes["minutes_id"] = None
        with pytest.raises(ValidationError):
            validator.validate(missing_minutes)

    failed: dict[str, Any] = {
        **meeting,
        "status": "failed",
        "ended_at": "2026-10-23T00:30:00Z",
        "error": {
            "code": "capture_failed",
            "message": "Synthetic capture failure.",
            "retryable": True,
            "param": None,
        },
    }
    validator.validate(failed)
    failed_without_error = copy.deepcopy(failed)
    del failed_without_error["error"]
    with pytest.raises(ValidationError):
        validator.validate(failed_without_error)
    failed_with_minutes = copy.deepcopy(failed)
    failed_with_minutes["minutes_id"] = "minutes_demo"
    with pytest.raises(ValidationError):
        validator.validate(failed_with_minutes)
    capturing_with_error = copy.deepcopy(meeting)
    capturing_with_error["error"] = failed["error"]
    with pytest.raises(ValidationError):
        validator.validate(capturing_with_error)


def test_transcription_logprobs_match_the_openai_sdk_shape() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    validator = _component_validator(speech, "TranscriptionLogprob")
    validator.validate({"token": "hello", "bytes": [104, 101], "logprob": -0.25})

    unknown = {"token": "hello", "bytes": [104, 101], "logprob": -0.25, "index": 1}
    with pytest.raises(ValidationError):
        validator.validate(unknown)


def test_internal_submit_contract_derives_paths_and_output_sample_metrics() -> None:
    execution = _resolved_yaml(_EXECUTION_CONTROL_OPENAPI)
    submit_validator = _component_validator(execution, "SubmitExecutionRequest")
    request = {
        "submit_intent_id": "intent-demo",
        "lease_id": "lease-demo",
        "operation_id": "op-demo",
        "attempt_id": "att-demo",
        "stage_id": "stage-demo",
        "workspace_id": "ws-demo",
        "pool_id": "tts-batch",
        "host_node_id": "node-demo",
        "gateway_id": "gateway-demo",
        "instance_id": "instance-demo",
        "instance_generation": 1,
        "binding_revision_id": "tts-binding-rev-1",
        "policy_revision_id": "policy-1",
        "fencing_token": 1,
        "command_digest": "sha256:" + "2" * 64,
        "deadline_at": "2026-10-22T01:00:00Z",
        "max_output_audio_ms": 30_000,
        "sample_rate_hz": 24_000,
    }
    request["command"] = _load_json(_CONTRACTS / "internal/execution-command.schema.json")[
        "examples"
    ][0]
    submit_validator.validate(request)
    inputless = copy.deepcopy(request)
    del inputless["command"]
    with pytest.raises(ValidationError):
        submit_validator.validate(inputless)
    text_only_request = copy.deepcopy(request)
    text_only_request["max_output_audio_ms"] = None
    text_only_request["sample_rate_hz"] = None
    submit_validator.validate(text_only_request)
    audio_without_sample_rate = copy.deepcopy(request)
    audio_without_sample_rate["sample_rate_hz"] = None
    with pytest.raises(ValidationError):
        submit_validator.validate(audio_without_sample_rate)
    text_with_sample_rate = copy.deepcopy(text_only_request)
    text_with_sample_rate["sample_rate_hz"] = 24_000
    with pytest.raises(ValidationError):
        submit_validator.validate(text_with_sample_rate)
    unsupported_sample_rate = copy.deepcopy(request)
    unsupported_sample_rate["sample_rate_hz"] = 8_000
    with pytest.raises(ValidationError):
        submit_validator.validate(unsupported_sample_rate)

    caller_path = copy.deepcopy(request)
    caller_path["staging_output_prefix"] = "staging/caller-controlled/"
    with pytest.raises(ValidationError):
        submit_validator.validate(caller_path)

    watermark_validator = _component_validator(execution, "OutputWatermark")
    watermark = {
        "output_stream_id": "tts-main",
        "generated_pcm_samples": 24_000,
        "sample_rate_hz": 24_000,
        "hard_output_ceiling_samples": 48_000,
    }
    watermark_validator.validate(watermark)
    _assert_output_watermark_semantics(watermark)
    over_ceiling = copy.deepcopy(watermark)
    over_ceiling["generated_pcm_samples"] = 48_001
    watermark_validator.validate(over_ceiling)
    with pytest.raises(ValueError, match="hard output ceiling"):
        _assert_output_watermark_semantics(over_ceiling)
    watermark_with_derived_ms = copy.deepcopy(watermark)
    watermark_with_derived_ms["generated_audio_ms"] = 1_000
    with pytest.raises(ValidationError):
        watermark_validator.validate(watermark_with_derived_ms)


def test_internal_terminal_evidence_and_drain_state_are_consistent() -> None:
    execution = _resolved_yaml(_EXECUTION_CONTROL_OPENAPI)
    status_validator = _component_validator(execution, "ExecutionStatusResponse")
    status: dict[str, Any] = {
        "submit_intent_id": "intent-demo",
        "lease_id": "lease-demo",
        "operation_id": "op-demo",
        "attempt_id": "att-demo",
        "stage_id": "stage-demo",
        "workspace_id": "ws-demo",
        "pool_id": "tts-batch",
        "host_node_id": "node-demo",
        "gateway_id": "gateway-demo",
        "instance_id": "instance-demo",
        "instance_generation": 1,
        "binding_revision_id": "tts-binding-rev-1",
        "policy_revision_id": "policy-1",
        "voice_revision_id": None,
        "fencing_token": 1,
        "command_digest": "sha256:" + "2" * 64,
        "deadline_at": "2026-10-22T01:00:00Z",
        "dispatched_at": "2026-10-22T00:00:01Z",
        "sample_rate_hz": 24_000,
        "engine_request_id": None,
        "execution_state": "aborted",
        "cleanup_state": "complete",
        "stop_evidence_kind": "engine_abort_ack",
        "output_watermark": None,
        "staging_artifact_ref": None,
        "error_code": None,
        "updated_at": "2026-10-22T00:00:00Z",
    }
    status_validator.validate(status)
    _assert_execution_status_semantics(status)

    output_watermark = {
        "output_stream_id": "tts-main",
        "generated_pcm_samples": 24_000,
        "sample_rate_hz": 24_000,
        "hard_output_ceiling_samples": 48_000,
    }
    status_with_watermark = copy.deepcopy(status)
    status_with_watermark["sample_rate_hz"] = 24_000
    status_with_watermark["output_watermark"] = output_watermark
    status_validator.validate(status_with_watermark)
    _assert_execution_status_semantics(status_with_watermark)
    mismatched_watermark_rate = copy.deepcopy(status_with_watermark)
    mismatched_watermark_rate["output_watermark"]["sample_rate_hz"] = 48_000
    status_validator.validate(mismatched_watermark_rate)
    with pytest.raises(ValueError, match="sample rates"):
        _assert_execution_status_semantics(mismatched_watermark_rate)

    unconfirmed = copy.deepcopy(status)
    unconfirmed["execution_state"] = "aborted"
    unconfirmed["cleanup_state"] = "quarantined"
    unconfirmed["stop_evidence_kind"] = "unconfirmed_timeout"
    with pytest.raises(ValidationError):
        status_validator.validate(unconfirmed)

    succeeded = copy.deepcopy(status)
    succeeded["execution_state"] = "succeeded"
    succeeded["cleanup_state"] = "pending"
    succeeded["stop_evidence_kind"] = "none"
    with pytest.raises(ValidationError):
        status_validator.validate(succeeded)
    succeeded["cleanup_state"] = "complete"
    succeeded["stop_evidence_kind"] = "engine_terminal_response"
    status_validator.validate(succeeded)

    failed = copy.deepcopy(status)
    failed["execution_state"] = "failed"
    failed["cleanup_state"] = "complete"
    failed["stop_evidence_kind"] = "none"
    failed["error_code"] = "policy_or_resource_disabled"
    with pytest.raises(ValidationError):
        status_validator.validate(failed)
    failed["stop_evidence_kind"] = "engine_terminal_response"
    status_validator.validate(failed)
    failed["cleanup_state"] = "not_required"
    failed["stop_evidence_kind"] = "none"
    with pytest.raises(ValidationError):
        status_validator.validate(failed)
    failed["dispatched_at"] = None
    status_validator.validate(failed)
    failed["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        status_validator.validate(failed)

    failed_without_error = copy.deepcopy(status)
    failed_without_error["execution_state"] = "failed"
    failed_without_error["cleanup_state"] = "not_required"
    failed_without_error["stop_evidence_kind"] = "none"
    failed_without_error["error_code"] = None
    with pytest.raises(ValidationError):
        status_validator.validate(failed_without_error)
    del failed_without_error["error_code"]
    with pytest.raises(ValidationError):
        status_validator.validate(failed_without_error)

    failed_after_dispatch = copy.deepcopy(status)
    failed_after_dispatch["execution_state"] = "failed"
    failed_after_dispatch["cleanup_state"] = "not_required"
    failed_after_dispatch["stop_evidence_kind"] = "none"
    failed_after_dispatch["error_code"] = "invalid_execution_request"
    failed_after_dispatch["dispatched_at"] = None
    status_validator.validate(failed_after_dispatch)
    failed_after_dispatch["engine_request_id"] = "engine-request-1"
    with pytest.raises(ValidationError):
        status_validator.validate(failed_after_dispatch)
    del failed_after_dispatch["engine_request_id"]
    with pytest.raises(ValidationError):
        status_validator.validate(failed_after_dispatch)
    failed_after_dispatch["engine_request_id"] = None
    failed_after_dispatch["dispatched_at"] = "2026-10-22T00:00:01Z"
    with pytest.raises(ValidationError):
        status_validator.validate(failed_after_dispatch)
    del failed_after_dispatch["dispatched_at"]
    with pytest.raises(ValidationError):
        status_validator.validate(failed_after_dispatch)

    quarantined = copy.deepcopy(status)
    quarantined["execution_state"] = "unknown_quarantined"
    quarantined["cleanup_state"] = "quarantined"
    quarantined["stop_evidence_kind"] = "unconfirmed_timeout"
    status_validator.validate(quarantined)

    drain_validator = _component_validator(execution, "DrainInstanceResponse")
    drained = {
        "instance_id": "instance-demo",
        "instance_generation": 1,
        "drain_state": "drained",
        "active_intent_count": 0,
        "submitting_intent_count": 0,
        "quarantined_intent_count": 0,
        "safe_to_stop_instance": True,
        "updated_at": "2026-10-22T00:00:00Z",
    }
    drain_validator.validate(drained)
    still_active = copy.deepcopy(drained)
    still_active["active_intent_count"] = 1
    with pytest.raises(ValidationError):
        drain_validator.validate(still_active)
    still_active["drain_state"] = "draining"
    still_active["safe_to_stop_instance"] = False
    drain_validator.validate(still_active)

    runtime = _resolved_yaml(_RUNTIME_CONTROL_OPENAPI)
    intent_validator = _component_validator(runtime, "RuntimeInstanceIntentResponse")
    stopped_intent: dict[str, Any] = {
        "intent_id": "intent-stop",
        "target_node_id": "node-demo",
        "pool_id": "asr-realtime",
        "gateway_id": "gateway-demo",
        "approved_template_id": "template-asr-realtime",
        "binding_revision_id": "mbrev-asr-realtime-4",
        "manifest_id": "capm-asr-realtime-full-segment-v1",
        "config_digest": "sha256:" + "3" * 64,
        "instance_id": "instance-demo",
        "instance_generation": 1,
        "action": "stop",
        "fencing_token": 1,
        "intent_state": "succeeded",
        "observed_instance_state": "stopped",
        "reserved_memory": {
            "architecture": "discrete_device_memory",
            "peak": {
                "unified_memory_bytes": 0,
                "device_memory_bytes": 1024,
                "host_memory_bytes": 512,
            },
            "steady": {
                "unified_memory_bytes": 0,
                "device_memory_bytes": 768,
                "host_memory_bytes": 384,
            },
        },
        "stop_evidence": {
            "process_or_container_exited": True,
            "resources_released_confirmed": True,
            "verified_at": "2026-10-22T00:00:00Z",
        },
        "idempotent_replay": True,
        "created_at": "2026-10-22T00:00:00Z",
        "updated_at": "2026-10-22T00:00:00Z",
    }
    intent_validator.validate(stopped_intent)
    succeeded_with_error = copy.deepcopy(stopped_intent)
    succeeded_with_error["error_code"] = "runtime_start_failed"
    with pytest.raises(ValidationError):
        intent_validator.validate(succeeded_with_error)
    missing_intent_identity = copy.deepcopy(stopped_intent)
    del missing_intent_identity["binding_revision_id"]
    with pytest.raises(ValidationError):
        intent_validator.validate(missing_intent_identity)
    missing_evidence = copy.deepcopy(stopped_intent)
    missing_evidence["stop_evidence"] = None
    with pytest.raises(ValidationError):
        intent_validator.validate(missing_evidence)
    false_evidence = copy.deepcopy(stopped_intent)
    false_evidence["stop_evidence"]["resources_released_confirmed"] = False
    with pytest.raises(ValidationError):
        intent_validator.validate(false_evidence)
    premature_evidence = copy.deepcopy(stopped_intent)
    premature_evidence["intent_state"] = "pending"
    with pytest.raises(ValidationError):
        intent_validator.validate(premature_evidence)

    failed_intent = copy.deepcopy(stopped_intent)
    failed_intent["action"] = "prepare"
    failed_intent["intent_state"] = "failed"
    failed_intent["observed_instance_state"] = "failed"
    failed_intent["stop_evidence"] = None
    with pytest.raises(ValidationError):
        intent_validator.validate(failed_intent)
    failed_intent["error_code"] = "runtime_start_failed"
    intent_validator.validate(failed_intent)
    failed_intent["error_code"] = None
    with pytest.raises(ValidationError):
        intent_validator.validate(failed_intent)

    runtime_error_validator = _component_validator(runtime, "RuntimeControlErrorResponse")
    runtime_error_validator.validate(
        {
            "error": {
                "code": "intent_conflict",
                "message": "intent_id already records a different request",
                "retryable": False,
                "observed_fencing_token": 7,
            }
        }
    )
    unknown_runtime_error = {
        "error": {
            "code": "conflicting_intent",
            "message": "unstable alias",
            "retryable": False,
        }
    }
    with pytest.raises(ValidationError):
        runtime_error_validator.validate(unknown_runtime_error)

    reconcile_validator = _component_validator(runtime, "NodeReconcileRequest")
    reconcile = {
        "controller_id": "controller-demo",
        "fencing_token": 3,
        "reported_at": "2026-10-22T00:00:00Z",
        "instances": [
            {
                "instance_id": "instance-demo",
                "instance_generation": 1,
                "approved_template_id": "template-asr-realtime",
                "config_digest": "sha256:" + "4" * 64,
                "observed_instance_state": "ready",
            },
            {
                "instance_id": "instance-demo",
                "instance_generation": 1,
                "approved_template_id": "template-asr-realtime",
                "config_digest": "sha256:" + "4" * 64,
                "observed_instance_state": "stopped",
                "stop_evidence": {
                    "process_or_container_exited": True,
                    "resources_released_confirmed": True,
                    "verified_at": "2026-10-22T00:00:00Z",
                },
            },
        ],
    }
    reconcile_validator.validate(reconcile)
    with pytest.raises(ValueError, match="repeats instance generation"):
        _assert_node_reconcile_semantics(reconcile)

    barrier_validator = _component_validator(execution, "GatewayFencingBarrierResponse")
    barrier: dict[str, Any] = {
        "gateway_id": "gateway-demo",
        "pool_id": "tts-batch",
        "enforced_min_fencing_token": 3,
        "barrier_sealed": True,
        "submitting_intent_ids": ["intent-demo"],
        "executing_intent_ids": [],
        "quarantined_intent_ids": [],
        "enforced_at": "2026-10-22T00:00:00Z",
    }
    barrier_validator.validate(barrier)
    duplicate_intent = copy.deepcopy(barrier)
    duplicate_intent["submitting_intent_ids"].append("intent-demo")
    with pytest.raises(ValidationError):
        barrier_validator.validate(duplicate_intent)


def test_internal_submit_response_echoes_the_persisted_intent_identity() -> None:
    execution = _resolved_yaml(_EXECUTION_CONTROL_OPENAPI)
    validator = _component_validator(execution, "SubmitExecutionResponse")
    response = {
        "submit_intent_id": "intent-demo",
        "lease_id": "lease-demo",
        "operation_id": "op-demo",
        "attempt_id": "att-demo",
        "stage_id": "stage-demo",
        "workspace_id": "ws-demo",
        "pool_id": "tts-batch",
        "host_node_id": "node-demo",
        "gateway_id": "gateway-demo",
        "instance_id": "instance-demo",
        "instance_generation": 1,
        "binding_revision_id": "tts-binding-rev-1",
        "policy_revision_id": "policy-1",
        "voice_revision_id": None,
        "fencing_token": 7,
        "command_digest": "sha256:" + "2" * 64,
        "deadline_at": "2026-10-22T01:00:00Z",
        "dispatched_at": "2026-10-22T00:00:01Z",
        "sample_rate_hz": 24_000,
        "execution_state": "executing",
        "cleanup_state": "pending",
        "idempotent_replay": True,
        "recorded_at": "2026-10-22T00:00:00Z",
    }
    validator.validate(response)

    for field in (
        "workspace_id",
        "binding_revision_id",
        "policy_revision_id",
        "command_digest",
        "deadline_at",
        "sample_rate_hz",
    ):
        missing_identity = copy.deepcopy(response)
        del missing_identity[field]
        with pytest.raises(ValidationError):
            validator.validate(missing_identity)

    active_with_released_cleanup = copy.deepcopy(response)
    active_with_released_cleanup["cleanup_state"] = "complete"
    with pytest.raises(ValidationError):
        validator.validate(active_with_released_cleanup)

    executing_without_dispatch = copy.deepcopy(response)
    executing_without_dispatch["dispatched_at"] = None
    with pytest.raises(ValidationError):
        validator.validate(executing_without_dispatch)

    failed_before_dispatch = copy.deepcopy(response)
    failed_before_dispatch["execution_state"] = "failed"
    failed_before_dispatch["cleanup_state"] = "not_required"
    failed_before_dispatch["dispatched_at"] = None
    failed_before_dispatch["engine_request_id"] = None
    validator.validate(failed_before_dispatch)
    del failed_before_dispatch["engine_request_id"]
    with pytest.raises(ValidationError):
        validator.validate(failed_before_dispatch)
    failed_before_dispatch["engine_request_id"] = "engine-request-1"
    with pytest.raises(ValidationError):
        validator.validate(failed_before_dispatch)


def test_runtime_memory_reservation_uses_architecture_specific_counters() -> None:
    runtime = _resolved_yaml(_RUNTIME_CONTROL_OPENAPI)
    validator = _component_validator(runtime, "RuntimeMemoryReservation")
    discrete: dict[str, Any] = {
        "architecture": "discrete_device_memory",
        "peak": {
            "unified_memory_bytes": 0,
            "device_memory_bytes": 1024,
            "host_memory_bytes": 512,
        },
        "steady": {
            "unified_memory_bytes": 0,
            "device_memory_bytes": 768,
            "host_memory_bytes": 384,
        },
    }
    validator.validate(discrete)
    _assert_runtime_memory_reservation_semantics(discrete)
    smaller_peak = copy.deepcopy(discrete)
    smaller_peak["peak"]["device_memory_bytes"] = 512
    validator.validate(smaller_peak)
    with pytest.raises(ValueError, match="peak is below steady"):
        _assert_runtime_memory_reservation_semantics(smaller_peak)

    unified = {
        "architecture": "unified_memory",
        "peak": {
            "unified_memory_bytes": 1024,
            "device_memory_bytes": 0,
            "host_memory_bytes": 0,
        },
        "steady": {
            "unified_memory_bytes": 768,
            "device_memory_bytes": 0,
            "host_memory_bytes": 0,
        },
    }
    validator.validate(unified)
    _assert_runtime_memory_reservation_semantics(unified)
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "architecture": "unified_memory",
                "peak": {
                    "unified_memory_bytes": 1024,
                    "device_memory_bytes": 1,
                    "host_memory_bytes": 0,
                },
                "steady": {
                    "unified_memory_bytes": 768,
                    "device_memory_bytes": 0,
                    "host_memory_bytes": 0,
                },
            }
        )


def test_openapi_request_schemas_reject_unknown_fields() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    speech_validator = _component_validator(speech, "SpeechRequest")
    with pytest.raises(ValidationError):
        speech_validator.validate(
            {
                "model": "tts-interactive",
                "input": "hello",
                "voice": "narrator",
                "response_format": "wav",
                "unknown": True,
            }
        )
    with pytest.raises(ValidationError):
        speech_validator.validate(
            {
                "model": "tts-interactive",
                "input": "hello",
                "voice": "narrator",
                "nexus": {"profile_id": "narrator-dialogue"},
            }
        )
    speech_validator.validate(
        {
            "model": "tts-interactive",
            "input": "hello",
            "voice": "narrator",
            "nexus": {
                "workspace_id": "ws-default",
                "overrides": {"tts": {"audio_delivery": "complete"}},
            },
        }
    )
    with pytest.raises(ValidationError):
        speech_validator.validate(
            {
                "model": "tts-interactive",
                "input": "hello",
                "voice": "narrator",
                "nexus": {
                    "workspace_id": "ws-default",
                    "overrides": {"tts": {"audio_delivery": "chunked"}},
                },
            }
        )
    for duplicate_rest_field in (
        {"speed": 1.0},
        {"instructions": "calm"},
        {"pronunciation_revision": "1"},
    ):
        with pytest.raises(ValidationError):
            speech_validator.validate(
                {
                    "model": "tts-interactive",
                    "input": "hello",
                    "voice": "narrator",
                    "nexus": {
                        "workspace_id": "ws-default",
                        "overrides": {"tts": duplicate_rest_field},
                    },
                }
            )

    transcription_nexus_validator = _component_validator(speech, "TranscriptionNexusOptions")
    transcription_nexus_validator.validate(
        {
            "workspace_id": "ws_default",
            "profile_id": "caption-file",
            "profile_revision": "3",
            "overrides": {"asr": {"language": "zh", "finalization": "full_segment"}},
        }
    )
    with pytest.raises(ValidationError):
        transcription_nexus_validator.validate(
            {
                "workspace_id": "ws_default",
                "voice_revision": "1",
            }
        )
    with pytest.raises(ValidationError):
        transcription_nexus_validator.validate(
            {
                "workspace_id": "ws_default",
                "profile_id": "caption-file",
            }
        )
    with pytest.raises(ValidationError):
        transcription_nexus_validator.validate(
            {
                "workspace_id": "ws_default",
                "overrides": {"tts": {"speed": 1.0}},
            }
        )

    transcription_request_validator = _component_validator(speech, "TranscriptionRequest")
    transcription_request_validator.validate(
        {
            "file": "synthetic.wav",
            "model": "asr-quality",
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["segment", "word"],
        }
    )
    with pytest.raises(ValidationError):
        transcription_request_validator.validate(
            {
                "file": "synthetic.wav",
                "model": "asr-quality",
                "response_format": "json",
                "timestamp_granularities[]": ["word"],
            }
        )

    job_validator = _component_validator(speech, "JobCreateRequest")
    job_validator.validate(
        {
            "kind": "asr",
            "model": "asr-quality",
            "input": {"audio_asset_id": "ast_input"},
            "output": {"transcript_format": "json", "save_artifacts": True},
        }
    )
    with pytest.raises(ValidationError):
        job_validator.validate(
            {
                "kind": "asr",
                "model": "asr-quality",
                "input": {"audio_asset_id": "ast_input"},
                "unknown": True,
            }
        )

    pronunciation_validator = _component_validator(speech, "PronunciationCreateRequest")
    pronunciation_validator.validate(
        {
            "id": "pron-demo",
            "expected_revision": "1",
            "encoding": "pinyin",
            "entries": [{"grapheme": "SpeechNexus", "phonemes": "si pi chi ne ke si"}],
        }
    )
    for incomplete in (
        {
            "id": "pron-demo",
            "encoding": "pinyin",
            "entries": [{"grapheme": "SpeechNexus", "phonemes": "si pi chi ne ke si"}],
        },
        {
            "expected_revision": "1",
            "encoding": "pinyin",
            "entries": [{"grapheme": "SpeechNexus", "phonemes": "si pi chi ne ke si"}],
        },
    ):
        with pytest.raises(ValidationError):
            pronunciation_validator.validate(incomplete)

    policy_patch_validator = _component_validator(_resolved_yaml(_ADMIN_OPENAPI), "PolicyPatch")
    policy_patch_validator.validate(
        {"expected_revision": "2", "features": {"workbench_enabled": False}}
    )
    with pytest.raises(ValidationError):
        policy_patch_validator.validate(
            {"expected_revision": "2", "quotas": {"max_concurrent_job": 2}}
        )
    with pytest.raises(ValidationError):
        policy_patch_validator.validate({"expected_revision": "2", "quotas": {}})
    with pytest.raises(ValidationError):
        policy_patch_validator.validate({"expected_revision": "2"})


def test_operation_result_status_is_separate_from_cleanup_state() -> None:
    shared = _load_json(_OPERATION_STATES_SCHEMA)
    shared_defs = cast(dict[str, dict[str, Any]], shared["$defs"])
    assert shared_defs["OperationStatus"]["enum"] == [
        "accepted",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert shared_defs["TerminalOperationStatus"]["enum"] == [
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert shared_defs["CleanupState"]["enum"] == [
        "not_required",
        "pending",
        "complete",
        "quarantined",
    ]

    speech = _resolved_yaml(_SPEECH_OPENAPI)
    terminal_validator = _component_validator(speech, "RealtimeTerminalOperation")
    terminal_validator.validate(
        {
            "operation_id": "op-demo",
            "kind": "asr",
            "status": "failed",
            "cleanup_state": "quarantined",
        }
    )
    with pytest.raises(ValidationError):
        terminal_validator.validate(
            {
                "operation_id": "op-demo",
                "kind": "asr",
                "status": "cancelled",
                "cleanup_state": "quarantined",
            }
        )
    with pytest.raises(ValidationError):
        terminal_validator.validate(
            {
                "operation_id": "op-demo",
                "kind": "asr",
                "status": "quarantined",
                "cleanup_state": "quarantined",
            }
        )

    job: dict[str, Any] = {
        "id": "job-demo",
        "object": "nexus.job",
        "kind": "asr",
        "status": "succeeded",
        "cleanup_state": "not_required",
        "request_id": "req-demo",
        "workspace_id": "ws-default",
        "execution_revision": "1",
        "progress": {"unit": "audio_ms", "completed": 1, "total": 1},
        "artifacts": [
            {
                "artifact_id": "artifact-demo",
                "kind": "transcript",
                "media_type": "application/json",
                "bytes": 128,
                "sha256": "sha256:" + "a" * 64,
                "receipt_id": "receipt-demo",
            }
        ],
        "result": {
            "kind": "asr",
            "transcript_artifact_id": "artifact-demo",
            "receipt_id": "receipt-demo",
        },
        "operations": [],
        "effective_configuration": {
            "models": {"asr": {"id": "asr-quality", "revision": "1"}},
            "policy_revision": "1",
            "catalog_revision": "1",
        },
        "error": None,
        "created_at": "2026-10-22T00:00:00Z",
        "updated_at": "2026-10-22T00:00:01Z",
    }
    job_validator = _component_validator(speech, "Job")
    job_validator.validate(job)
    succeeded_with_unverified_cleanup = copy.deepcopy(job)
    succeeded_with_unverified_cleanup["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        job_validator.validate(succeeded_with_unverified_cleanup)

    queued_job = copy.deepcopy(job)
    queued_job["status"] = "queued"
    queued_job["result"] = None
    queued_job["artifacts"] = []
    job_validator.validate(queued_job)

    succeeded_without_result = copy.deepcopy(job)
    succeeded_without_result["result"] = None
    with pytest.raises(ValidationError):
        job_validator.validate(succeeded_without_result)

    succeeded_without_artifact = copy.deepcopy(job)
    succeeded_without_artifact["artifacts"] = []
    with pytest.raises(ValidationError):
        job_validator.validate(succeeded_without_artifact)

    mismatched_result_kind = copy.deepcopy(job)
    mismatched_result_kind["kind"] = "tts"
    with pytest.raises(ValidationError):
        job_validator.validate(mismatched_result_kind)

    failed_job = copy.deepcopy(job)
    failed_job["status"] = "failed"
    failed_job["result"] = None
    failed_job["error"] = None
    with pytest.raises(ValidationError):
        job_validator.validate(failed_job)
    failed_job["error"] = {
        "code": "engine_failure",
        "message": "synthetic failure",
        "retryable": False,
    }
    job_validator.validate(failed_job)
    failed_job["result"] = job["result"]
    with pytest.raises(ValidationError):
        job_validator.validate(failed_job)
    cancelled_job = copy.deepcopy(job)
    cancelled_job["status"] = "cancelled"
    cancelled_job["result"] = None
    cancelled_job["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        job_validator.validate(cancelled_job)
    cancelled_job["cleanup_state"] = "complete"
    job_validator.validate(cancelled_job)
    expired_job = copy.deepcopy(job)
    expired_job["status"] = "expired"
    expired_job["result"] = None
    expired_job["cleanup_state"] = "pending"
    with pytest.raises(ValidationError):
        job_validator.validate(expired_job)
    expired_job["cleanup_state"] = "not_required"
    job_validator.validate(expired_job)

    result_validator = _component_validator(speech, "JobResult")
    result_validator.validate(
        {
            "kind": "voice_design",
            "voice_draft_id": "draft-demo",
            "draft_revision": "2",
            "preview_artifact_ids": ["preview-demo"],
            "receipt_id": "receipt-demo",
        }
    )
    result_validator.validate(
        {
            "kind": "voice_preview",
            "voice_draft_id": "draft-demo",
            "draft_revision": "2",
            "preview_artifact_id": "preview-demo",
            "receipt_id": "receipt-demo",
        }
    )
    validation_example = copy.deepcopy(
        next(
            example
            for example in cast(list[dict[str, Any]], _load_json(_VOICE_SCHEMA)["examples"])
            if example["resource_type"] == "voice_validation"
        )
    )
    result_validator.validate(
        {
            "kind": "voice_validation",
            "validation": validation_example,
            "validation_report_artifact_id": "validation-report-demo",
            "receipt_id": "receipt-demo",
        }
    )
    with pytest.raises(ValidationError):
        result_validator.validate(
            {
                "kind": "voice_design",
                "voice_draft_id": "draft-demo",
                "draft_revision": "2",
                "preview_artifact_ids": [],
                "receipt_id": "receipt-demo",
            }
        )

    operation: dict[str, Any] = {
        "id": "op-demo",
        "object": "nexus.operation",
        "kind": "asr",
        "status": "succeeded",
        "cleanup_state": "not_required",
        "execution_revision": "1",
        "receipt": {
            "receipt_id": "receipt-demo",
            "requested": {"language": "zh"},
            "effective": {"language": "zh"},
            "observed": {"sample_rate_hz": 24000},
            "missing": [],
            "unknown": [],
        },
        "error": None,
        "created_at": "2026-10-22T00:00:00Z",
        "updated_at": "2026-10-22T00:00:01Z",
    }
    operation_validator = _component_validator(speech, "Operation")
    operation_validator.validate(operation)
    succeeded_with_unverified_cleanup = copy.deepcopy(operation)
    succeeded_with_unverified_cleanup["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        operation_validator.validate(succeeded_with_unverified_cleanup)
    operation_without_receipt = copy.deepcopy(operation)
    operation_without_receipt["receipt"] = None
    with pytest.raises(ValidationError):
        operation_validator.validate(operation_without_receipt)
    job_operation = copy.deepcopy(operation)
    job_operation["job_id"] = "job-demo"
    job_operation["session_id"] = None
    operation_validator.validate(job_operation)
    session_operation = copy.deepcopy(operation)
    session_operation["job_id"] = None
    session_operation["session_id"] = "session-demo"
    operation_validator.validate(session_operation)
    dual_owned_operation = copy.deepcopy(job_operation)
    dual_owned_operation["session_id"] = "session-demo"
    with pytest.raises(ValidationError):
        operation_validator.validate(dual_owned_operation)
    failed_operation = copy.deepcopy(operation)
    failed_operation["status"] = "failed"
    failed_operation["error"] = None
    with pytest.raises(ValidationError):
        operation_validator.validate(failed_operation)
    failed_operation["error"] = {
        "code": "engine_failure",
        "message": "synthetic failure",
        "retryable": False,
    }
    operation_validator.validate(failed_operation)
    failed_operation["status"] = "cancelled"
    with pytest.raises(ValidationError):
        operation_validator.validate(failed_operation)
    failed_operation["error"] = None
    failed_operation["cleanup_state"] = "quarantined"
    with pytest.raises(ValidationError):
        operation_validator.validate(failed_operation)


def test_realtime_session_terminal_state_requires_cleanup_and_failure_facts() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    session_validator = _component_validator(speech, "RealtimeSession")
    session: dict[str, Any] = {
        "id": "session-demo",
        "object": "nexus.realtime_session",
        "status": "created",
        "expires_at": "2026-10-23T00:00:30Z",
        "connection_url": "wss://speechnexus.example/v1/realtime?session_id=session-demo",
        "effective_configuration": {
            "models": {"asr": {"id": "asr-quality", "revision": "1"}},
            "policy_revision": "1",
            "catalog_revision": "1",
        },
        "limits": {
            "max_input_chunk_ms": 250,
            "max_message_bytes": 65536,
            "input_credit_samples": 48000,
            "output_credit_samples": 48000,
            "max_segment_ms": 30000,
            "max_session_duration_ms": 28800000,
        },
        "cleanup_state": "not_required",
        "terminal_operations": [],
        "closed_at": None,
        "error": None,
    }
    session_validator.validate(session)

    active = copy.deepcopy(session)
    active["status"] = "active"
    active["cleanup_state"] = "pending"
    session_validator.validate(active)
    active["cleanup_state"] = "not_required"
    with pytest.raises(ValidationError):
        session_validator.validate(active)

    closed = copy.deepcopy(session)
    closed["status"] = "closed"
    closed["connection_url"] = None
    closed["closed_at"] = "2026-10-23T00:10:00Z"
    session_validator.validate(closed)
    closed["connection_url"] = session["connection_url"]
    with pytest.raises(ValidationError):
        session_validator.validate(closed)

    failed = copy.deepcopy(closed)
    failed["status"] = "failed"
    failed["connection_url"] = None
    failed["cleanup_state"] = "quarantined"
    failed["error"] = {
        "code": "cleanup_unconfirmed",
        "message": "engine termination could not be confirmed",
        "retryable": False,
    }
    session_validator.validate(failed)
    failed_without_error = copy.deepcopy(failed)
    failed_without_error["error"] = None
    with pytest.raises(ValidationError):
        session_validator.validate(failed_without_error)

    expired = copy.deepcopy(session)
    expired["status"] = "expired"
    expired["connection_url"] = None
    expired["closed_at"] = "2026-10-23T00:00:30Z"
    session_validator.validate(expired)
    expired["cleanup_state"] = "complete"
    with pytest.raises(ValidationError):
        session_validator.validate(expired)

    premature_operation = copy.deepcopy(session)
    premature_operation["terminal_operations"] = [
        {
            "operation_id": "op-demo",
            "kind": "asr",
            "status": "succeeded",
            "cleanup_state": "complete",
            "receipt_id": "receipt-demo",
        }
    ]
    with pytest.raises(ValidationError):
        session_validator.validate(premature_operation)

    terminal_operation_validator = _component_validator(speech, "RealtimeTerminalOperation")
    terminal_operation_validator.validate(
        {
            "operation_id": "op-demo",
            "kind": "asr",
            "status": "succeeded",
            "cleanup_state": "complete",
            "receipt_id": "receipt-demo",
        }
    )
    with pytest.raises(ValidationError):
        terminal_operation_validator.validate(
            {
                "operation_id": "op-demo",
                "kind": "asr",
                "status": "succeeded",
                "cleanup_state": "quarantined",
                "receipt_id": "receipt-demo",
            }
        )
    with pytest.raises(ValidationError):
        terminal_operation_validator.validate(
            {
                "operation_id": "op-demo",
                "kind": "asr",
                "status": "succeeded",
                "cleanup_state": "complete",
                "receipt_id": None,
            }
        )

    terminal_reference: dict[str, Any] = {
        "operation_id": "op-demo",
        "kind": "asr",
        "status": "succeeded",
        "cleanup_state": "complete",
        "receipt_id": "receipt-demo",
    }
    terminal_session = copy.deepcopy(closed)
    terminal_session["connection_url"] = None
    terminal_session["terminal_operations"] = [terminal_reference]
    session_validator.validate(terminal_session)
    operation: dict[str, Any] = {
        "id": "op-demo",
        "object": "nexus.operation",
        "kind": "asr",
        "status": "succeeded",
        "cleanup_state": "complete",
        "job_id": None,
        "session_id": "session-demo",
        "execution_revision": "1",
        "receipt": {
            "receipt_id": "receipt-demo",
            "requested": {},
            "effective": {},
            "observed": {},
            "missing": [],
            "unknown": [],
        },
        "error": None,
        "created_at": "2026-10-23T00:00:00Z",
        "updated_at": "2026-10-23T00:10:00Z",
    }
    operation_validator = _component_validator(speech, "Operation")
    operation_validator.validate(operation)
    _assert_realtime_terminal_operations(terminal_session, [operation])

    operation_from_other_session = copy.deepcopy(operation)
    operation_from_other_session["session_id"] = "other-session"
    operation_validator.validate(operation_from_other_session)
    with pytest.raises(ValueError, match="another session"):
        _assert_realtime_terminal_operations(terminal_session, [operation_from_other_session])

    mismatched_terminal_receipt = copy.deepcopy(terminal_session)
    mismatched_terminal_receipt["terminal_operations"][0]["receipt_id"] = "other-receipt"
    session_validator.validate(mismatched_terminal_receipt)
    with pytest.raises(ValueError, match="mismatched receipt"):
        _assert_realtime_terminal_operations(mismatched_terminal_receipt, [operation])

    repeated_terminal_operation = copy.deepcopy(terminal_session)
    repeated_terminal_operation["terminal_operations"].append(
        copy.deepcopy(repeated_terminal_operation["terminal_operations"][0])
    )
    session_validator.validate(repeated_terminal_operation)
    with pytest.raises(ValueError, match="repeat operation_id"):
        _assert_realtime_terminal_operations(repeated_terminal_operation, [operation])

    unresolved_terminal_operation = copy.deepcopy(terminal_session)
    unresolved_terminal_operation["terminal_operations"][0]["operation_id"] = "op-missing"
    session_validator.validate(unresolved_terminal_operation)
    with pytest.raises(ValueError, match="cannot be resolved"):
        _assert_realtime_terminal_operations(unresolved_terminal_operation, [operation])


def test_ready_assets_require_validated_media_facts() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    validator = _component_validator(speech, "Asset")
    audio_asset: dict[str, Any] = {
        "id": "asset-demo",
        "object": "nexus.asset",
        "workspace_id": "workspace-demo",
        "purpose": "asr_input",
        "status": "ready",
        "media_type": "audio/wav",
        "bytes": 4096,
        "sha256": "sha256:" + "b" * 64,
        "duration_ms": 1000,
        "sample_rate_hz": 24000,
        "channels": 1,
        "created_at": "2026-10-23T00:00:00Z",
        "expires_at": None,
    }
    validator.validate(audio_asset)

    missing_duration = copy.deepcopy(audio_asset)
    missing_duration["duration_ms"] = None
    with pytest.raises(ValidationError):
        validator.validate(missing_duration)
    empty_audio = copy.deepcopy(audio_asset)
    empty_audio["bytes"] = 0
    with pytest.raises(ValidationError):
        validator.validate(empty_audio)

    text_asset = copy.deepcopy(audio_asset)
    text_asset.update(
        {
            "purpose": "text_input",
            "media_type": "text/plain; charset=utf-8",
            "duration_ms": None,
            "sample_rate_hz": None,
            "channels": None,
        }
    )
    validator.validate(text_asset)
    text_asset_with_audio_metadata = copy.deepcopy(text_asset)
    text_asset_with_audio_metadata["channels"] = 1
    with pytest.raises(ValidationError):
        validator.validate(text_asset_with_audio_metadata)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "tts",
            "model": "tts-interactive",
            "input": {"text": "hello"},
            "voice": {"id": "narrator", "revision": "1"},
        },
        {
            "kind": "voice_design",
            "model": "voice-design",
            "input": {
                "description": "Calm narration voice",
                "preview_text": "Welcome.",
            },
        },
        {
            "kind": "voice_clone",
            "model": "voice-clone",
            "input": {
                "reference_audio_asset_id": "ast_reference",
                "source_use_confirmed": True,
            },
        },
        {
            "kind": "voice_preview",
            "input": {
                "voice_draft_id": "vdraft_demo",
                "draft_revision": "1",
                "text": "Welcome.",
            },
        },
        {
            "kind": "voice_validation",
            "input": {
                "voice_draft_id": "vdraft_demo",
                "draft_revision": "1",
                "validation_set_revision": "2",
            },
        },
    ],
)
def test_job_create_discriminators_validate(payload: dict[str, Any]) -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    _component_validator(speech, "JobCreateRequest").validate(payload)


def test_realtime_session_models_match_modalities() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    validator = _component_validator(speech, "RealtimeSessionCreateRequest")
    validator.validate(
        {
            "profile": {"id": "caption-live", "revision": "1"},
            "modalities": ["asr"],
            "models": {"asr": {"id": "asr-quality", "revision": "7"}},
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "profile": {"id": "caption-live", "revision": "1"},
                "modalities": ["asr"],
                "models": {"tts": {"id": "tts-interactive", "revision": "5"}},
            }
        )


def test_voice_creation_inputs_do_not_require_an_extra_kind_field() -> None:
    speech = _resolved_yaml(_SPEECH_OPENAPI)
    _component_validator(speech, "VoiceDesignCreateRequest").validate(
        {
            "model": "voice-design",
            "input": {
                "description": "Warm narration voice",
                "preview_text": "Welcome.",
            },
        }
    )
    _component_validator(speech, "VoiceCloneCreateRequest").validate(
        {
            "model": "voice-clone",
            "input": {
                "reference_audio_asset_id": "ast_reference",
                "source_use_confirmed": True,
            },
        }
    )


def test_workbench_meeting_requires_a_transcript_or_speech_source() -> None:
    workbench = _resolved_yaml(_WORKBENCH_OPENAPI)
    validator = _component_validator(workbench, "MeetingCreateRequest")
    with pytest.raises(ValidationError):
        validator.validate({"display_name": "Weekly sync"})
