"""Contract and schema invariant tests for P0-A machine contracts and P0-B coordination DDL."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import pytest
from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from openapi_spec_validator import validate
from referencing import Registry, Resource

from tests.support.safe_yaml import load_yaml

_ROOT = Path(__file__).resolve().parents[2]
_BINDING_SCHEMA_PATH = _ROOT / "contracts" / "model-binding.schema.json"
_CAPACITY_MANIFEST_SCHEMA_PATH = _ROOT / "contracts" / "capacity-manifest.schema.json"
_QUALIFICATION_REPORT_SCHEMA_PATH = _ROOT / "contracts" / "qualification-report.schema.json"
_EXECUTION_CONTROL_OPENAPI_PATH = (
    _ROOT / "contracts" / "internal" / "execution-control.openapi.yaml"
)
_RUNTIME_CONTROL_OPENAPI_PATH = _ROOT / "contracts" / "internal" / "runtime-control.openapi.yaml"
_SPEECH_OPENAPI_PATH = _ROOT / "contracts" / "speech-openapi.yaml"
_SCENARIO_PROFILE_SCHEMA_PATH = _ROOT / "contracts" / "scenario-profile.schema.json"
_VOICE_RESOURCE_SCHEMA_PATH = _ROOT / "contracts" / "voice-resource.schema.json"
_REALTIME_EVENTS_SCHEMA_PATH = _ROOT / "contracts" / "realtime-events.schema.json"
_CAPABILITY_DOMAINS_SCHEMA_PATH = _ROOT / "contracts" / "shared" / "capability-domains.schema.json"
_COORDINATION_SQL_PATH = _ROOT / "tests" / "fixtures" / "coordination" / "legacy_postgresql.sql"


def _load_json(path: Path) -> dict[str, Any]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    document = cast(dict[str, Any], raw)
    _resolve_local_refs(document, path)
    return document


def _load_yaml(path: Path) -> dict[str, Any]:
    raw: object = load_yaml(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    document = cast(dict[str, Any], raw)
    _resolve_local_refs(document, path)
    return document


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


def _retrieve_contract(uri: str) -> Resource[Any]:
    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    path = Path(unquote(parsed.path))
    return Resource.from_contents(_load_json(path))


_REGISTRY: Registry[Any] = cast(Any, Registry)(retrieve=_retrieve_contract)


def _assert_qualification_matches(
    report: dict[str, Any],
    binding: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Executable cross-document identity rules for release evidence."""
    reference = cast(dict[str, Any], report["binding"])
    if reference["id"] != binding["binding_id"] or reference["revision"] != binding["revision"]:
        raise ValueError("qualification binding reference does not identify the cited binding")
    if report["binding_revision_id"] != binding["revision_id"]:
        raise ValueError("qualification binding_revision_id does not match the binding revision")
    if manifest["binding_revision_id"] != binding["revision_id"]:
        raise ValueError("capacity manifest does not belong to the binding revision")
    if report["capacity_manifest_id"] != manifest["manifest_id"]:
        raise ValueError("qualification report does not identify the cited capacity manifest")
    if report["release_tier"] != manifest["release_tier"]:
        raise ValueError("qualification and capacity release tiers differ")
    if binding["execution_recipe"]["capacity_manifest_id"] != manifest["manifest_id"]:
        raise ValueError("binding execution recipe cites a different capacity manifest")
    if binding["publication"]["capacity_manifest_id"] != manifest["manifest_id"]:
        raise ValueError("published binding cites a different capacity manifest")
    if binding["publication"]["qualification_report_id"] != report["report_id"]:
        raise ValueError("published binding cites a different qualification report")

    stages = {
        cast(str, stage["stage_id"]): stage
        for stage in cast(list[dict[str, Any]], manifest["stage_graph"])
    }
    components = cast(dict[str, dict[str, Any]], report["software"]["engine_components"])
    if set(components) != set(stages):
        raise ValueError("engine component evidence must match stage_graph one-to-one")
    for stage_id, component in components.items():
        if component["engine_kind"] != stages[stage_id]["engine_kind"]:
            raise ValueError(f"engine kind for {stage_id} does not match the stage declaration")

    if report["topology"]["memory_architecture"] != manifest["topology_ref"]["memory_architecture"]:
        raise ValueError("qualification topology memory architecture differs from the manifest")

    qualification = cast(dict[str, Any], manifest["qualification"])
    for field in ("qualified_at", "expires_at"):
        if report[field] != qualification[field]:
            raise ValueError(f"qualification {field} differs from the capacity manifest evidence")


