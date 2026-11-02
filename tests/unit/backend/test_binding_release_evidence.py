"""Synthetic report mutations must not become publication authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.interfaces.binding_release_evidence import (
    BindingReleaseEvidenceError,
    verify_binding_release_evidence,
)

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "deploy/engines/file-asr-audio/evidence/gb10-20261027"
NOW = datetime(2026, 11, 2, tzinfo=UTC)


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> str:
    return digest(rfc8785.dumps(value))


def packet() -> dict[str, Any]:
    # Reuse schema-complete examples only as synthetic inputs. Changing these
    # scenarios does not update or qualify the retained historical report.
    manifest = json.loads((SAMPLE / "capacity-manifest.json").read_text())
    report = json.loads((SAMPLE / "qualification-report.json").read_text())
    definition = json.loads((SAMPLE / "binding-definition.candidate.json").read_text())
    pin = json.loads((ROOT / "deploy/engines/file-asr-audio/artifact-pin.json").read_text())
    sources: dict[str, Any] = {}
    for scenario in report["scenarios"]:
        scenario["status"] = "passed"
        sources[scenario["scenario_id"]] = {"synthetic": True, "case": scenario["scenario_id"]}
        scenario["evidence_digest"] = canonical(sources[scenario["scenario_id"]])
    report["required_scenarios_passed"] = True
    report["overall_status"] = "pass"
    manifest["qualification"]["config_bundle_digest"] = canonical(
        {"artifact_pin": pin, "binding_definition": definition}
    )
    return {
        "manifest": manifest,
        "report": report,
        "definition": definition,
        "pin": pin,
        "evidence": {"scenarios": sources},
    }


def verify(
    value: dict[str, Any], *, encoded_overrides: dict[str, bytes] | None = None, **overrides: Any
) -> Any:
    raw = {name: json.dumps(doc).encode() for name, doc in value.items()}
    report = value["report"]
    report["evidence_digest"] = digest(raw["evidence"])
    raw["report"] = json.dumps(report).encode()
    raw.update(encoded_overrides or {})
    publication = Publication(
        "binding",
        report["binding"]["id"],
        int(report["binding"]["revision"]),
        report["binding_revision_id"],
        1,
    )
    authority = BindingPublicationEvidence(
        publication.object_id,
        publication.revision,
        publication.revision_id,
        value["manifest"]["manifest_id"],
        report["report_id"],
        "operator_cli",
        1,
        digest(raw["manifest"]),
        digest(raw["report"]),
    )
    arguments: dict[str, Any] = {
        "publication": publication,
        "recorded_evidence": authority,
        "binding_definition": raw["definition"],
        "artifact_pin": raw["pin"],
        "manifest": raw["manifest"],
        "report": raw["report"],
        "scenario_evidence": raw["evidence"],
        "now": NOW,
    }
    arguments.update(overrides)
    return verify_binding_release_evidence(**arguments)


def test_matching_bundle_returns_only_fixed_consistency_facts() -> None:
    value = packet()
    result = verify(value)
    assert result.binding_revision_id == value["report"]["binding_revision_id"]
    assert result.manifest_id == value["manifest"]["manifest_id"]
    assert result.report_id == value["report"]["report_id"]
    assert result.release_tier == "experimental"
    assert result.stage_pins[0].image_digest.startswith("sha256:")


@pytest.mark.parametrize(
    "mutation",
    [
        "required-skipped",
        "required-failed",
        "no-required",
        "false-summary",
        "duplicate-scenario",
        "missing-scenario",
        "duplicate-stage",
        "wrong-engine",
        "wrong-binding",
        "wrong-report",
        "wrong-manifest",
        "wrong-tier",
        "wrong-config",
        "wrong-topology",
        "wrong-envelope",
        "missing-source",
        "wrong-source",
        "future",
        "expired",
        "optional-without-limitation",
    ],
)
def test_inconsistent_or_unqualified_bundle_is_rejected(mutation: str) -> None:
    value = packet()
    m, r = value["manifest"], value["report"]
    if mutation.startswith("required-"):
        next(s for s in r["scenarios"] if s["required"])["status"] = mutation.split("-")[1]
    elif mutation == "no-required":
        for scenario in r["scenarios"]:
            scenario["required"] = False
    elif mutation == "false-summary":
        r["required_scenarios_passed"] = False
    elif mutation == "duplicate-scenario":
        r["scenarios"].append(r["scenarios"][0])
    elif mutation == "missing-scenario":
        r["test_suite"]["scenario_ids"].pop()
    elif mutation == "duplicate-stage":
        m["stage_graph"].append(m["stage_graph"][0])
    elif mutation == "wrong-engine":
        next(iter(r["software"]["engine_components"].values()))["engine_kind"] = "llama_cpp"
    elif mutation == "wrong-binding":
        m["binding_revision_id"] = "binding-other"
    elif mutation == "wrong-report":
        m["qualification"]["qualification_report_id"] = "report-other"
    elif mutation == "wrong-manifest":
        r["capacity_manifest_id"] = "manifest-other"
    elif mutation == "wrong-tier":
        r["release_tier"] = "best_effort"
    elif mutation == "wrong-config":
        m["qualification"]["config_bundle_digest"] = "sha256:" + "0" * 64
    elif mutation == "wrong-topology":
        r["topology"]["host_node_id"] = "node-other"
    elif mutation == "wrong-envelope":
        m["resource_envelope"]["resident_budget"]["cpu_decode_millicores"] += 1
    elif mutation == "missing-source":
        value["evidence"]["scenarios"].pop(r["scenarios"][0]["scenario_id"])
    elif mutation == "wrong-source":
        value["evidence"]["scenarios"][r["scenarios"][0]["scenario_id"]]["synthetic"] = False
    elif mutation in ("future", "expired"):
        field = "qualified_at" if mutation == "future" else "expires_at"
        timestamp = "2026-11-03T00:00:00Z" if mutation == "future" else "2026-11-01T00:00:00Z"
        r[field] = m["qualification"][field] = timestamp
    elif mutation == "optional-without-limitation":
        r["scenarios"][0].update(required=False, status="skipped")
        r["limitations"] = []
    with pytest.raises(BindingReleaseEvidenceError, match=r"^Invalid binding release evidence$"):
        verify(value)


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b"[]", b"\xff", b"{" * 1_048_577]
)
def test_malformed_private_input_has_a_bounded_safe_error(raw: bytes) -> None:
    with pytest.raises(BindingReleaseEvidenceError, match=r"^Invalid binding release evidence$"):
        verify(packet(), manifest=raw)


def test_missing_or_different_recorded_digest_cannot_be_replaced_by_input_claim() -> None:
    value = packet()
    report = value["report"]
    authority = BindingPublicationEvidence(
        report["binding"]["id"],
        int(report["binding"]["revision"]),
        report["binding_revision_id"],
        value["manifest"]["manifest_id"],
        report["report_id"],
        "operator_cli",
        1,
    )
    for candidate in (
        authority,
        replace(
            authority,
            capacity_manifest_digest="sha256:" + "0" * 64,
            qualification_report_digest="sha256:" + "1" * 64,
        ),
    ):
        with pytest.raises(BindingReleaseEvidenceError):
            verify(value, recorded_evidence=candidate)


def test_exponent_overflow_in_a_report_metric_is_not_a_finite_measurement() -> None:
    value = packet()
    value["report"]["scenarios"][0]["metrics"] = [
        {"name": "latency", "value": "__overflow__", "unit": "ms"}
    ]
    value["report"]["evidence_digest"] = digest(json.dumps(value["evidence"]).encode())
    raw = json.dumps(value["report"]).replace('"__overflow__"', "1e999").encode()
    with pytest.raises(BindingReleaseEvidenceError):
        verify(value, encoded_overrides={"report": raw})
