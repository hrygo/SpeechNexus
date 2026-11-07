"""Read the frozen draft/generation facts a validation report must restate."""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from speechnexus_backend.speech.application.voice_validation_report import (
    MAX_PROBE_TEXT,
    MAX_REFERENCE_TEXT,
    ValidationRunFacts,
)
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchJob,
    DispatchProtocolError,
    DispatchUnavailableError,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import VoiceMaterial
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class PostgresValidationRunFacts:
    """One bounded read-only query; never infers a fact the rows do not store."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Validation fact reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def read(self, job: DispatchJob) -> ValidationRunFacts:
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                with connection.cursor(row_factory=dict_row) as cursor:
                    # The frozen public intent is authoritative, not the dispatch
                    # document: an admitted Job carries the complete business
                    # command, while the intent still names the validation facts.
                    intent_row = cursor.execute(
                        "SELECT request_document FROM speech_job_intents WHERE job_id=%s",
                        (job.job_id,),
                    ).fetchone()
                    if intent_row is None:
                        raise DispatchProtocolError("Validation Job intent is not readable")
                    intent = _document(intent_row["request_document"], "Validation Job request")
                    if intent.get("kind") != "voice_validation":
                        raise DispatchProtocolError(
                            "Validation Job request kind disagrees with the Job kind"
                        )
                    request_input = intent.get("input")
                    if not isinstance(request_input, dict):
                        raise DispatchProtocolError("Validation Job request has no input object")
                    draft_id = request_input.get("voice_draft_id")
                    draft_revision = request_input.get("draft_revision")
                    validation_set_revision = request_input.get("validation_set_revision")
                    if (
                        not isinstance(draft_id, str)
                        or not isinstance(draft_revision, str)
                        or not isinstance(validation_set_revision, str)
                    ):
                        raise DispatchProtocolError(
                            "Validation Job request target is outside the contract"
                        )
                    revision = cursor.execute(
                        "SELECT r.kind, r.input_json, r.binding_json, j.workspace_id, "
                        "i.request_document AS generation_request "
                        "FROM speech_voice_draft_revisions AS r "
                        "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
                        "JOIN speech_job_intents AS i ON i.job_id = r.generation_job_id "
                        "WHERE r.draft_id=%s AND r.revision=%s",
                        (draft_id, draft_revision),
                    ).fetchone()
                    if revision is None or revision["workspace_id"] != job.workspace_id:
                        raise DispatchProtocolError("Validation draft revision is not readable")
                    if revision["kind"] != "clone":
                        # Design drafts stay outside the admitted validation lane
                        # in this slice; a stored Job for one is invalid.
                        raise DispatchProtocolError("Validation draft revision is not a clone")
                    draft_input = _document(revision["input_json"], "Draft input")
                    draft_binding = _document(revision["binding_json"], "Draft binding")
                    generation = _document(revision["generation_request"], "Generation Job request")
                    asset_id = draft_input.get("reference_audio_asset_id")
                    if not isinstance(asset_id, str):
                        raise DispatchProtocolError("Validation draft has no reference asset")
                    asset = cursor.execute(
                        "SELECT asset_state, purpose, sha256, bytes, media_type, "
                        "sample_rate_hz, channels FROM speech_assets "
                        "WHERE workspace_id=%s AND asset_id=%s",
                        (job.workspace_id, asset_id),
                    ).fetchone()
        except psycopg.Error:
            raise DispatchUnavailableError("Validation facts are unavailable") from None

        if asset is None:
            raise DispatchProtocolError("Validation reference asset is not readable")
        if asset["asset_state"] != "ready" or asset["purpose"] != "voice_reference":
            raise DispatchProtocolError("Validation reference asset is not ready")
        media_type = asset["media_type"]
        if (
            media_type != "audio/wav"
            or int(asset["sample_rate_hz"]) not in (16_000, 24_000, 48_000)
            or not 1 <= int(asset["channels"]) <= 8
        ):
            raise DispatchProtocolError("Validation reference asset is outside the contract")

        generation_input = generation.get("input")
        if not isinstance(generation_input, dict):
            raise DispatchProtocolError("Generation Job request has no input object")
        preview_text = generation_input.get("preview_text")
        if not isinstance(preview_text, str) or not 1 <= len(preview_text) <= MAX_PROBE_TEXT:
            # Without the exact generation text, "new text reuse" is unprovable.
            raise DispatchProtocolError("Generation Job has no frozen preview text")

        binding_id = draft_binding.get("id")
        binding_revision = draft_binding.get("revision")
        if not isinstance(binding_id, str) or not isinstance(binding_revision, str):
            raise DispatchProtocolError("Draft binding reference is outside the contract")
        reference_text = draft_input.get("reference_text")
        if reference_text is not None and (
            not isinstance(reference_text, str)
            or not 1 <= len(reference_text) <= MAX_REFERENCE_TEXT
        ):
            raise DispatchProtocolError("Draft reference text is outside the contract")
        return ValidationRunFacts(
            draft_id=draft_id,
            draft_revision=draft_revision,
            validation_set_revision=validation_set_revision,
            binding_id=binding_id,
            binding_revision=binding_revision,
            generation_preview_text=preview_text,
            material=VoiceMaterial(
                asset_id,
                str(asset["sha256"]),
                int(asset["bytes"]),
                media_type,
            ),
            reference_text=reference_text,
        )


def _document(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise DispatchProtocolError(f"{name} is not a stored document")
    try:
        document = json.loads(value)
    except ValueError, TypeError, RecursionError:
        raise DispatchProtocolError(f"{name} is not interpretable") from None
    if not isinstance(document, dict):
        raise DispatchProtocolError(f"{name} is not an object")
    return document


__all__ = ["PostgresValidationRunFacts"]