def test_capacity_manifest_schema_and_positive_examples_are_valid() -> None:
    schema = _load_json(_CAPACITY_MANIFEST_SCHEMA_PATH)
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, registry=_REGISTRY, format_checker=FormatChecker())

    examples = schema.get("examples")
    assert isinstance(examples, list)
    assert len(examples) >= 1
    qualification_schema = _load_json(_QUALIFICATION_REPORT_SCHEMA_PATH)
    qualification_validator = validator_for(qualification_schema)(
        qualification_schema,
        registry=_REGISTRY,
        format_checker=FormatChecker(),
    )
    qualification_reports = {
        report["report_id"]: report
        for report in cast(
            list[dict[str, Any]],
            qualification_schema["examples"],
        )
    }
    binding_schema = _load_json(_BINDING_SCHEMA_PATH)
    bindings = {
        binding["revision_id"]: binding
        for binding in cast(
            list[dict[str, Any]],
            binding_schema["examples"],
        )
    }
    for example in examples:
        validator.validate(example)
        stage_ids = [stage["stage_id"] for stage in example["stage_graph"]]
        assert len(stage_ids) == len(set(stage_ids))
        assert all(isinstance(stage["emits_audio_pcm"], bool) for stage in example["stage_graph"])
        binding = bindings[example["binding_revision_id"]]
        assert binding["execution_recipe"]["capacity_manifest_id"] == example["manifest_id"]
        assert binding["publication"]["capacity_manifest_id"] == example["manifest_id"]
        assert set(binding["execution_recipe"]["stage_roles"]) == {
            stage["stage_role"] for stage in example["stage_graph"]
        }
        report = qualification_reports[example["qualification"]["qualification_report_id"]]
        _assert_qualification_matches(report, binding, example)

    example = cast(dict[str, Any], examples[0])
    binding = bindings[example["binding_revision_id"]]
    report = qualification_reports[example["qualification"]["qualification_report_id"]]

    wrong_binding_reference = copy.deepcopy(report)
    wrong_binding_reference["binding"]["revision"] = "999"
    qualification_validator.validate(wrong_binding_reference)
    with pytest.raises(ValueError, match="binding reference"):
        _assert_qualification_matches(wrong_binding_reference, binding, example)

    mismatched_engine_kind = copy.deepcopy(report)
    first_stage_id = example["stage_graph"][0]["stage_id"]
    mismatched_engine_kind["software"]["engine_components"][first_stage_id]["engine_kind"] = "vllm"
    qualification_validator.validate(mismatched_engine_kind)
    with pytest.raises(ValueError, match="engine kind"):
        _assert_qualification_matches(mismatched_engine_kind, binding, example)

    mismatched_binding_revision = copy.deepcopy(example)
    mismatched_binding_revision["binding_revision_id"] = "mbrev-other-1"
    validator.validate(mismatched_binding_revision)
    with pytest.raises(ValueError, match="does not belong"):
        _assert_qualification_matches(report, binding, mismatched_binding_revision)

    mismatched_release_tier = copy.deepcopy(report)
    mismatched_release_tier["release_tier"] = "best_effort"
    qualification_validator.validate(mismatched_release_tier)
    with pytest.raises(ValueError, match="release tiers"):
        _assert_qualification_matches(mismatched_release_tier, binding, example)

    mismatched_memory_architecture = copy.deepcopy(report)
    mismatched_memory_architecture["topology"]["memory_architecture"] = "discrete_device_memory"
    qualification_validator.validate(mismatched_memory_architecture)
    with pytest.raises(ValueError, match="memory architecture"):
        _assert_qualification_matches(mismatched_memory_architecture, binding, example)

    invalid_qualification_time = copy.deepcopy(report)
    invalid_qualification_time["qualified_at"] = "2026-10-22"
    with pytest.raises(ValidationError):
        qualification_validator.validate(invalid_qualification_time)


