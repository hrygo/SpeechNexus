"""Verify a recorded binding report bundle, without issuing deployment or voice authority.

The boundary validates canonical shapes and raw file hashes first. These checks
then compare the same decoded snapshots with the selected immutable publication.
Recorded measurements are still attestations: their truth, model bytes and the
current engine generation require separate qualification and runtime evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.publications import Publication, publication_digest


class BindingReleaseEvidenceError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid binding release evidence")


@dataclass(frozen=True, slots=True)
class ReleaseStagePin:
    stage_id: str
    model_digest: str
    engine_kind: str
    image_digest: str
    runtime_version: str


@dataclass(frozen=True, slots=True)
class VerifiedBindingRelease:
    """Content consistency facts only; never a voice or instance readiness grant."""

    binding_revision_id: str
    manifest_id: str
    report_id: str
    release_tier: str
    manifest_digest: str
    report_digest: str
    config_bundle_digest: str
    qualified_at: datetime
    expires_at: datetime | None
    stage_pins: tuple[ReleaseStagePin, ...]


def _require(condition: bool) -> None:
    if not condition:
        raise BindingReleaseEvidenceError


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None)
    return parsed.astimezone(UTC)


def verify_report_bundle(
    *,
    publication: Publication,
    recorded: BindingPublicationEvidence,
    definition: dict[str, Any],
    manifest: dict[str, Any],
    report: dict[str, Any],
    scenario_evidence: dict[str, Any],
    artifact_pin: dict[str, Any],
    now: datetime,
) -> VerifiedBindingRelease:
    """Pure decision over schema-validated private snapshots, with no I/O or mutation."""
    _require(now.tzinfo is not None and now.utcoffset() is not None)
    _require(publication.kind == "binding")
    _require(
        (recorded.object_id, recorded.revision, recorded.revision_id)
        == (publication.object_id, publication.revision, publication.revision_id)
    )
    _require(report["binding"] == publication.reference)
    _require(
        report["binding_revision_id"] == manifest["binding_revision_id"] == publication.revision_id
    )
    _require(
        manifest["manifest_id"] == report["capacity_manifest_id"] == recorded.capacity_manifest_id
    )
    qualification = manifest["qualification"]
    _require(
        qualification["qualification_report_id"]
        == report["report_id"]
        == recorded.qualification_report_id
    )
    _require(manifest["release_tier"] == report["release_tier"] == definition["release_tier"])
    recipe = definition["execution_recipe"]
    _require(recipe["capacity_manifest_id"] == manifest["manifest_id"])
    _require(set(manifest["supported_tasks"]).issubset(definition["tasks"]))
    _require(set(recipe["stage_roles"]) == {s["stage_role"] for s in manifest["stage_graph"]})
    _require(
        qualification["config_bundle_digest"]
        == publication_digest({"artifact_pin": artifact_pin, "binding_definition": definition})
    )

    qualified = _time(report["qualified_at"])
    expires = _time(report["expires_at"]) if report.get("expires_at") is not None else None
    _require(_time(qualification["qualified_at"]) == qualified)
    _require(
        (None if qualification["expires_at"] is None else _time(qualification["expires_at"]))
        == expires
    )
    _require(qualified <= now and (expires is None or (qualified < expires and now < expires)))

    scenarios = report["scenarios"]
    ids = [s["scenario_id"] for s in scenarios]
    suite = report["test_suite"]["scenario_ids"]
    _require(len(ids) == len(set(ids)) == len(suite) == len(set(suite)))
    _require(set(ids) == set(suite))
    required = [s for s in scenarios if s["required"]]
    _require(bool(required) and all(s["status"] == "passed" for s in required))
    _require(report["required_scenarios_passed"] is True and report["overall_status"] == "pass")
    _require(all(isinstance(s, str) and bool(s.strip()) for s in report["limitations"]))
    if any(s["status"] != "passed" for s in scenarios) or report["release_tier"] != "production":
        _require(bool(report["limitations"]))
    sources = scenario_evidence["scenarios"]
    _require(isinstance(sources, dict) and set(sources) == set(ids))
    for scenario in scenarios:
        source = sources[scenario["scenario_id"]]
        _require(
            isinstance(source, dict) and scenario["evidence_digest"] == publication_digest(source)
        )

    topology = report["topology"]
    descriptor = {
        key: topology[key]
        for key in (
            "host_node_id",
            "cpu_arch",
            "hardware_class",
            "memory_architecture",
            "total_physical_memory_bytes",
        )
    }
    _require(manifest["topology_ref"]["topology_digest"] == publication_digest(descriptor))
    _require(manifest["topology_ref"]["memory_architecture"] == topology["memory_architecture"])
    stages = manifest["stage_graph"]
    _require(len(stages) == len({s["stage_id"] for s in stages}))
    components = report["software"]["engine_components"]
    _require(set(components) == {s["stage_id"] for s in stages})
    for stage in stages:
        _require(components[stage["stage_id"]]["engine_kind"] == stage["engine_kind"])
    # A bundle may include auxiliary models; the supplied primary model pin
    # must at least occur in the immutable stage graph. Other artifact bytes
    # remain the responsibility of the release qualification producer.
    _require(
        any(s["model_revision_digest"] == publication_digest(artifact_pin["model"]) for s in stages)
    )
    _check_envelope(manifest)
    if recorded.capacity_manifest_digest is None or recorded.qualification_report_digest is None:
        raise BindingReleaseEvidenceError
    return VerifiedBindingRelease(
        publication.revision_id,
        manifest["manifest_id"],
        report["report_id"],
        report["release_tier"],
        recorded.capacity_manifest_digest,
        recorded.qualification_report_digest,
        qualification["config_bundle_digest"],
        qualified,
        expires,
        tuple(
            ReleaseStagePin(
                s["stage_id"],
                s["model_revision_digest"],
                s["engine_kind"],
                components[s["stage_id"]]["image_digest"],
                components[s["stage_id"]]["runtime_version"],
            )
            for s in stages
        ),
    )


def _check_envelope(manifest: dict[str, Any]) -> None:
    stages, envelope = manifest["stage_graph"], manifest["resource_envelope"]
    for budget in ("resident_budget", "loading_peak_budget", "per_slot_incremental_budget"):
        actual = envelope[budget]
        for key in actual:
            values = [s[budget][key] for s in stages]
            _require(all(type(v) is int for v in values) and type(actual[key]) is int)
            expected = max(values) if key == "engine_active_slots" else sum(values)
            _require(actual[key] == expected)
    maximum = min(s["max_active_slots"] for s in stages)
    _require(
        envelope["max_active_slots"] == maximum and envelope["realtime_reserved_slots"] <= maximum
    )
