"""One bounded transaction that turns a committed validation run into evidence."""

from __future__ import annotations

import json
import time
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.voice_capability_evidence import (
    ValidationRunFacts,
    VoiceEvidenceConflictError,
    VoiceEvidenceNotEligibleError,
    VoiceEvidenceUnavailableError,
    VoiceReportUnavailableError,
    build_evidence,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import (
    VoiceCapabilityEvidence,
    VoiceMaterial,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.voice_publication_schema import EVIDENCE_TABLE


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw, self.deadline = cursor, deadline

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise VoiceEvidenceUnavailableError
        self.raw.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self.raw.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.raw.fetchall()


def _json_object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return None
    return value if type(value) is dict else None


def _material(row: dict[str, Any]) -> VoiceMaterial:
    return VoiceMaterial(
        asset_id=row["asset_id"],
        sha256=row["sha256"],
        bytes=int(row["bytes"]),
        media_type=row["media_type"],
    )


class PostgresVoiceEvidenceRecorder:
    """Writes one append-only evidence row for a succeeded validation Job."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Evidence transactions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def record(
        self, validation_job_id: str, report: bytes, measured_by: str
    ) -> tuple[VoiceCapabilityEvidence, bool]:
        deadline = time.monotonic() + self._timeout
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.cursor() as raw,
            ):
                evidence, created = self._record(
                    _Cursor(raw, deadline),
                    validation_job_id,
                    report,
                    measured_by,
                )
            return evidence, created
        except (
            VoiceEvidenceNotEligibleError,
            VoiceEvidenceConflictError,
            VoiceReportUnavailableError,
            VoiceEvidenceUnavailableError,
        ):
            raise
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise VoiceEvidenceUnavailableError from None

    def _record(
        self,
        cursor: _Cursor,
        validation_job_id: str,
        report: bytes,
        measured_by: str,
    ) -> tuple[VoiceCapabilityEvidence, bool]:
        cursor.execute(
            "SELECT job_id, kind, state, result_json, workspace_id FROM speech_jobs "
            "WHERE job_id=%s",
            (validation_job_id,),
        )
        jobs = cursor.fetchall()
        if len(jobs) != 1:
            raise VoiceReportUnavailableError
        job = jobs[0]
        result = _json_object(job["result_json"])
        if (
            job["kind"] != "voice_validation"
            or result is None
            or result.get("kind") != "voice_validation"
        ):
            raise VoiceEvidenceNotEligibleError("validation_job_not_succeeded")
        validation = result.get("validation")
        if type(validation) is not dict:
            raise VoiceEvidenceNotEligibleError("validation_result_invalid")
        draft_id = validation.get("draft_id")
        draft_revision = validation.get("draft_revision")
        if not isinstance(draft_id, str) or not isinstance(draft_revision, str):
            raise VoiceEvidenceNotEligibleError("validation_result_invalid")
        artifact_id = result.get("validation_report_artifact_id")
        receipt_id = result.get("receipt_id")
        if not isinstance(artifact_id, str) or not isinstance(receipt_id, str):
            raise VoiceEvidenceNotEligibleError("validation_report_mismatch")

        cursor.execute(
            "SELECT kind, input_json, binding_json FROM speech_voice_draft_revisions "
            "WHERE draft_id=%s AND revision=%s",
            (draft_id, draft_revision),
        )
        revisions = cursor.fetchall()
        if len(revisions) != 1:
            raise VoiceEvidenceNotEligibleError("draft_revision_missing")
        revision = revisions[0]
        draft_input = _json_object(revision["input_json"])
        binding = _json_object(revision["binding_json"])
        if draft_input is None or binding is None or set(binding) != {"id", "revision"}:
            raise VoiceEvidenceNotEligibleError("draft_revision_invalid")

        cursor.execute(
            "SELECT revision_id FROM speech_binding_revisions WHERE binding_id=%s AND revision=%s",
            (binding["id"], binding["revision"]),
        )
        bindings = cursor.fetchall()
        if len(bindings) != 1:
            raise VoiceEvidenceNotEligibleError("binding_revision_missing")
        binding_revision_id = bindings[0]["revision_id"]

        cursor.execute(
            "SELECT artifact_state, artifact_kind, bytes, sha256, receipt_id "
            "FROM speech_artifacts WHERE workspace_id=%s AND artifact_id=%s",
            (job["workspace_id"], artifact_id),
        )
        artifacts = cursor.fetchall()
        if len(artifacts) != 1:
            raise VoiceReportUnavailableError
        artifact = artifacts[0]
        if (
            artifact["artifact_state"] != "ready"
            or artifact["artifact_kind"] != "validation_report"
            or artifact["receipt_id"] != receipt_id
        ):
            raise VoiceReportUnavailableError

        source_asset: VoiceMaterial | None = None
        if revision["kind"] == "clone":
            asset_id = draft_input.get("reference_audio_asset_id")
            if not isinstance(asset_id, str):
                raise VoiceEvidenceNotEligibleError("clone_material_missing")
            cursor.execute(
                "SELECT asset_id, asset_state, bytes, sha256, media_type FROM speech_assets "
                "WHERE asset_id=%s AND workspace_id=%s",
                (asset_id, job["workspace_id"]),
            )
            assets = cursor.fetchall()
            if len(assets) != 1 or assets[0]["asset_state"] != "ready":
                raise VoiceEvidenceNotEligibleError("clone_material_missing")
            source_asset = _material(assets[0])
            cursor.execute(
                "SELECT 1 AS proof FROM speech_asset_objects "
                "WHERE asset_id=%s AND bytes=%s AND sha256=%s",
                (source_asset.asset_id, source_asset.bytes, source_asset.sha256),
            )
            if not cursor.fetchall():
                raise VoiceEvidenceNotEligibleError("clone_material_unproven")

        cursor.execute("SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp())*1000000 AS BIGINT) AS t")
        clocks = cursor.fetchall()
        if len(clocks) != 1:
            raise VoiceEvidenceUnavailableError
        measured_at_us = int(clocks[0]["t"])

        facts = ValidationRunFacts(
            validation_job_id=validation_job_id,
            job_kind=job["kind"],
            job_state=job["state"],
            result_json=job["result_json"],
            draft_id=draft_id,
            draft_revision=draft_revision,
            draft_kind=revision["kind"],
            draft_input=draft_input,
            binding={
                "id": str(binding["id"]),
                "revision": str(binding["revision"]),
            },
            binding_revision_id=binding_revision_id,
            report_artifact_id=artifact_id,
            report_receipt_id=receipt_id,
            report_sha256=artifact["sha256"],
            report_bytes=int(artifact["bytes"]),
            source_asset=source_asset,
        )
        evidence = build_evidence(
            facts, report, measured_by=measured_by, measured_at_us=measured_at_us
        )
        row = evidence.as_row()
        cursor.execute(
            "INSERT INTO speech_voice_capability_evidence "
            "(evidence_id, draft_id, draft_revision, validation_job_id, validation_id, "
            "binding_revision_id, report_artifact_id, report_sha256, report_receipt_id, "
            "evidence_kind, measured_by, measured_at_us, capabilities, coverage, material, "
            "evidence_digest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
            "%s::jsonb,%s::jsonb,%s) ON CONFLICT (evidence_id) DO NOTHING",
            (
                row["evidence_id"],
                row["draft_id"],
                row["draft_revision"],
                row["validation_job_id"],
                row["validation_id"],
                row["binding_revision_id"],
                row["report_artifact_id"],
                row["report_sha256"],
                row["report_receipt_id"],
                row["evidence_kind"],
                row["measured_by"],
                row["measured_at_us"],
                json.dumps(row["capabilities"]),
                json.dumps(row["coverage"]),
                None if row["material"] is None else json.dumps(row["material"]),
                row["evidence_digest"],
            ),
        )
        if cursor.rowcount == 1:
            return evidence, True
        cursor.execute(
            "SELECT draft_id, draft_revision, validation_job_id, validation_id, "
            "binding_revision_id, report_artifact_id, report_sha256, report_receipt_id, "
            "evidence_kind, measured_by, measured_at_us, capabilities, coverage, material, "
            f"evidence_digest FROM {EVIDENCE_TABLE} WHERE evidence_id=%s",
            (evidence.evidence_id,),
        )
        stored = cursor.fetchall()
        if len(stored) != 1 or stored[0]["evidence_digest"] != evidence.evidence_digest:
            raise VoiceEvidenceConflictError(
                "validation job already records different capability evidence"
            )
        row = stored[0]
        return (
            VoiceCapabilityEvidence(
                evidence_id=evidence.evidence_id,
                draft_id=row["draft_id"],
                draft_revision=row["draft_revision"],
                validation_job_id=row["validation_job_id"],
                validation_id=row["validation_id"],
                binding_revision_id=row["binding_revision_id"],
                report_artifact_id=row["report_artifact_id"],
                report_sha256=row["report_sha256"],
                report_receipt_id=row["report_receipt_id"],
                evidence_kind=row["evidence_kind"],
                measured_by=row["measured_by"],
                measured_at_us=int(row["measured_at_us"]),
                capabilities=dict(row["capabilities"]),
                coverage=dict(row["coverage"]),
                material=None
                if row["material"] is None
                else VoiceMaterial(
                    asset_id=row["material"]["asset_id"],
                    sha256=row["material"]["sha256"],
                    bytes=int(row["material"]["bytes"]),
                    media_type=row["material"]["media_type"],
                ),
                evidence_digest=row["evidence_digest"],
            ),
            False,
        )
