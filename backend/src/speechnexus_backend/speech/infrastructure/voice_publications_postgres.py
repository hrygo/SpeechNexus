"""One PostgreSQL transaction that publishes an immutable voice revision.

Lock order is fixed and shared by every publication path:

1. draft-level advisory lock (serializes publishes of one draft),
2. idempotency receipt lookup / insert for the caller's original key,
3. ``SELECT ... FOR UPDATE`` on the draft row,
4. evidence, Job, report Artifact and material proof reads.

The transaction performs no object I/O. The report bytes were verified once by
the recorder; here the evidence digest is compared with the Artifact row's
committed digest and the material is compared with both ``speech_assets`` and
``speech_asset_objects`` so a missing or replaced object can never be published.
"""

from __future__ import annotations

import json
import time
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.voice_capability_evidence import (
    ValidationRunFacts,
    VoiceEvidenceNotEligibleError,
    committed_validation,
)
from speechnexus_backend.speech.application.voice_publications import (
    PublicationRequest,
    VoicePublicationConflictError,
    VoicePublicationContractError,
    VoicePublicationNotEligibleError,
    VoicePublicationNotFoundError,
    VoicePublicationUnavailableError,
    build_evidence_checked_at_us,
    build_publication_view,
    build_voice_revision_document,
    require_source_eligibility,
    stable_revision_id,
    stable_voice_id,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.domain.voice_capability_evidence import (
    REUSABLE_TTS_PATH,
    VoiceMaterial,
    publication_id_for,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    EVIDENCE_TABLE,
    INTENT_TABLE,
    REVISION_TABLE,
)

_SELECT_DRAFT = (
    "SELECT draft_id, kind, status, head_revision, display_name "
    "FROM speech_voice_drafts WHERE draft_id=%s"
)
_SELECT_REVISION = (
    "SELECT kind, input_json, binding_json FROM speech_voice_draft_revisions "
    "WHERE draft_id=%s AND revision=%s"
)
_SELECT_EVIDENCE = (
    "SELECT evidence_id, draft_id, draft_revision, validation_job_id, validation_id, "
    "binding_revision_id, report_artifact_id, report_sha256, report_receipt_id, "
    "capabilities, coverage, material, evidence_digest, measured_by, measured_at_us "
    f"FROM {EVIDENCE_TABLE} WHERE validation_job_id=%s"
)
_SELECT_PUBLISHED = (
    "SELECT v.voice_id, v.revision, v.revision_id, v.document, e.validation_job_id, "
    "e.validation_id, v.published_at_us "
    f"FROM {REVISION_TABLE} v JOIN {EVIDENCE_TABLE} e ON e.evidence_id=v.evidence_id "
    "WHERE v.draft_id=%s AND v.draft_revision=%s"
)


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw, self.deadline = cursor, deadline

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise VoicePublicationUnavailableError
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


def _material(row: dict[str, Any]) -> VoiceMaterial | None:
    value = row["material"]
    if value is None:
        return None
    try:
        return VoiceMaterial.from_value(value)
    except ValueError:
        raise VoicePublicationNotEligibleError("material_invalid") from None


class PostgresVoicePublications:
    """Publish one draft revision with evidence, receipt and material reference."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Publication transactions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def publish(
        self,
        draft_id: str,
        request: PublicationRequest,
        *,
        request_digest: str,
        key_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        deadline = time.monotonic() + self._timeout
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.cursor() as raw,
            ):
                return self._publish(
                    _Cursor(raw, deadline),
                    draft_id,
                    request,
                    request_digest=request_digest,
                    key_digest=key_digest,
                )
        except (
            VoicePublicationConflictError,
            VoicePublicationNotEligibleError,
            VoicePublicationNotFoundError,
            VoicePublicationUnavailableError,
            VoicePublicationContractError,
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
            raise VoicePublicationUnavailableError from None

    def _publish(
        self,
        cursor: _Cursor,
        draft_id: str,
        request: PublicationRequest,
        *,
        request_digest: str,
        key_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:voice-publication:' || current_schema() || ':' || %s, 0))",
            (draft_id,),
        )
        if key_digest is not None:
            cursor.execute(
                f"SELECT request_digest, result FROM {INTENT_TABLE} WHERE key_digest=%s",
                (key_digest,),
            )
            receipts = cursor.fetchall()
            if receipts:
                if receipts[0]["request_digest"] != request_digest:
                    raise VoicePublicationConflictError("idempotency_conflict")
                return dict(receipts[0]["result"]), False

        cursor.execute(_SELECT_DRAFT + " FOR UPDATE", (draft_id,))
        drafts = cursor.fetchall()
        if len(drafts) != 1:
            raise VoicePublicationNotFoundError
        draft = drafts[0]

        existing = self._existing_publication(cursor, draft_id, request)
        if existing is not None:
            return existing, False

        if draft["head_revision"] != request.expected_draft_revision:
            raise VoicePublicationConflictError("stale_draft_revision")
        if draft["status"] in ("published", "preview_only"):
            raise VoicePublicationConflictError("draft_not_publishable")
        require_source_eligibility(draft["kind"])

        cursor.execute(_SELECT_REVISION, (draft_id, request.expected_draft_revision))
        revisions = cursor.fetchall()
        if len(revisions) != 1:
            raise VoicePublicationNotFoundError
        revision = revisions[0]
        draft_input = _json_object(revision["input_json"])
        binding = _json_object(revision["binding_json"])
        if draft_input is None or binding is None or set(binding) != {"id", "revision"}:
            raise VoicePublicationNotEligibleError("draft_revision_invalid")
        if draft_input.get("source_use_confirmed") is not True:
            raise VoicePublicationNotEligibleError("source_use_not_confirmed")
        reference_asset_id = draft_input.get("reference_audio_asset_id")
        if not isinstance(reference_asset_id, str):
            raise VoicePublicationNotEligibleError("clone_material_missing")

        cursor.execute(_SELECT_EVIDENCE, (request.validation_job_id,))
        evidence_rows = cursor.fetchall()
        if len(evidence_rows) != 1:
            raise VoicePublicationNotEligibleError("evidence_missing")
        evidence = evidence_rows[0]
        if evidence["draft_id"] != draft_id:
            raise VoicePublicationNotEligibleError("evidence_draft_mismatch")
        if evidence["draft_revision"] != request.expected_draft_revision:
            raise VoicePublicationNotEligibleError("evidence_revision_mismatch")
        coverage = evidence["coverage"]
        if type(coverage) is not dict or coverage.get("synthesis_path") != REUSABLE_TTS_PATH:
            raise VoicePublicationNotEligibleError("synthesis_path_not_reusable")

        cursor.execute(
            "SELECT revision_id FROM speech_binding_revisions WHERE binding_id=%s AND revision=%s",
            (binding["id"], binding["revision"]),
        )
        bindings = cursor.fetchall()
        if len(bindings) != 1 or bindings[0]["revision_id"] != evidence["binding_revision_id"]:
            raise VoicePublicationNotEligibleError("binding_revision_mismatch")

        cursor.execute(
            "SELECT state, kind, result_json, workspace_id FROM speech_jobs WHERE job_id=%s",
            (request.validation_job_id,),
        )
        jobs = cursor.fetchall()
        if len(jobs) != 1:
            raise VoicePublicationNotEligibleError("validation_job_missing")
        job = jobs[0]
        material = _material(evidence)
        if material is None:
            raise VoicePublicationNotEligibleError("clone_material_missing")
        if material.asset_id != reference_asset_id:
            raise VoicePublicationNotEligibleError("clone_material_mismatch")
        self._require_material_proof(cursor, material, job["workspace_id"])

        facts = ValidationRunFacts(
            validation_job_id=request.validation_job_id,
            job_kind=job["kind"],
            job_state=job["state"],
            result_json=job["result_json"],
            draft_id=draft_id,
            draft_revision=request.expected_draft_revision,
            draft_kind=draft["kind"],
            draft_input=draft_input,
            binding={"id": str(binding["id"]), "revision": str(binding["revision"])},
            binding_revision_id=evidence["binding_revision_id"],
            report_artifact_id=evidence["report_artifact_id"],
            report_receipt_id=evidence["report_receipt_id"],
            report_sha256=evidence["report_sha256"],
            report_bytes=0,
            source_asset=material,
        )
        try:
            validation, _ = committed_validation(facts)
        except VoiceEvidenceNotEligibleError as error:
            raise VoicePublicationNotEligibleError(str(error)) from None
        if validation.get("id") != evidence["validation_id"]:
            raise VoicePublicationNotEligibleError("evidence_validation_mismatch")
        checked_at_us = build_evidence_checked_at_us(validation)

        self._require_report_proof(cursor, evidence, job["workspace_id"])

        voice_id = stable_voice_id(draft_id)
        voice_revision = self._next_revision(cursor, voice_id)
        cursor.execute("SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp())*1000000 AS BIGINT) AS t")
        clocks = cursor.fetchall()
        if len(clocks) != 1:
            raise VoicePublicationUnavailableError
        published_at_us = int(clocks[0]["t"])
        revision_id = stable_revision_id(voice_id, voice_revision)
        display_name = request.display_name or draft["display_name"]
        document = build_voice_revision_document(
            voice_id=voice_id,
            revision=voice_revision,
            display_name=display_name,
            source_type="cloned",
            draft_id=draft_id,
            draft_revision=request.expected_draft_revision,
            material=material,
            evidence=_evidence_view(evidence),
            binding_id=str(binding["id"]),
            binding_revision=str(binding["revision"]),
            checked_at_us=checked_at_us,
            published_at_us=published_at_us,
        )
        publication = Publication(
            kind="voice",
            object_id=voice_id,
            revision=int(voice_revision),
            revision_id=revision_id,
            published_at_us=published_at_us,
        )
        if not append_publication(cursor, publication):
            raise VoicePublicationConflictError("publication_revision_exists")

        cursor.execute(
            f"INSERT INTO {REVISION_TABLE} "
            "(kind, voice_id, revision, revision_id, source_type, document, material, "
            "evidence_id, draft_id, draft_revision, published_at_us) "
            "VALUES ('voice', %s, %s, %s, 'cloned', %s::jsonb, %s::jsonb, %s, %s, %s, %s)",
            (
                voice_id,
                voice_revision,
                revision_id,
                json.dumps(document),
                json.dumps(material.as_json()),
                evidence["evidence_id"],
                draft_id,
                request.expected_draft_revision,
                published_at_us,
            ),
        )
        cursor.execute(
            "INSERT INTO speech_asset_references "
            "(reference_id, asset_id, consumer_kind, consumer_id, consumer_revision, "
            "created_at_us) VALUES (%s,%s,'voice_revision',%s,%s,%s) "
            "ON CONFLICT (asset_id, consumer_kind, consumer_id, consumer_revision) "
            "DO NOTHING",
            (
                "ref-" + revision_id,
                material.asset_id,
                voice_id,
                voice_revision,
                published_at_us,
            ),
        )
        cursor.execute(
            "UPDATE speech_voice_drafts SET status='published' WHERE draft_id=%s",
            (draft_id,),
        )
        if cursor.rowcount != 1:
            raise VoicePublicationUnavailableError

        body = build_publication_view(
            publication_id=publication_id_for(voice_id, voice_revision),
            draft_id=draft_id,
            draft_revision=request.expected_draft_revision,
            voice_id=voice_id,
            voice_revision=voice_revision,
            validation_job_id=request.validation_job_id,
            validation_id=evidence["validation_id"],
            published_at_us=published_at_us,
        )
        if key_digest is not None:
            cursor.execute(
                f"INSERT INTO {INTENT_TABLE} "
                "(key_digest, request_digest, intent, draft_id, draft_revision, "
                "voice_id, voice_revision, result) "
                "VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (key_digest) DO NOTHING",
                (
                    key_digest,
                    request_digest,
                    json.dumps(request.as_intent()),
                    draft_id,
                    request.expected_draft_revision,
                    voice_id,
                    voice_revision,
                    json.dumps(body),
                ),
            )
            if cursor.rowcount != 1:
                raise VoicePublicationConflictError("idempotency_race")
        return body, True

    def _existing_publication(
        self, cursor: _Cursor, draft_id: str, request: PublicationRequest
    ) -> dict[str, Any] | None:
        cursor.execute(_SELECT_PUBLISHED, (draft_id, request.expected_draft_revision))
        rows = cursor.fetchall()
        if not rows:
            return None
        stored = rows[0]
        if stored["validation_job_id"] != request.validation_job_id:
            raise VoicePublicationConflictError("draft_revision_already_published")
        return build_publication_view(
            publication_id=publication_id_for(stored["voice_id"], stored["revision"]),
            draft_id=draft_id,
            draft_revision=request.expected_draft_revision,
            voice_id=stored["voice_id"],
            voice_revision=stored["revision"],
            validation_job_id=stored["validation_job_id"],
            validation_id=stored["validation_id"],
            published_at_us=int(stored["published_at_us"]),
        )

    def _next_revision(self, cursor: _Cursor, voice_id: str) -> str:
        cursor.execute(
            f"SELECT COALESCE(MAX(revision::numeric), 0) AS current FROM {REVISION_TABLE} "
            "WHERE voice_id=%s",
            (voice_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise VoicePublicationUnavailableError
        return str(int(rows[0]["current"]) + 1)

    def _require_material_proof(
        self, cursor: _Cursor, material: VoiceMaterial, workspace_id: str
    ) -> None:
        cursor.execute(
            "SELECT asset_state, bytes, sha256 FROM speech_assets "
            "WHERE asset_id=%s AND workspace_id=%s",
            (material.asset_id, workspace_id),
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0]["asset_state"] != "ready"
            or int(rows[0]["bytes"]) != material.bytes
            or rows[0]["sha256"] != material.sha256
        ):
            raise VoicePublicationNotEligibleError("clone_material_unproven")
        cursor.execute(
            "SELECT 1 AS proof FROM speech_asset_objects "
            "WHERE asset_id=%s AND bytes=%s AND sha256=%s",
            (material.asset_id, material.bytes, material.sha256),
        )
        if not cursor.fetchall():
            raise VoicePublicationNotEligibleError("clone_material_unproven")

    def _require_report_proof(
        self, cursor: _Cursor, evidence: dict[str, Any], workspace_id: str
    ) -> None:
        cursor.execute(
            "SELECT artifact_state, artifact_kind, sha256, receipt_id FROM speech_artifacts "
            "WHERE workspace_id=%s AND artifact_id=%s",
            (workspace_id, evidence["report_artifact_id"]),
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0]["artifact_state"] != "ready"
            or rows[0]["artifact_kind"] != "validation_report"
            or rows[0]["receipt_id"] != evidence["report_receipt_id"]
        ):
            # The report row is gone, expired or detached: an availability
            # failure, not a recorded eligibility decision.
            raise VoicePublicationUnavailableError
        if rows[0]["sha256"] != evidence["report_sha256"]:
            # The bytes changed after measurement. The evidence still names the
            # old digest, so this run no longer qualifies the target.
            raise VoicePublicationNotEligibleError("report_digest_mismatch")


def _evidence_view(row: dict[str, Any]) -> Any:
    """Rebuild the evidence dataclass from the stored row without re-reading bytes."""
    from speechnexus_backend.speech.domain.voice_capability_evidence import (
        VoiceCapabilityEvidence,
    )

    material = None
    if row["material"] is not None:
        material = VoiceMaterial.from_value(row["material"])
    return VoiceCapabilityEvidence(
        evidence_id=row["evidence_id"],
        draft_id=row["draft_id"],
        draft_revision=row["draft_revision"],
        validation_job_id=row["validation_job_id"],
        validation_id=row["validation_id"],
        binding_revision_id=row["binding_revision_id"],
        report_artifact_id=row["report_artifact_id"],
        report_sha256=row["report_sha256"],
        report_receipt_id=row["report_receipt_id"],
        evidence_kind="validation_run",
        measured_by=row["measured_by"],
        measured_at_us=int(row["measured_at_us"]),
        capabilities=row["capabilities"],
        coverage=row["coverage"],
        material=material,
        evidence_digest=row["evidence_digest"],
    )
