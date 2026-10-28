#!/usr/bin/env python3
"""Verify one capacity-manifest / qualification-report release bundle.

The command is read-only. It validates the frozen JSON schemas, cross-document
release rules, content digests, and (when a database or snapshot is supplied)
the binding definition, publication evidence, host budget, engine instance
vectors, resource pool, and workspace quota.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

try:
    from jsonschema import FormatChecker
    from jsonschema.validators import validator_for
    from referencing import Registry, Resource
except ImportError:  # pragma: no cover - exercised by the documented command only
    print(
        "verify-release-evidence: missing dependency; run with "
        "--with 'jsonschema>=4.23,<5' --with 'rfc8785>=0.1.4,<0.2' "
        "(and psycopg for --database-url)",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

try:
    import rfc8785
except ImportError:  # pragma: no cover - exercised by the documented command only
    print(
        "verify-release-evidence: missing rfc8785; run with --with 'rfc8785>=0.1.4,<0.2'",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

ROOT = Path(__file__).resolve().parents[2]
CAPACITY_SCHEMA = ROOT / "contracts" / "capacity-manifest.schema.json"
REPORT_SCHEMA = ROOT / "contracts" / "qualification-report.schema.json"
PHYSICAL_DIMENSIONS = (
    "unified_physical_memory_bytes",
    "device_memory_bytes",
    "host_memory_bytes",
    "cpu_decode_millicores",
    "host_pcm_buffer_bytes",
    "scratch_disk_bytes",
)
VECTOR_KEYS = (
    *PHYSICAL_DIMENSIONS,
    "ingress_bandwidth_bps",
    "egress_bandwidth_bps",
    "object_storage_bytes",
)
REQUIRED_EVIDENCE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SCHEMA_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
PLACEHOLDER_ID = re.compile(r"(^|[-_.:])local-test([-_.:]|$)|local-test-capacity")


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    digests: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> bool:
        return any(c.status == "FAIL" for c in self.checks)

    @property
    def warned(self) -> bool:
        return any(c.status == "WARN" for c in self.checks)


def canonical(value: Any) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError(f"value is not RFC 8785 JSON: {exc}") from None


def sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha_value(value: Any) -> str:
    return sha_bytes(canonical(value))


def file_sha(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from None


def _resolve_local_refs(node: Any, source: Path) -> None:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith(
            ("#", "http://", "https://", "file://")
        ):
            path_part, separator, fragment = reference.partition("#")
            target = (source.parent / path_part).resolve()
            node["$ref"] = target.as_uri() + (f"#{fragment}" if separator else "")
        for value in node.values():
            _resolve_local_refs(value, source)
    elif isinstance(node, list):
        for value in node:
            _resolve_local_refs(value, source)


def load_contract(path: Path) -> dict[str, Any]:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError(f"contract is not an object: {path}")
    document = cast(dict[str, Any], raw)
    _resolve_local_refs(document, path)
    return document


def _retrieve(uri: str) -> Resource[Any]:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported contract reference: {uri}")
    return Resource.from_contents(load_contract(Path(unquote(parsed.path))))


REGISTRY: Registry[Any] = cast(Any, Registry)(retrieve=_retrieve)


def schema_errors(document: Any, schema_path: Path) -> list[str]:
    schema = load_contract(schema_path)
    validator = validator_for(schema)(schema, registry=REGISTRY, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors
    ]


def integer(value: Any) -> int:
    if type(value) is bool:
        raise ValueError("boolean is not an integer")
    return int(value)


def timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field_name} is not an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} has no timezone")
    return parsed.astimezone(UTC)


def check_envelope(manifest: dict[str, Any], checks: Report) -> None:
    stages = manifest.get("stage_graph")
    envelope = manifest.get("resource_envelope")
    if not isinstance(stages, list) or not stages or not isinstance(envelope, dict):
        checks.add("manifest.envelope", "FAIL", "stage_graph or resource_envelope missing")
        return
    expected: dict[str, dict[str, int]] = {}
    for budget in ("resident_budget", "loading_peak_budget", "per_slot_incremental_budget"):
        expected[budget] = {}
        for key in VECTOR_KEYS:
            expected[budget][key] = sum(integer(stage[budget][key]) for stage in stages)
        expected[budget]["engine_active_slots"] = max(
            integer(stage[budget]["engine_active_slots"]) for stage in stages
        )
    expected_max = min(integer(stage["max_active_slots"]) for stage in stages)
    mismatches: list[str] = []
    for budget in expected:
        actual = envelope[budget]
        if any(integer(actual[key]) != value for key, value in expected[budget].items()):
            mismatches.append(budget)
    if integer(envelope["max_active_slots"]) != expected_max:
        mismatches.append("max_active_slots")
    if integer(envelope["realtime_reserved_slots"]) > expected_max:
        mismatches.append("realtime_reserved_slots")
    if mismatches:
        checks.add("manifest.envelope", "FAIL", "aggregation mismatch: " + ", ".join(mismatches))
    else:
        checks.add(
            "manifest.envelope",
            "PASS",
            f"3 vectors aggregate exactly; max_active_slots={expected_max}",
        )


def check_placeholders(
    manifest: dict[str, Any],
    report: dict[str, Any],
    evidence: dict[str, Any],
    definition: dict[str, Any],
    checks: Report,
) -> None:
    recipe = definition.get("execution_recipe")
    values = {
        "manifest.manifest_id": manifest.get("manifest_id"),
        "manifest.binding_revision_id": manifest.get("binding_revision_id"),
        "report.report_id": report.get("report_id"),
        "report.binding_revision_id": report.get("binding_revision_id"),
        "report.capacity_manifest_id": report.get("capacity_manifest_id"),
        "report.qualification_report_id": manifest.get("qualification", {}).get(
            "qualification_report_id"
        ),
        "evidence.capacity_manifest_id": evidence.get("capacity_manifest_id"),
        "evidence.qualification_report_id": evidence.get("qualification_report_id"),
        "definition.capacity_manifest_id": (
            recipe.get("capacity_manifest_id") if isinstance(recipe, dict) else None
        ),
    }
    bad = [
        f"{name}={value}"
        for name, value in values.items()
        if isinstance(value, str) and PLACEHOLDER_ID.search(value)
    ]
    checks.add(
        "release.placeholders",
        "FAIL" if bad else "PASS",
        "placeholder identifiers: " + ", ".join(bad)
        if bad
        else "no local-test placeholder in release identifiers",
    )


def check_report_logic(report: dict[str, Any], checks: Report) -> None:
    suite = report["test_suite"]
    scenarios = report["scenarios"]
    ids = suite["scenario_ids"]
    scenario_ids = [item["scenario_id"] for item in scenarios]
    if len(ids) != len(set(ids)) or len(scenario_ids) != len(set(scenario_ids)):
        checks.add("report.scenario_ids", "FAIL", "duplicate scenario id")
    elif set(ids) != set(scenario_ids):
        checks.add("report.scenario_ids", "FAIL", "test_suite ids do not match scenario results")
    else:
        checks.add("report.scenario_ids", "PASS", f"{len(ids)} ids match one-to-one")

    required = [item for item in scenarios if item["required"]]
    required_passed = bool(required) and all(item["status"] == "passed" for item in required)
    if any(item["status"] == "failed" for item in required):
        computed = "fail"
    elif required_passed:
        computed = "pass"
    else:
        computed = "incomplete"
    if required_passed != report["required_scenarios_passed"]:
        checks.add("report.required_scenarios_passed", "FAIL", "field disagrees with scenarios")
    else:
        checks.add("report.required_scenarios_passed", "PASS", f"computed={required_passed}")
    if computed != report["overall_status"]:
        checks.add(
            "report.overall_status",
            "FAIL",
            f"computed={computed}, declared={report['overall_status']}",
        )
    else:
        checks.add("report.overall_status", "PASS", computed)
    optional_failed = [
        item["scenario_id"]
        for item in scenarios
        if not item["required"] and item["status"] != "passed"
    ]
    if optional_failed:
        checks.add(
            "report.optional_scenarios",
            "WARN",
            "non-passing optional evidence: " + ", ".join(optional_failed),
        )
    if report["release_tier"] == "production":
        if computed != "pass" or not report.get("expires_at"):
            checks.add("report.production_gate", "FAIL", "production requires pass and expires_at")
        else:
            checks.add("report.production_gate", "PASS", "required scenarios pass and expiry set")
    elif not report.get("limitations"):
        checks.add("report.limitations", "FAIL", "experimental report requires limitations")
    else:
        checks.add("report.limitations", "PASS", f"{len(report['limitations'])} limitations")


def check_cross_document(
    manifest: dict[str, Any],
    report: dict[str, Any],
    evidence: dict[str, Any],
    artifact_pin: dict[str, Any],
    binding_definition: dict[str, Any],
    manifest_path: Path,
    report_path: Path,
    evidence_path: Path,
    source_body: Path | None,
    checks: Report,
) -> None:
    if manifest["manifest_id"] != report["capacity_manifest_id"]:
        checks.add("cross.manifest_id", "FAIL", "report cites another manifest")
    else:
        checks.add("cross.manifest_id", "PASS", manifest["manifest_id"])
    if manifest["binding_revision_id"] != report["binding_revision_id"]:
        checks.add("cross.binding_revision_id", "FAIL", "manifest/report revision ids differ")
    else:
        checks.add("cross.binding_revision_id", "PASS", manifest["binding_revision_id"])
    if manifest["release_tier"] != report["release_tier"]:
        checks.add("cross.release_tier", "FAIL", "manifest/report release tiers differ")
    else:
        checks.add("cross.release_tier", "PASS", manifest["release_tier"])
    if manifest["release_tier"] == "production" and report["overall_status"] != "pass":
        checks.add(
            "cross.production_status",
            "FAIL",
            f"production manifest cites report status={report['overall_status']}",
        )
    elif manifest["release_tier"] == "production":
        checks.add("cross.production_status", "PASS", "production report passes")
    if manifest["topology_ref"]["memory_architecture"] != report["topology"]["memory_architecture"]:
        checks.add("cross.memory_architecture", "FAIL", "topology architectures differ")
    else:
        checks.add(
            "cross.memory_architecture", "PASS", manifest["topology_ref"]["memory_architecture"]
        )

    qualification = manifest["qualification"]
    for field_name in ("qualified_at", "expires_at"):
        if qualification[field_name] != report.get(field_name):
            checks.add(f"cross.{field_name}", "FAIL", "manifest/report timestamps differ")
        else:
            checks.add(f"cross.{field_name}", "PASS", qualification[field_name])
    if qualification["qualification_report_id"] != report["report_id"]:
        checks.add("cross.report_id", "FAIL", "manifest cites another report")
    else:
        checks.add("cross.report_id", "PASS", report["report_id"])

    stages = {stage["stage_id"]: stage for stage in manifest["stage_graph"]}
    components = report["software"]["engine_components"]
    if set(stages) != set(components):
        checks.add("cross.engine_components", "FAIL", "stage/component ids differ")
    else:
        bad = [
            stage_id
            for stage_id, stage in stages.items()
            if components[stage_id]["engine_kind"] != stage["engine_kind"]
        ]
        checks.add(
            "cross.engine_components",
            "FAIL" if bad else "PASS",
            "engine kind mismatch: " + ", ".join(bad) if bad else f"{len(stages)} stages match",
        )

    model_digest = sha_value(artifact_pin["model"])
    checks.digests["model_pin_digest"] = model_digest
    asr_stages = [
        stage
        for stage in stages.values()
        if stage["stage_role"] in ("asr_preview", "asr_review", "asr_batch")
    ]
    if not asr_stages:
        checks.add("digest.model_pin", "WARN", "no ASR stage to compare with artifact pin")
    else:
        bad = [
            stage["stage_id"]
            for stage in asr_stages
            if stage["model_revision_digest"] != model_digest
        ]
        checks.add(
            "digest.model_pin",
            "FAIL" if bad else "PASS",
            "artifact-pin model digest differs: " + ", ".join(bad) if bad else model_digest,
        )

    config_digest = sha_value(
        {"artifact_pin": artifact_pin, "binding_definition": binding_definition}
    )
    checks.digests["config_bundle_digest"] = config_digest
    if qualification["config_bundle_digest"] != config_digest:
        checks.add("digest.config_bundle", "FAIL", f"expected {config_digest}")
    else:
        checks.add("digest.config_bundle", "PASS", config_digest)

    descriptor = {
        "host_node_id": report["topology"]["host_node_id"],
        "cpu_arch": report["topology"]["cpu_arch"],
        "hardware_class": report["topology"]["hardware_class"],
        "memory_architecture": report["topology"]["memory_architecture"],
        "total_physical_memory_bytes": report["topology"]["total_physical_memory_bytes"],
    }
    topology_digest = sha_value(descriptor)
    checks.digests["topology_digest"] = topology_digest
    if manifest["topology_ref"]["topology_digest"] != topology_digest:
        checks.add("digest.topology", "FAIL", f"expected {topology_digest}")
    else:
        checks.add("digest.topology", "PASS", topology_digest)

    evidence_file_digest = file_sha(evidence_path)
    checks.digests["evidence_file_digest"] = evidence_file_digest
    if report["evidence_digest"] != evidence_file_digest:
        checks.add("digest.evidence_file", "FAIL", f"expected {evidence_file_digest}")
    else:
        checks.add("digest.evidence_file", "PASS", evidence_file_digest)
    checks.digests["capacity_manifest_file_digest"] = file_sha(manifest_path)
    checks.digests["qualification_report_file_digest"] = file_sha(report_path)

    evidence_scenarios = evidence.get("scenarios", {})
    bad_scenarios: list[str] = []
    for scenario in report["scenarios"]:
        source = evidence_scenarios.get(scenario["scenario_id"])
        if not isinstance(source, dict):
            bad_scenarios.append(scenario["scenario_id"] + ":missing")
        elif scenario["evidence_digest"] != sha_value(source):
            bad_scenarios.append(scenario["scenario_id"] + ":digest")
    checks.add(
        "digest.scenarios",
        "FAIL" if bad_scenarios else "PASS",
        ", ".join(bad_scenarios)
        if bad_scenarios
        else f"{len(report['scenarios'])} scenario digests match",
    )

    source_info = evidence.get("source", {})
    if source_body is not None:
        raw = source_body.read_bytes()
        if source_info.get("body_bytes") != len(raw) or source_info.get("body_sha256") != sha_bytes(
            raw
        ):
            checks.add("digest.source_body", "FAIL", "source body bytes/digest differs")
        else:
            checks.add("digest.source_body", "PASS", source_info["body_sha256"])
    else:
        checks.add("digest.source_body", "NA", "raw source body not supplied; recorded digest only")

    now = datetime.now(UTC)
    qualified_at = timestamp(report["qualified_at"], "qualified_at")
    if not report.get("expires_at"):
        checks.add("lifecycle.expiry", "WARN", "no expires_at; allowable only for non-production")
    else:
        expires_at = timestamp(report["expires_at"], "expires_at")
        if qualified_at >= expires_at:
            checks.add("lifecycle.expiry", "FAIL", "qualified_at is not before expires_at")
        elif now >= expires_at:
            checks.add("lifecycle.expiry", "FAIL", f"report expired at {report['expires_at']}")
        else:
            checks.add("lifecycle.expiry", "PASS", f"expires {report['expires_at']}")

    image_digest = evidence.get("software", {}).get("engine_image_digest")
    image_components = [
        component
        for component in components.values()
        if component["engine_kind"] in ("vllm", "vllm_omni")
    ]
    if image_digest and len(image_components) == 1:
        if image_components[0]["image_digest"] != image_digest:
            checks.add("digest.image", "FAIL", "report image digest differs from evidence")
        else:
            checks.add("digest.image", "PASS", image_digest)
    else:
        checks.add("digest.image", "NA", "cannot map one evidence image to one vLLM component")


def check_definition(
    definition: dict[str, Any],
    manifest: dict[str, Any],
    checks: Report,
) -> None:
    recipe = definition.get("execution_recipe")
    if not isinstance(recipe, dict):
        checks.add("definition.execution_recipe", "FAIL", "missing execution_recipe")
        return
    declared = recipe.get("capacity_manifest_id")
    if not isinstance(declared, str):
        checks.add("definition.capacity_manifest_id", "FAIL", "missing")
    elif declared != manifest["manifest_id"]:
        checks.add(
            "definition.capacity_manifest_id",
            "FAIL",
            f"declares {declared}, expected {manifest['manifest_id']}",
        )
    else:
        checks.add("definition.capacity_manifest_id", "PASS", declared)
    if isinstance(declared, str) and PLACEHOLDER_ID.search(declared):
        checks.add(
            "definition.placeholder",
            "FAIL",
            f"definition declares placeholder capacity manifest {declared}",
        )
    else:
        checks.add("definition.placeholder", "PASS", "no local-test placeholder")
    definition_tier = definition.get("release_tier")
    if definition_tier is not None and definition_tier != manifest["release_tier"]:
        checks.add(
            "definition.release_tier",
            "FAIL",
            f"definition={definition_tier}, manifest={manifest['release_tier']}",
        )
    elif definition_tier is None:
        checks.add("definition.release_tier", "WARN", "definition has no release_tier")
    else:
        checks.add("definition.release_tier", "PASS", definition_tier)
    roles = recipe.get("stage_roles", [])
    manifest_roles = [stage["stage_role"] for stage in manifest["stage_graph"]]
    if set(roles) != set(manifest_roles):
        checks.add(
            "definition.stage_roles", "FAIL", f"definition={roles}, manifest={manifest_roles}"
        )
    else:
        checks.add("definition.stage_roles", "PASS", ",".join(roles))
    tasks = definition.get("tasks", [])
    missing = [task for task in manifest["supported_tasks"] if task not in tasks]
    checks.add(
        "definition.tasks",
        "FAIL" if missing else "PASS",
        "missing: " + ", ".join(missing) if missing else ",".join(manifest["supported_tasks"]),
    )
    domain_ms = definition.get("parameter_domains", {}).get("asr", {}).get("max_segment_ms")
    limit_ms = manifest["rate_and_segment_limits"]["max_segment_audio_ms"]
    if domain_ms is not None and integer(domain_ms) != integer(limit_ms):
        checks.add("definition.max_segment", "FAIL", f"definition={domain_ms}, manifest={limit_ms}")
    else:
        checks.add("definition.max_segment", "PASS", str(limit_ms))


def _vector(row: dict[str, Any], prefix: str) -> dict[str, int]:
    return {key: integer(row[prefix + key]) for key in PHYSICAL_DIMENSIONS}


def check_database(
    snapshot: dict[str, Any],
    manifest: dict[str, Any],
    report: dict[str, Any],
    manifest_path: Path,
    report_path: Path,
    config_bundle_digest: str,
    require_published: bool,
    require_digests: bool,
    checks: Report,
) -> None:
    source = snapshot.get("source", "unknown")
    if source != "live_database":
        checks.add(
            "database.source",
            "WARN",
            f"{source}; this proves the query path, not production registration or capacity",
        )
    else:
        checks.add("database.source", "PASS", "live read-only PostgreSQL transaction")

    binding = snapshot.get("binding_revision")
    if not isinstance(binding, dict):
        checks.add("database.binding_revision", "FAIL", "binding revision row not found")
        return
    if binding.get("binding_id") != report["binding"]["id"] or str(binding.get("revision")) != str(
        report["binding"]["revision"]
    ):
        checks.add("database.binding_revision", "FAIL", "row identity differs from report")
    elif binding.get("revision_id") != report["binding_revision_id"]:
        checks.add("database.binding_revision", "FAIL", "revision_id differs from report")
    else:
        checks.add("database.binding_revision", "PASS", binding["revision_id"])

    definition = binding.get("definition")
    if definition is None and isinstance(binding.get("definition_json"), str):
        definition = json.loads(binding["definition_json"])
    if not isinstance(definition, dict):
        checks.add("database.definition", "FAIL", "stored definition is not an object")
        return
    check_definition(definition, manifest, checks)

    publication = snapshot.get("publication")
    evidence = snapshot.get("evidence")
    if publication is None:
        if evidence is not None:
            checks.add("database.evidence", "FAIL", "evidence exists without publication")
        elif require_published:
            checks.add("database.publication", "FAIL", "publication row is required")
        else:
            checks.add("database.publication", "NA", "no publication row; pre-publication audit")
    else:
        publication_ok = (
            publication.get("kind") == "binding"
            and publication.get("object_id") == report["binding"]["id"]
            and str(publication.get("revision")) == str(report["binding"]["revision"])
            and publication.get("revision_id") == report["binding_revision_id"]
        )
        if not publication_ok:
            checks.add(
                "database.publication",
                "FAIL",
                "publication kind/object_id/revision/revision_id differs",
            )
        else:
            checks.add("database.publication", "PASS", publication["revision_id"])
        if evidence is None:
            checks.add("database.evidence", "FAIL", "publication exists without evidence row")
        else:
            facts_ok = (
                evidence.get("object_id") == report["binding"]["id"]
                and str(evidence.get("revision")) == str(report["binding"]["revision"])
                and evidence.get("revision_id") == report["binding_revision_id"]
                and evidence.get("capacity_manifest_id") == manifest["manifest_id"]
                and evidence.get("qualification_report_id") == report["report_id"]
            )
            checks.add(
                "database.evidence_identity",
                "PASS" if facts_ok else "FAIL",
                "IDs match" if facts_ok else "evidence IDs/revision differ",
            )
            placeholder_fields = [
                name
                for name in ("capacity_manifest_id", "qualification_report_id")
                if isinstance(evidence.get(name), str)
                and PLACEHOLDER_ID.search(evidence.get(name, ""))
            ]
            checks.add(
                "database.evidence_placeholder",
                "FAIL" if placeholder_fields else "PASS",
                "placeholder evidence ids: " + ", ".join(placeholder_fields)
                if placeholder_fields
                else "no local-test placeholder",
            )
            for field_name, path in (
                ("capacity_manifest_digest", manifest_path),
                ("qualification_report_digest", report_path),
            ):
                stored = evidence.get(field_name)
                expected = file_sha(path)
                if stored is None:
                    checks.add(
                        f"database.{field_name}",
                        "FAIL" if require_digests else "WARN",
                        "missing; bundle content is not fixed by the publication row",
                    )
                elif stored != expected:
                    checks.add(
                        f"database.{field_name}", "FAIL", f"stored={stored}, file={expected}"
                    )
                else:
                    checks.add(f"database.{field_name}", "PASS", stored)

    instances = snapshot.get("engine_instances")
    if not isinstance(instances, list) or not instances:
        checks.add("capacity.engine_instances", "FAIL", "no instance declares this manifest")
        return
    hosts = {
        row["host_node_id"]: row
        for row in snapshot.get("host_nodes", [])
        if isinstance(row, dict) and "host_node_id" in row
    }
    pools = {
        row["pool_id"]: row
        for row in snapshot.get("resource_pools", [])
        if isinstance(row, dict) and "pool_id" in row
    }
    envelope = manifest["resource_envelope"]
    vector_map = {
        "peak_": envelope["loading_peak_budget"],
        "steady_": envelope["resident_budget"],
        "per_slot_": envelope["per_slot_incremental_budget"],
    }
    for instance in instances:
        instance_id = instance.get("instance_id", "<unknown>")
        prefix = f"capacity.instance[{instance_id}]"
        identity_ok = (
            instance.get("manifest_id") == manifest["manifest_id"]
            and instance.get("binding_revision_id") == manifest["binding_revision_id"]
            and instance.get("pool_id") == manifest["pool_id"]
            and instance.get("management_mode") == manifest["management_mode"]
            and instance.get("loading_policy") == manifest["loading_policy"]
        )
        checks.add(
            f"{prefix}.identity",
            "PASS" if identity_ok else "FAIL",
            "manifest/revision/pool/mode match" if identity_ok else "identity differs",
        )
        vectors_ok = True
        for db_prefix, expected_vector in vector_map.items():
            actual = _vector(instance, db_prefix)
            if any(actual[key] != integer(expected_vector[key]) for key in PHYSICAL_DIMENSIONS):
                vectors_ok = False
        checks.add(
            f"{prefix}.vectors",
            "PASS" if vectors_ok else "FAIL",
            "peak/steady/per-slot six dimensions match"
            if vectors_ok
            else "resource vectors differ",
        )
        if integer(instance["max_slots"]) != integer(envelope["max_active_slots"]):
            checks.add(f"{prefix}.max_slots", "FAIL", "instance max_slots differs from envelope")
        else:
            checks.add(f"{prefix}.max_slots", "PASS", str(instance["max_slots"]))
        if integer(instance["realtime_reserved_slots"]) != integer(
            envelope["realtime_reserved_slots"]
        ):
            checks.add(f"{prefix}.realtime_reserved_slots", "FAIL", "reserved slots differ")
        else:
            checks.add(f"{prefix}.realtime_reserved_slots", "PASS", "match")
        state_ok = (
            instance.get("instance_state") == "ready"
            and instance.get("base_budget_kind") == "steady"
            and instance.get("stopped_at_us") is None
        )
        checks.add(
            f"{prefix}.state",
            "PASS" if state_ok else "FAIL",
            "ready/steady" if state_ok else "instance is not a live steady instance",
        )
        if instance.get("config_digest") != config_bundle_digest:
            checks.add(
                f"{prefix}.config_digest",
                "FAIL",
                "instance config_digest differs from manifest config bundle "
                f"{config_bundle_digest}",
            )
        else:
            checks.add(f"{prefix}.config_digest", "PASS", config_bundle_digest)
        if integer(instance["held_slots"]) + integer(instance["quarantined_slots"]) > integer(
            instance["max_slots"]
        ):
            checks.add(f"{prefix}.slot_accounting", "FAIL", "held + quarantined exceeds max_slots")
        else:
            checks.add(f"{prefix}.slot_accounting", "PASS", "held + quarantined <= max_slots")

        host = hosts.get(instance.get("host_node_id"))
        if host is None:
            checks.add(f"{prefix}.host", "FAIL", "host node row not found")
            continue
        host_ok = (
            host.get("topology_digest") == manifest["topology_ref"]["topology_digest"]
            and host.get("memory_architecture") == manifest["topology_ref"]["memory_architecture"]
            and host.get("node_state") == "active"
        )
        checks.add(
            f"{prefix}.host",
            "PASS" if host_ok else "FAIL",
            "topology/architecture/node state match" if host_ok else "host topology/state differs",
        )
        for key in PHYSICAL_DIMENSIONS:
            alloc = integer(host["allocatable_" + key])
            held = integer(host["held_" + key])
            quarantine = integer(host["quarantined_" + key])
            if held + quarantine > alloc:
                checks.add(
                    f"{prefix}.host_ledger[{key}]", "FAIL", "held + quarantined > allocatable"
                )
            elif held + quarantine < 0:
                checks.add(f"{prefix}.host_ledger[{key}]", "FAIL", "negative ledger")
            else:
                checks.add(
                    f"{prefix}.host_ledger[{key}]", "PASS", f"headroom={alloc - held - quarantine}"
                )
        if manifest["topology_ref"]["memory_architecture"] == "unified_memory":
            if integer(host["allocatable_unified_physical_memory_bytes"]) > integer(
                report["topology"]["total_physical_memory_bytes"]
            ):
                checks.add(f"{prefix}.host_total", "FAIL", "allocatable exceeds physical memory")
            else:
                checks.add(f"{prefix}.host_total", "PASS", "allocatable <= physical memory")
        peak = _vector(instance, "peak_")
        steady = _vector(instance, "steady_")
        per_slot = _vector(instance, "per_slot_")
        peak_held = all(integer(host["held_" + key]) >= peak[key] for key in PHYSICAL_DIMENSIONS)
        checks.add(
            f"{prefix}.host_peak_held",
            "PASS" if peak_held else "FAIL",
            "host held covers declared peak" if peak_held else "host held is below declared peak",
        )
        peak_fit = all(
            peak[key] <= integer(host["allocatable_" + key]) for key in PHYSICAL_DIMENSIONS
        )
        checks.add(
            f"{prefix}.peak_static_fit",
            "PASS" if peak_fit else "FAIL",
            "peak <= allocatable" if peak_fit else "peak exceeds allocatable",
        )
        remaining_slots = (
            integer(instance["max_slots"])
            - integer(instance["held_slots"])
            - integer(instance["quarantined_slots"])
        )
        slot_fit = all(
            per_slot[key] * remaining_slots
            <= integer(host["allocatable_" + key])
            - integer(host["held_" + key])
            - integer(host["quarantined_" + key])
            for key in PHYSICAL_DIMENSIONS
        )
        checks.add(
            f"{prefix}.admission_headroom",
            "PASS" if slot_fit else "FAIL",
            f"{remaining_slots} remaining slot(s) fit measured headroom"
            if slot_fit
            else "remaining slots exceed host headroom",
        )
        checks.add(
            f"{prefix}.base_budget",
            "PASS" if all(steady[key] <= peak[key] for key in PHYSICAL_DIMENSIONS) else "FAIL",
            "steady <= peak"
            if all(steady[key] <= peak[key] for key in PHYSICAL_DIMENSIONS)
            else "steady exceeds peak",
        )

    pool = pools.get(manifest["pool_id"])
    if pool is None:
        checks.add("capacity.resource_pool", "FAIL", "pool row not found")
    elif pool.get("service_class") != manifest["service_class"]:
        checks.add("capacity.resource_pool", "FAIL", "pool service_class differs")
    elif pool.get("pool_state") != "active":
        checks.add("capacity.resource_pool", "FAIL", f"pool state={pool.get('pool_state')}")
    else:
        checks.add("capacity.resource_pool", "PASS", "active service class matches")
    checks.add(
        "capacity.pool_quota",
        "NA",
        "resource_pools has no numeric quota column; quotas are host/workspace ledgers",
    )

    quotas = snapshot.get("workspace_quotas", [])
    if not quotas:
        checks.add("capacity.workspace_quota", "NA", "workspace quota not supplied")
    else:
        for quota in quotas:
            used = integer(quota["held_attempts"]) + integer(quota["quarantined_attempts"])
            if used > integer(quota["max_attempts"]):
                checks.add(
                    f"capacity.workspace_quota[{quota.get('workspace_id', '?')}]",
                    "FAIL",
                    "held + quarantined attempts exceed max_attempts",
                )
            else:
                checks.add(
                    f"capacity.workspace_quota[{quota.get('workspace_id', '?')}]",
                    "PASS",
                    f"attempt headroom={integer(quota['max_attempts']) - used}",
                )


def load_snapshot(path: Path) -> dict[str, Any]:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("database snapshot must be an object")
    return raw


def load_live_database(
    database_url: str,
    schema: str,
    binding_id: str,
    revision: str,
    workspace_id: str | None,
) -> dict[str, Any]:
    if SCHEMA_NAME.fullmatch(schema) is None:
        raise ValueError("invalid database schema")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise ValueError(
            "psycopg is required for --database-url; install psycopg[binary]"
        ) from None

    prefix = f'"{schema}".'
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '10s'")
        connection.execute("SET LOCAL lock_timeout = '2s'")
        binding_rows = connection.execute(
            f"SELECT binding_id, revision, revision_id, definition_json "
            f"FROM {prefix}speech_binding_revisions "
            "WHERE binding_id = %s AND revision = %s",
            (binding_id, revision),
        ).fetchall()
        binding = binding_rows[0] if len(binding_rows) == 1 else None
        if binding is not None:
            binding["definition_json"] = binding["definition_json"]
        publication_rows = connection.execute(
            f"SELECT kind, object_id, revision, revision_id, published_at_us "
            f"FROM {prefix}speech_publications "
            "WHERE kind = 'binding' AND object_id = %s AND revision = %s",
            (binding_id, revision),
        ).fetchall()
        evidence_rows = connection.execute(
            f"SELECT kind, object_id, revision, revision_id, capacity_manifest_id, "
            "qualification_report_id, evidence_source, recorded_at_us, "
            "capacity_manifest_digest, qualification_report_digest "
            f"FROM {prefix}speech_binding_publication_evidence "
            "WHERE kind = 'binding' AND object_id = %s AND revision = %s",
            (binding_id, revision),
        ).fetchall()
        instances = connection.execute(
            f"SELECT * FROM {prefix}engine_instances "
            "WHERE binding_revision_id = %s ORDER BY instance_id, instance_generation",
            (binding["revision_id"],) if binding is not None else (binding_id,),
        ).fetchall()
        host_ids = sorted({row["host_node_id"] for row in instances})
        pool_ids = sorted({row["pool_id"] for row in instances})
        hosts = (
            connection.execute(
                f"SELECT * FROM {prefix}host_nodes WHERE host_node_id = ANY(%s)", (host_ids,)
            ).fetchall()
            if host_ids
            else []
        )
        pools = (
            connection.execute(
                f"SELECT * FROM {prefix}resource_pools WHERE pool_id = ANY(%s)", (pool_ids,)
            ).fetchall()
            if pool_ids
            else []
        )
        quotas = (
            connection.execute(
                f"SELECT * FROM {prefix}workspace_quotas WHERE workspace_id = %s",
                (workspace_id,),
            ).fetchall()
            if workspace_id
            else []
        )
        return {
            "source": "live_database",
            "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "binding_revision": binding,
            "publication": publication_rows[0] if len(publication_rows) == 1 else None,
            "evidence": evidence_rows[0] if len(evidence_rows) == 1 else None,
            "engine_instances": instances,
            "host_nodes": hosts,
            "resource_pools": pools,
            "workspace_quotas": quotas,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument(
        "--expect-manifest-id",
        help="assert the manifest file carries this id (the id supplied by the release request)",
    )
    parser.add_argument(
        "--expect-report-id",
        help="assert the report file carries this id (the id supplied by the release request)",
    )
    parser.add_argument(
        "--artifact-pin",
        type=Path,
        default=ROOT / "deploy" / "engines" / "file-asr-audio" / "artifact-pin.json",
    )
    parser.add_argument(
        "--binding-definition",
        type=Path,
        default=ROOT / "deploy" / "engines" / "file-asr-audio" / "binding-definition.json",
    )
    parser.add_argument("--database-snapshot", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--schema", default="speech")
    parser.add_argument("--workspace-id")
    parser.add_argument("--source-body", type=Path)
    parser.add_argument("--require-published", action="store_true")
    parser.add_argument("--require-digests", action="store_true")
    parser.add_argument("--show-digests", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.database_snapshot) == bool(args.database_url):
        parser.error("set exactly one of --database-snapshot or --database-url")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checks = Report()
    try:
        manifest = read_json(args.manifest)
        report = read_json(args.report)
        evidence = read_json(args.evidence)
        artifact_pin = read_json(args.artifact_pin)
        binding_definition = read_json(args.binding_definition)
        if not all(
            isinstance(value, dict)
            for value in (manifest, report, evidence, artifact_pin, binding_definition)
        ):
            raise ValueError("manifest, report, evidence and config files must be JSON objects")
        if args.expect_manifest_id is not None:
            checks.add(
                "expected.manifest_id",
                "PASS" if manifest.get("manifest_id") == args.expect_manifest_id else "FAIL",
                str(manifest.get("manifest_id")),
            )
        if args.expect_report_id is not None:
            checks.add(
                "expected.report_id",
                "PASS" if report.get("report_id") == args.expect_report_id else "FAIL",
                str(report.get("report_id")),
            )
        for label, document, schema_path in (
            ("schema.manifest", manifest, CAPACITY_SCHEMA),
            ("schema.report", report, REPORT_SCHEMA),
        ):
            errors = schema_errors(document, schema_path)
            checks.add(
                label,
                "FAIL" if errors else "PASS",
                "; ".join(errors[:5]) if errors else schema_path.name,
            )
        if checks.failed:
            raise ValueError("schema validation failed")

        check_envelope(manifest, checks)
        check_placeholders(manifest, report, evidence, binding_definition, checks)
        check_report_logic(report, checks)
        check_cross_document(
            manifest,
            report,
            evidence,
            artifact_pin,
            binding_definition,
            args.manifest,
            args.report,
            args.evidence,
            args.source_body,
            checks,
        )
        if args.database_snapshot:
            snapshot = load_snapshot(args.database_snapshot)
        else:
            snapshot = load_live_database(
                args.database_url,
                args.schema,
                report["binding"]["id"],
                str(report["binding"]["revision"]),
                args.workspace_id,
            )
        check_database(
            snapshot,
            manifest,
            report,
            args.manifest,
            args.report,
            checks.digests["config_bundle_digest"],
            args.require_published,
            args.require_digests,
            checks,
        )
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        checks.add("input", "FAIL", str(exc))

    failed = checks.failed
    exit_code = 1 if failed else 0
    result = "FAIL" if failed else ("PASS" if not checks.warned else "PASS_WITH_WARNINGS")
    if args.json:
        payload = {
            "result": result,
            "exit_code": exit_code,
            "manifest": str(args.manifest),
            "report": str(args.report),
            "digests": checks.digests,
            "checks": [check.__dict__ for check in checks.checks],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for check in checks.checks:
            print(f"{check.status:<4} {check.name}: {check.detail}")
        if args.show_digests:
            print("\nDigests:")
            for name, value in sorted(checks.digests.items()):
                print(f"  {name}={value}")
        print(f"\nRESULT {result}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