def test_capacity_manifest_envelope_aggregation_is_frozen() -> None:
    schema = _load_json(_CAPACITY_MANIFEST_SCHEMA_PATH)
    example = cast(list[dict[str, Any]], schema["examples"])[0]
    stages = cast(list[dict[str, Any]], example["stage_graph"])
    envelope = cast(dict[str, Any], example["resource_envelope"])

    for budget_name in ("resident_budget", "loading_peak_budget"):
        envelope_budget = cast(dict[str, int], envelope[budget_name])
        for resource, declared_total in envelope_budget.items():
            assert declared_total == sum(
                cast(dict[str, int], stage[budget_name])[resource] for stage in stages
            )

    per_slot = cast(dict[str, int], envelope["per_slot_incremental_budget"])
    for resource, declared_total in per_slot.items():
        stage_values = [
            cast(dict[str, int], stage["per_slot_incremental_budget"])[resource] for stage in stages
        ]
        if resource == "engine_active_slots":
            assert declared_total == max(stage_values)
        else:
            assert declared_total == sum(stage_values)

    assert envelope["max_active_slots"] == min(stage["max_active_slots"] for stage in stages)
    assert envelope["realtime_reserved_slots"] <= envelope["max_active_slots"]


def _terminal_schema_alias(node: dict[str, Any], source: Path) -> tuple[Path, str]:
    """Follow only pure aliases, proving the terminal canonical source identity."""
    seen: set[tuple[Path, str]] = set()
    while True:
        assert set(node) == {"$ref"}
        ref = node["$ref"]
        assert isinstance(ref, str)
        parsed = urlparse(ref)
        assert not parsed.scheme and not parsed.netloc
        source = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
        assert source.is_relative_to(_ROOT / "contracts")
        identity = (source, parsed.fragment)
        assert identity not in seen, "schema alias cycle"
        seen.add(identity)
        target = json.loads(source.read_text(encoding="utf-8"))
        assert parsed.fragment.startswith("/")
        for token in parsed.fragment[1:].split("/"):
            target = target[unquote(token).replace("~1", "/").replace("~0", "~")]
        assert isinstance(target, dict)
        if "$ref" not in target:
            return identity
        node = target


