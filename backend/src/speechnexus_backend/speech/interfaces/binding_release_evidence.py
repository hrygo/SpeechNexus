"""Bounded, offline ingestion of already-recorded release documents; no filesystem paths."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from speechnexus_contracts.release_evidence import parse

from speechnexus_backend.speech.application.binding_release_evidence import (
    BindingReleaseEvidenceError,
    VerifiedBindingRelease,
    verify_report_bundle,
)
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.publications import Publication

MAX_DOCUMENT_BYTES = 1_048_576


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BindingReleaseEvidenceError
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise BindingReleaseEvidenceError


def _finite(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise BindingReleaseEvidenceError
    return parsed


def _document(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_DOCUMENT_BYTES:
        raise BindingReleaseEvidenceError
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique,
        parse_constant=_nonfinite,
        parse_float=_finite,
    )
    if not isinstance(value, dict):
        raise BindingReleaseEvidenceError
    return value


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def verify_binding_release_evidence(
    *,
    publication: Publication,
    recorded_evidence: BindingPublicationEvidence,
    binding_definition: bytes,
    artifact_pin: bytes,
    manifest: bytes,
    report: bytes,
    scenario_evidence: bytes,
    now: datetime,
) -> VerifiedBindingRelease:
    """Validate same-byte digests and canonical schemas, then cross-document rules.

    The caller obtains publication/evidence/definition from authority, not the
    uploader. This does not authenticate their issuer, reproduce measurements,
    qualify a speaker or establish the current runtime generation.
    """
    try:
        # Parse each bounded immutable bytes object once; hashing and validation
        # cannot observe different filesystem versions or fetch remote refs.
        m, r = _document(manifest), _document(report)
        definition, pin, sources = (
            _document(binding_definition),
            _document(artifact_pin),
            _document(scenario_evidence),
        )
        if recorded_evidence.capacity_manifest_digest != _digest(
            manifest
        ) or recorded_evidence.qualification_report_digest != _digest(report):
            raise BindingReleaseEvidenceError
        parse("CapacityManifest", m)
        parse("QualificationReport", r)
        if r["evidence_digest"] != _digest(scenario_evidence):
            raise BindingReleaseEvidenceError
        return verify_report_bundle(
            publication=publication,
            recorded=recorded_evidence,
            definition=definition,
            manifest=m,
            report=r,
            scenario_evidence=sources,
            artifact_pin=pin,
            now=now,
        )
    except ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError:
        raise BindingReleaseEvidenceError from None


__all__ = ["BindingReleaseEvidenceError", "verify_binding_release_evidence"]