def test_shared_speech_control_domains_have_one_source() -> None:
    domains = json.loads(_CAPABILITY_DOMAINS_SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = domains["$defs"]
    assert defs["AudioFormat"]["enum"] == ["mp3", "wav", "pcm"]
    assert defs["TranscriptFormat"]["enum"] == ["json", "text", "verbose_json", "srt", "vtt"]
    assert defs["VadMode"]["enum"] == ["server_vad", "manual"]
    assert defs["FinalizationMode"]["enum"] == ["full_segment", "streaming_finalize"]
    assert defs["AuxiliaryMode"]["enum"] == ["off", "required", "best_effort"]
    assert defs["TextCommitMode"]["enum"] == ["explicit", "sentence"]
    assert defs["AudioDeliveryMode"]["enum"] == ["complete", "chunked"]
    assert defs["OperationKind"]["enum"] == ["asr", "tts", "voice_preview", "voice_validation"]

    binding = json.loads(_BINDING_SCHEMA_PATH.read_text(encoding="utf-8"))
    tts_domain = binding["$defs"]["TtsParameterDomain"]["properties"]
    assert tts_domain["audio_formats"]["items"]["$ref"].endswith(
        "shared/capability-domains.schema.json#/$defs/AudioFormat"
    )
    assert tts_domain["text_commit_modes"]["items"]["$ref"].endswith(
        "shared/capability-domains.schema.json#/$defs/TextCommitMode"
    )
    assert tts_domain["audio_delivery_modes"]["items"]["$ref"].endswith(
        "shared/capability-domains.schema.json#/$defs/AudioDeliveryMode"
    )

    scenario = json.loads(_SCENARIO_PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
    scenario_defs = scenario["$defs"]
    for section, field, domain in (
        ("AsrParameters", "finalization", "FinalizationMode"),
        ("AsrParameters", "alignment", "AuxiliaryMode"),
        ("TtsParameters", "text_commit_mode", "TextCommitMode"),
        ("OutputParameters", "audio_format", "AudioFormat"),
    ):
        assert _terminal_schema_alias(
            scenario_defs[section]["properties"][field], _SCENARIO_PROFILE_SCHEMA_PATH
        ) == (_CAPABILITY_DOMAINS_SCHEMA_PATH, f"/$defs/{domain}")

    voice = json.loads(_VOICE_RESOURCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    voice_capabilities = voice["$defs"]["VoiceCapabilities"]["properties"]
    assert voice_capabilities["audio_formats"]["items"]["$ref"].endswith(
        "shared/capability-domains.schema.json#/$defs/AudioFormat"
    )
    assert voice_capabilities["modes"]["items"]["$ref"].endswith(
        "shared/capability-domains.schema.json#/$defs/AudioDeliveryMode"
    )

    realtime = json.loads(_REALTIME_EVENTS_SCHEMA_PATH.read_text(encoding="utf-8"))
    realtime_defs = realtime["$defs"]
    for name in (
        "VadMode",
        "FinalizationMode",
        "AuxiliaryMode",
        "TextCommitMode",
        "AudioDeliveryMode",
        "OperationKind",
    ):
        assert realtime_defs[name]["$ref"].endswith(
            f"shared/capability-domains.schema.json#/$defs/{name}"
        )

    speech = load_yaml(_SPEECH_OPENAPI_PATH.read_text(encoding="utf-8"))
    speech_schemas = speech["components"]["schemas"]
    for name in ("AudioFormat", "TranscriptFormat", "TextCommitMode", "OperationKind"):
        assert speech_schemas[name]["$ref"].endswith(
            f"shared/capability-domains.schema.json#/$defs/{name}"
        )


def test_capacity_manifest_rejects_external_production_and_missing_parent_budget() -> None:
    schema = _load_json(_CAPACITY_MANIFEST_SCHEMA_PATH)
    validator = validator_for(schema)(schema, registry=_REGISTRY)
    examples = cast(list[dict[str, Any]], schema["examples"])
    base_example = copy.deepcopy(examples[0])

    # 1. external management_mode cannot claim production release_tier
    external_prod = copy.deepcopy(base_example)
    external_prod["management_mode"] = "external"
    external_prod["release_tier"] = "production"
    with pytest.raises(ValidationError):
        validator.validate(external_prod)

    # 2. external management_mode with experimental release_tier is allowed
    external_exp = copy.deepcopy(base_example)
    external_exp["management_mode"] = "external"
    external_exp["release_tier"] = "experimental"
    validator.validate(external_exp)

    production = copy.deepcopy(base_example)
    production["release_tier"] = "production"
    validator.validate(production)
    production_without_fencing = copy.deepcopy(production)
    production_without_fencing["cancellation_and_fencing"]["supports_fencing_barrier"] = False
    with pytest.raises(ValidationError):
        validator.validate(production_without_fencing)
    production_without_output_quota = copy.deepcopy(production)
    production_without_output_quota["rate_and_segment_limits"]["hard_output_quota"] = False
    with pytest.raises(ValidationError):
        validator.validate(production_without_output_quota)

    abort_without_support = copy.deepcopy(base_example)
    abort_without_support["cancellation_and_fencing"]["supports_request_abort"] = False
    with pytest.raises(ValidationError):
        validator.validate(abort_without_support)
    quarantine_with_abort = copy.deepcopy(base_example)
    quarantine_with_abort["cancellation_and_fencing"]["cancellation_mode"] = (
        "connection_drop_with_quarantine"
    )
    with pytest.raises(ValidationError):
        validator.validate(quarantine_with_abort)

    # 3. on_demand loading_policy requires integer idle_reclaim_cooldown_ms >= 1000
    on_demand_missing_cooldown = copy.deepcopy(base_example)
    on_demand_missing_cooldown["loading_policy"] = "on_demand"
    on_demand_missing_cooldown["cancellation_and_fencing"]["idle_reclaim_cooldown_ms"] = None
    with pytest.raises(ValidationError):
        validator.validate(on_demand_missing_cooldown)

    # 4. requires_shared_parent_budget must be true
    unshared_budget = copy.deepcopy(base_example)
    unshared_budget["topology_ref"]["requires_shared_parent_budget"] = False
    with pytest.raises(ValidationError):
        validator.validate(unshared_budget)

    # 5. unknown properties are rejected
    unknown_field = copy.deepcopy(base_example)
    unknown_field["gpu_marketing_name"] = "NVIDIA GB10"
    with pytest.raises(ValidationError):
        validator.validate(unknown_field)

    missing_audio_flag = copy.deepcopy(base_example)
    del missing_audio_flag["stage_graph"][0]["emits_audio_pcm"]
    with pytest.raises(ValidationError):
        validator.validate(missing_audio_flag)

    audio_stage_with_text_only_formats = copy.deepcopy(base_example)
    audio_stage_with_text_only_formats["stage_graph"][0]["emits_audio_pcm"] = True
    with pytest.raises(ValidationError):
        validator.validate(audio_stage_with_text_only_formats)
    audio_stage_with_text_only_formats["supported_output_formats"].append("wav")
    validator.validate(audio_stage_with_text_only_formats)


def test_internal_execution_and_runtime_control_openapi_specs_are_valid() -> None:
    exec_doc = _load_yaml(_EXECUTION_CONTROL_OPENAPI_PATH)
    validate(exec_doc)
    assert set(exec_doc["paths"]) == {
        "/internal/v1/executions/submit",
        "/internal/v1/executions/{submit_intent_id}",
        "/internal/v1/executions/{submit_intent_id}/abort",
        "/internal/v1/gateways/{gateway_id}/fencing-barrier",
        "/internal/v1/instances/{instance_id}/drain",
    }

    runtime_doc = _load_yaml(_RUNTIME_CONTROL_OPENAPI_PATH)
    validate(runtime_doc)
    assert set(runtime_doc["paths"]) == {
        "/internal/v1/nodes/{node_id}/instance-intents",
        "/internal/v1/nodes/{node_id}/instance-intents/{intent_id}",
        "/internal/v1/nodes/{node_id}/instances/{instance_id}/generations/"
        "{instance_generation}/verified-stop",
        "/internal/v1/nodes/{node_id}/reconcile",
    }

    for document in (exec_doc, runtime_doc):
        assert document["security"] == [{"ComponentBearer": []}]
        security_scheme = document["components"]["securitySchemes"]["ComponentBearer"]
        assert security_scheme["type"] == "http"
        assert security_scheme["scheme"] == "bearer"
        for path_item in document["paths"].values():
            for method, operation in path_item.items():
                if method in {"get", "post", "put", "patch", "delete"}:
                    assert "401" in operation["responses"], (method, operation["operationId"])
                    if "requestBody" in operation:
                        assert "422" in operation["responses"], (
                            method,
                            operation["operationId"],
                        )


def test_legacy_coordination_fixture_retains_original_database_guards() -> None:
    sql_text = _COORDINATION_SQL_PATH.read_text(encoding="utf-8")
    required_tables = (
        "CREATE TABLE host_nodes",
        "CREATE TABLE resource_pools",
        "CREATE TABLE scheduler_pool_leases",
        "CREATE TABLE engine_instances",
        "CREATE TABLE runtime_instance_intents",
        "CREATE TABLE admission_leases",
        "CREATE TABLE gateway_fencing_barriers",
        "CREATE TABLE submit_intents",
    )
    for table_decl in required_tables:
        assert table_decl in sql_text

    required_constraints = (
        "ck_host_nodes_unified_budget_ceiling",
        "ck_scheduler_pool_leases_active_requires_reconciled",
        "ck_engine_instances_peak_release_requires_ready",
        "ck_runtime_intents_stop_evidence",
        "ck_admission_leases_release_invariant",
        "ck_admission_leases_reservations_non_negative",
        "ck_engine_instances_stopped_evidence",
        "ck_runtime_intents_prepare_evidence",
        "ck_runtime_intents_fencing_positive",
        "fk_runtime_intents_execution_identity",
        "ck_submit_intents_fencing_positive",
        "fk_admission_leases_execution_identity",
        "uq_admission_leases_operation_attempt_stage",
        "fk_submit_intents_lease_stage",
        "fk_submit_intents_lease_execution_identity",
        "uq_submit_intents_operation_attempt_stage",
        "uq_engine_instances_execution_identity",
        "ck_submit_intents_execution_cleanup_matrix",
        "ck_submit_intents_aborted_requires_evidence",
        "ck_submit_intents_quarantine_invariant",
    )
    for constraint_name in required_constraints:
        assert constraint_name in sql_text
