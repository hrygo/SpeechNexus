"""Complete public Job admission built from frozen binding and request facts.

The public contract deliberately keeps only the caller's intent. This module is
the one producer that turns that intent into the complete effective command the
execution gateway consumes, using only facts frozen in the same transaction:
the binding revision definition, its execution defaults, the published voice or
pronunciation revision, the ready runtime plan and the stored asset row. It
never reads deployment settings for a value the binding must declare.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

from speechnexus_backend.speech.application.transcription import AdmittedExecution, StagedAudio
from speechnexus_backend.speech.application.voice_validation_report import (
    VALIDATION_PROBE_LANGUAGE,
)
from speechnexus_backend.speech.domain.admission import (
    CAPACITY_REJECTION_CODES,
    PUBLIC_ADMISSION_CODES,
    AdmissionIdentity,
    AdmissionPool,
    AdmissionQuota,
    AdmissionRequest,
    AdmissionRuntime,
    AdmitRecipe,
    ApprovedAdmissionRecipe,
    ApprovedStage,
    OriginalBusinessCommand,
    Refused,
    StageAllocation,
    decide_create_admission,
)
from speechnexus_backend.speech.domain.job_admission_readiness import (
    DEFAULT_READINESS_WARNING_SECONDS,
    READINESS_INVALID,
    READINESS_MISSING,
    AdmissionReadiness,
    AdmissionReadinessCause,
    ReadinessInstanceReport,
    admitted_readiness,
    cause_for_refusal,
    refused_readiness,
)
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from speechnexus_backend.speech.domain.public_jobs import (
    NewPublicJob,
    canonical_digest,
    identifier,
)
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeHost,
    RuntimeRejectedError,
)
from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat, SpeechPlan
from speechnexus_backend.speech.domain.transcription import TranscriptFormat
from speechnexus_backend.speech.domain.voice_capability_evidence import VoiceMaterial
from speechnexus_backend.speech.infrastructure.admission_codec import (
    PreparedAdmission,
    business_command_from_value,
    prepare_admission,
)
from speechnexus_backend.speech.infrastructure.admission_sql import (
    _pool as _admission_pool_row,
)
from speechnexus_backend.speech.infrastructure.admission_sql import (
    _quota as _admission_quota_row,
)
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import RETIREMENT_TABLE
from speechnexus_backend.speech.infrastructure.qualification_source_evidence import (
    STATE_EVIDENCED,
    qualification_source_state_sql,
    source_refusal_detail,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import decode_record
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    decode_observation,
    validate_stored_readiness,
)
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    _head,
    _host,
    _record,
)
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    _instance as _runtime_instance,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import build_asr_submit
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit_from_parameters
from speechnexus_backend.speech.infrastructure.voice_validation_submit import (
    VOICE_VALIDATION_TIMEOUT_SECONDS,
    build_voice_validation_submit,
    validation_probe_text,
)

# The public answer for every admission refusal lives with the vocabulary it maps
# (`domain.admission.PUBLIC_ADMISSION_CODES`), so the mapping, the accepted public
# codes and the shipped error table cannot drift apart (#828 review M1). The public
# code states the recovery the caller needs; ``job_unavailable`` is reserved for a
# store that really refused the read or the write.
_PUBLIC_CODES: dict[str, str] = PUBLIC_ADMISSION_CODES
_ASR_REQUIRED = (
    "language",
    "turn_detection",
    "finalization",
    "preview_interval_ms",
    "max_segment_ms",
    "rollback_tokens",
    "final_deadline_ms",
    "auxiliary_deadline_ms",
    "alignment",
    "diarization",
    "vocabulary_set_revision",
    "temperature",
    "timeout_seconds",
)
_TTS_REQUIRED = (
    "language",
    "text_commit_mode",
    "audio_delivery",
    "speed",
    "instructions",
    "pronunciation_revision",
    "seed",
    "planner",
    "sample_rate_hz",
    "max_output_audio_ms",
    "timeout_seconds",
)


class PublicJobAdmissionRejectedError(Exception):
    """A create must be refused with one stable public error code.

    ``detail`` names the concrete missing item when the class alone cannot (no
    registered engine instance, an unpublished binding). It never names a DSN or
    credential value and never changes the public code (#877).
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PublicJobAdmission:
    prepared: PreparedAdmission
    lease_id: str
    deadline_at_us: int


class _Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Any: ...
    def fetchall(self) -> list[dict[str, Any]]: ...


def _one(cursor: _Cursor, query: str, parameters: tuple[object, ...]) -> dict[str, Any]:
    cursor.execute(query, parameters)
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return rows[0]


def _intent(job: NewPublicJob) -> dict[str, Any]:
    try:
        value = json.loads(job.request_document)
    except ValueError, TypeError, RecursionError:
        raise PublicJobAdmissionRejectedError("job_unavailable") from None
    if not isinstance(value, dict):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return value


def _execution_block(definition: dict[str, Any], kind: str) -> dict[str, Any]:
    defaults = definition.get("execution_defaults")
    if not isinstance(defaults, dict):
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    block = defaults.get(kind)
    if not isinstance(block, dict):
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    required = _ASR_REQUIRED if kind == "asr" else _TTS_REQUIRED
    for name in required:
        if name not in block:
            raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    return block


def _binding(cursor: _Cursor, model_id: str, revision: str) -> tuple[str, int, dict[str, Any]]:
    row = _one(
        cursor,
        "SELECT p.object_id, p.revision, p.revision_id, r.definition_json "
        "FROM speech_publications AS p "
        "JOIN speech_binding_revisions AS r ON r.binding_id=p.object_id "
        "AND r.revision=p.revision AND r.revision_id=p.revision_id "
        "WHERE p.kind='binding' AND p.object_id=%s AND p.revision=%s "
        "AND NOT EXISTS ("
        f"SELECT 1 FROM {RETIREMENT_TABLE} AS retired WHERE retired.kind=p.kind "
        "AND retired.object_id=p.object_id AND retired.revision=p.revision "
        "AND retired.revision_id=p.revision_id)",
        (model_id, revision),
    )
    try:
        definition = json.loads(row["definition_json"])
        revision_id = row["revision_id"]
        number = int(row["revision"])
    except ValueError, TypeError, KeyError:
        raise PublicJobAdmissionRejectedError("job_unavailable") from None
    if not isinstance(definition, dict):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return revision_id, number, definition


def _quota(cursor: _Cursor, workspace_id: str) -> dict[str, Any]:
    """Resolve the reservation row; no row is a configuration fact, not an outage.

    ``speech_workspaces`` is seeded with ``workspace-default`` by the asset
    migration, while ``workspace_quotas`` is written only when an engine is
    registered for that workspace. A create for a workspace without a quota row
    can therefore never be admitted by retrying, so it must not answer the
    storage-outage code: the empty read is deterministic and reported as
    ``workspace_not_configured``. A failed read (an exception) stays
    ``job_unavailable``. The workspace row itself is created by that migration
    and has no product deletion path -- the admin surface creates and patches
    state -- so the join's empty result cannot mean a vanished workspace;
    ``PublicJobs`` has already answered ``workspace_not_found`` for an unknown id
    before admission.
    """
    cursor.execute(
        "SELECT q.workspace_id, q.policy_revision, q.version, q.held_attempts, "
        "q.quarantined_attempts, q.max_attempts, "
        "w.policy_revision AS workspace_policy_revision "
        "FROM workspace_quotas AS q JOIN speech_workspaces AS w "
        "ON w.workspace_id=q.workspace_id WHERE q.workspace_id=%s",
        (workspace_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise PublicJobAdmissionRejectedError("workspace_not_configured")
    if len(rows) != 1:
        # Defensive only: both joined columns are primary keys, so the join yields at
        # most one row and this branch is unreachable under the shipped schema. It
        # answers the storage-outage code rather than a caller-fixable 4xx because
        # duplicated reservation rows would be data the store cannot reconcile.
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return rows[0]


def _instance(cursor: _Cursor, binding_revision_id: str, model_id: str) -> dict[str, Any]:
    cursor.execute(
        "SELECT e.instance_id, e.instance_generation, e.host_node_id, e.pool_id, "
        "e.gateway_id, e.binding_revision_id, e.manifest_id, "
        "e.version AS instance_version, e.instance_state, "
        "h.current_generation, h.current_prepare_intent_id, "
        "p.owner_id, p.fencing_token, p.pool_state, p.service_class "
        "FROM engine_instances AS e "
        "JOIN runtime_instance_heads AS h ON h.instance_id=e.instance_id "
        "AND h.current_generation=e.instance_generation "
        "JOIN resource_pools AS p ON p.pool_id=e.pool_id "
        "WHERE e.binding_revision_id=%s AND e.instance_state='ready' "
        "AND h.current_prepare_intent_id IS NOT NULL "
        "ORDER BY e.instance_id, e.instance_generation LIMIT 1",
        (binding_revision_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        # No ready instance is a capacity refusal, not an unavailable store. The
        # refusal names the deployment input: the read-only self-check already
        # reported "instance <id> generation <n> not registered" while this answer
        # pointed at capacity or the model catalogue (#877 item 4). A second read
        # separates "nothing was ever registered" from "registered, not ready":
        # only the first needs the engine-registration profile.
        cursor.execute(
            "SELECT instance_state FROM engine_instances WHERE binding_revision_id=%s "
            "ORDER BY instance_state",
            (binding_revision_id,),
        )
        states = sorted(str(row["instance_state"]) for row in cursor.fetchall())
        if not states:
            detail = (
                f"no engine instance is registered for model '{model_id}': register "
                "the engine (engine-registration profile / speechnexus-register-engine) "
                "so admission has a ready instance; retrying this request cannot create one"
            )
        else:
            detail = (
                f"the engine instance(s) registered for model '{model_id}' are not ready "
                f"(states: {', '.join(states)}): restore or re-register the engine; "
                "retrying this request cannot make one ready"
            )
        raise PublicJobAdmissionRejectedError("job_capacity_exhausted", detail)
    if len(rows) != 1:
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return rows[0]


def _runtime_plan(cursor: _Cursor, instance: dict[str, Any]) -> ApprovedRuntimePlan:
    row = _one(
        cursor,
        "SELECT * FROM runtime_instance_intents WHERE target_node_id=%s AND intent_id=%s",
        (instance["host_node_id"], instance["current_prepare_intent_id"]),
    )
    try:
        _, plan, _ = decode_record(row["request_document"])
    except Exception:  # The codec has already validated every stored immutable fact.
        # A stored intent admission cannot decode is a state it must reconcile, not a
        # store that refused the read: the transaction already answers
        # ``reconciliation_required`` for the same row, and the readiness report names
        # that refusal. A storage code here would make the two surfaces disagree (#863).
        raise PublicJobAdmissionRejectedError("engine_state_mismatch") from None
    return plan


def _voice(cursor: _Cursor, intent: dict[str, Any]) -> dict[str, Any]:
    reference = intent.get("voice")
    if not isinstance(reference, dict):
        raise PublicJobAdmissionRejectedError("voice_not_published")
    voice_id, revision = reference.get("id"), reference.get("revision")
    if isinstance(revision, str) and revision.isdigit():
        revision = int(revision)
    if not isinstance(voice_id, str) or isinstance(revision, bool) or not isinstance(revision, int):
        raise PublicJobAdmissionRejectedError("voice_not_published")
    cursor.execute(
        "SELECT revision_id, revision FROM speech_publications "
        "WHERE kind='voice' AND object_id=%s AND revision=%s AND NOT EXISTS ("
        f"SELECT 1 FROM {RETIREMENT_TABLE} AS retired WHERE retired.kind='voice' "
        "AND retired.object_id=speech_publications.object_id "
        "AND retired.revision=speech_publications.revision "
        "AND retired.revision_id=speech_publications.revision_id)",
        (voice_id, str(revision)),
    )
    rows = cursor.fetchall()
    if not rows:
        raise PublicJobAdmissionRejectedError("voice_not_published")
    row = rows[0]
    return {"id": voice_id, "revision": int(row["revision"]), "revision_id": row["revision_id"]}


_MAX_REFERENCE_TEXT_CHARS = 10_000


@dataclass(frozen=True, slots=True)
class _CustomVoiceFacts:
    voice_material: dict[str, Any]
    reference_text: str


def _require_evidenced_qualification(cursor: _Cursor, voice: dict[str, Any]) -> None:
    """Refuse a builtin qualification whose source has no evidence, by name.

    The public code stays ``voice_not_published`` -- "this revision is not usable"
    is exactly the case, and it is deterministic rather than a retry -- but the
    detail names the qualification evidence, so an operator does not go looking for
    a publication that is in fact already there (#877/#880 naming rule).
    """
    cursor.execute(
        "SELECT 1 FROM speech_builtin_voice_qualifications q "
        "WHERE q.voice_id=%s AND q.revision=%s AND q.revision_id=%s",
        (voice["id"], str(voice["revision"]), voice["revision_id"]),
    )
    if not cursor.fetchall():
        return
    cursor.execute(
        "SELECT "
        f"({qualification_source_state_sql('q')}) AS source_state "
        "FROM speech_builtin_voice_qualifications q "
        "WHERE q.voice_id=%s AND q.revision=%s AND q.revision_id=%s",
        (voice["id"], str(voice["revision"]), voice["revision_id"]),
    )
    rows = cursor.fetchall()
    state = rows[0].get("source_state") if len(rows) == 1 else None
    if state != STATE_EVIDENCED:
        raise PublicJobAdmissionRejectedError(
            "voice_not_published", source_refusal_detail(str(state))
        )


def _custom_voice(
    cursor: _Cursor,
    voice: dict[str, Any],
    *,
    workspace_id: str,
    binding_id: str,
    binding_revision: str,
    storage_bucket: str,
    now_us: int,
) -> _CustomVoiceFacts | None:
    """Resolve published clone material for one published voice revision.

    ``None`` means the revision is the existing builtin qualification path.
    A published ``cloned`` revision must name the same immutable Base binding
    that produced it; the Base template owns the shared resident TTS variant
    slot, so a CustomVoice or VoiceDesign binding cannot synthesize it.
    """
    cursor.execute(
        "SELECT source_type, material, draft_id, draft_revision, document "
        "FROM speech_voice_revisions WHERE voice_id=%s AND revision=%s AND revision_id=%s",
        (voice["id"], str(voice["revision"]), voice["revision_id"]),
    )
    rows = cursor.fetchall()
    if not rows:
        # Custom revision identities are minted as ``vrev-*``. A missing one is
        # fail-closed unless an independent builtin qualification owns the same
        # publication identity; otherwise a partial restore could synthesize a
        # clone through the builtin speaker.
        if voice["revision_id"].startswith("vrev-"):
            # The fallback is only the builtin path when the qualification's source
            # is evidenced: a published row with an `unverified` provenance would
            # otherwise reach the shared speaker without a controller observation
            # and without an accepted operator declaration.
            cursor.execute(
                "SELECT 1 FROM speech_builtin_voice_qualifications q "
                "WHERE q.voice_id=%s AND q.revision=%s AND q.revision_id=%s",
                (voice["id"], str(voice["revision"]), voice["revision_id"]),
            )
            qualified = bool(cursor.fetchall())
            if not qualified:
                raise PublicJobAdmissionRejectedError("voice_not_published")
            _require_evidenced_qualification(cursor, voice)
            return None
        # Older synthetic fixtures and operator-authored builtin ids are not
        # ``vrev-*``. A remaining draft still proves the missing row was custom.
        prefix = "voice-"
        draft_id = (
            "draft-" + voice["id"].removeprefix(prefix) if voice["id"].startswith(prefix) else None
        )
        if draft_id is not None:
            cursor.execute("SELECT 1 FROM speech_voice_drafts WHERE draft_id=%s", (draft_id,))
            if cursor.fetchall():
                raise PublicJobAdmissionRejectedError("voice_not_published")
        # A published row that names a builtin qualification is trusted only with
        # its evidence: an `unverified` provenance row means the voice was published
        # with nothing backing its source, so the shared speaker must not serve it.
        # The absence of a qualification row keeps the historical builtin path.
        _require_evidenced_qualification(cursor, voice)
        return None
    if len(rows) != 1:
        raise PublicJobAdmissionRejectedError("job_unavailable")
    row = rows[0]
    if row["source_type"] != "cloned":
        # A design revision is preview-only and must never reach TTS.
        raise PublicJobAdmissionRejectedError("voice_not_published")
    try:
        material = VoiceMaterial.from_value(row["material"])
        document = row["document"]
        if not isinstance(document, dict) or document.get("model_binding") != {
            "id": binding_id,
            "revision": binding_revision,
        }:
            raise PublicJobAdmissionRejectedError("model_binding_task_not_supported")
        draft_id = row["draft_id"]
        draft_revision = row["draft_revision"]
        if not isinstance(draft_id, str) or not isinstance(draft_revision, str):
            raise ValueError
    except PublicJobAdmissionRejectedError:
        raise
    except ValueError, TypeError, KeyError, RecursionError:
        raise PublicJobAdmissionRejectedError("job_binding_incomplete") from None
    if material.media_type != "audio/wav":
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    # Serialize with confirmed deletion: the deleter takes FOR UPDATE on this same
    # draft before it re-reads this revision, so a shared lock here decides whether
    # this admission pins the revision into an intent or observes it deleted. The
    # intent has no foreign key to the revision, so this row lock is the only
    # serialization point. Lock order is draft row first, then the revision/asset
    # reads, matching the deletion path.
    cursor.execute("SELECT 1 FROM speech_voice_drafts WHERE draft_id=%s FOR SHARE", (draft_id,))
    cursor.execute(
        "SELECT asset_state, purpose, object_key, sha256, bytes, expires_at_us "
        "FROM speech_assets WHERE workspace_id=%s AND asset_id=%s",
        (workspace_id, material.asset_id),
    )
    assets = cursor.fetchall()
    if len(assets) != 1:
        raise PublicJobAdmissionRejectedError("voice_not_published")
    asset = assets[0]
    try:
        usable = (
            asset["asset_state"] == "ready"
            and asset["purpose"] == "voice_reference"
            and asset["sha256"] == material.sha256
            and int(asset["bytes"]) == material.bytes
            and int(asset["expires_at_us"]) > now_us
            and isinstance(asset["object_key"], str)
            and bool(asset["object_key"])
        )
    except TypeError, ValueError, KeyError:
        usable = False
    if not usable:
        # The publication ledger names material this workspace can no longer use.
        raise PublicJobAdmissionRejectedError("voice_not_published")
    cursor.execute(
        "SELECT input_json FROM speech_voice_draft_revisions WHERE draft_id=%s AND revision=%s",
        (draft_id, draft_revision),
    )
    drafts = cursor.fetchall()
    if len(drafts) != 1:
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    try:
        draft_input = json.loads(drafts[0]["input_json"])
    except ValueError, TypeError, RecursionError:
        raise PublicJobAdmissionRejectedError("job_binding_incomplete") from None
    reference_text = draft_input.get("reference_text") if isinstance(draft_input, dict) else None
    if (
        not isinstance(reference_text, str)
        or not reference_text.strip()
        or not 1 <= len(reference_text) <= _MAX_REFERENCE_TEXT_CHARS
        or "\x00" in reference_text
    ):
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    return _CustomVoiceFacts(
        voice_material={
            "asset_or_artifact_id": material.asset_id,
            "workspace_id": workspace_id,
            "storage_bucket": storage_bucket,
            "object_key": asset["object_key"],
            "content_sha256": material.sha256,
            "byte_length": material.bytes,
        },
        reference_text=reference_text,
    )


def _pronunciation(
    cursor: _Cursor, intent: dict[str, Any], block: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    reference = intent.get("pronunciation")
    overrides = intent.get("overrides")
    tts_overrides = overrides.get("tts") if isinstance(overrides, dict) else None
    if isinstance(tts_overrides, dict) and "pronunciation_revision" in tts_overrides:
        revision = tts_overrides["pronunciation_revision"]
        if revision is None:
            reference = None
        else:
            if not isinstance(reference, dict):
                raise PublicJobAdmissionRejectedError("job_binding_incomplete")
            reference = {"id": reference.get("id"), "revision": revision}
    if reference is None:
        reference = block.get("pronunciation_revision")
    if reference is None:
        return [], None
    if not isinstance(reference, dict):
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    pronunciation_id, revision = reference.get("id"), reference.get("revision")
    if not isinstance(pronunciation_id, str) or not isinstance(revision, (str, int)):
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    row = _one(
        cursor,
        "SELECT definition_json FROM speech_pronunciation_revisions "
        "WHERE pronunciation_id=%s AND revision=%s",
        (pronunciation_id, str(revision)),
    )
    try:
        definition = json.loads(row["definition_json"])
        entries = definition["entries"]
    except ValueError, TypeError, KeyError:
        raise PublicJobAdmissionRejectedError("job_unavailable") from None
    if not isinstance(entries, list):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return entries, {"id": pronunciation_id, "revision": str(revision)}


def _audio(cursor: _Cursor, workspace_id: str, intent: dict[str, Any], bucket: str) -> _AudioFacts:
    request_input = intent.get("input")
    if not isinstance(request_input, dict):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    asset_id = request_input.get("audio_asset_id")
    if not isinstance(asset_id, str):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    row = _one(
        cursor,
        "SELECT asset_state, purpose, object_key, sha256, bytes, media_type, "
        "sample_rate_hz, channels FROM speech_assets "
        "WHERE workspace_id=%s AND asset_id=%s",
        (workspace_id, asset_id),
    )
    if row["asset_state"] != "ready" or row["purpose"] != "asr_input":
        raise PublicJobAdmissionRejectedError("asset_not_ready")
    try:
        audio = StagedAudio(
            asset_id,
            workspace_id,
            bucket,
            row["object_key"],
            row["sha256"],
            int(row["bytes"]),
        )
        media_type = row["media_type"]
        sample_rate = int(row["sample_rate_hz"])
        channels = int(row["channels"])
    except ValueError, TypeError, KeyError:
        raise PublicJobAdmissionRejectedError("job_unavailable") from None
    if not isinstance(media_type, str):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return _AudioFacts(audio, media_type, sample_rate, channels)


@dataclass(frozen=True, slots=True)
class _AudioFacts:
    audio: StagedAudio
    media_type: str
    sample_rate_hz: int
    channels: int


def _overrides(intent: dict[str, Any], kind: str) -> dict[str, Any]:
    value = intent.get("overrides")
    if not isinstance(value, dict):
        return {}
    branch = value.get(kind)
    if branch is None:
        return {}
    if not isinstance(branch, dict):
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    return branch


def _asr_parameters(block: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    merged = {**block, **_overrides(intent, "asr")}
    response_format = intent["output"]["transcript_format"]
    gateway_format = (
        TranscriptFormat.TEXT.value
        if response_format == TranscriptFormat.TEXT.value
        else TranscriptFormat.JSON.value
    )
    return {
        "language": merged["language"],
        "turn_detection": merged["turn_detection"],
        "finalization": merged["finalization"],
        "preview_interval_ms": merged["preview_interval_ms"],
        "max_segment_ms": merged["max_segment_ms"],
        "rollback_tokens": merged["rollback_tokens"],
        "final_deadline_ms": merged["final_deadline_ms"],
        "auxiliary_deadline_ms": merged["auxiliary_deadline_ms"],
        "alignment": merged["alignment"],
        "diarization": merged["diarization"],
        "vocabulary_set_revision": merged["vocabulary_set_revision"],
        "prompt": None,
        "temperature": merged["temperature"],
        "response_format": gateway_format,
        "timestamp_granularities": [],
    }


def _custom_tts_unsupported(
    *,
    instructions: Any,
    speed: Any,
    pronunciation: list[dict[str, Any]],
    pronunciation_source: dict[str, Any] | None,
) -> bool:
    """The Base reference-audio lane has no instructions, speed or pronunciation pass."""
    return (
        bool(instructions) or speed != 1 or bool(pronunciation) or pronunciation_source is not None
    )


def _tts_parameters(
    block: dict[str, Any],
    intent: dict[str, Any],
    pronunciation: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = {**block, **_overrides(intent, "tts")}
    audio_format = intent["output"]["audio_format"]
    return {
        "text_commit_mode": merged["text_commit_mode"],
        "audio_delivery": merged["audio_delivery"],
        "language": merged["language"],
        "pronunciation_revision": pronunciation,
        "instructions": merged["instructions"],
        "speed": merged["speed"],
        "seed": merged["seed"],
        "planner": merged["planner"],
        "response_format": audio_format,
    }


def _admitted(admission: AdmissionRequest) -> AdmittedExecution:
    stage = admission.stages[0]
    return AdmittedExecution(
        submit_intent_identity(admission.job.job_id),
        stage.lease_id,
        admission.identity.operation_id,
        admission.identity.attempt_id,
        stage.stage_id,
        admission.workspace_id,
        stage.pool_id,
        stage.host_node_id,
        stage.gateway_id,
        stage.instance_id,
        stage.instance_generation,
        admission.binding_revision_id,
        admission.policy_revision_id,
        admission.authorities[0].fencing_token,
        admission.deadline_at_us,
    )


def build_public_job_admission(
    cursor: _Cursor,
    job: NewPublicJob,
    *,
    now_us: int,
    max_command_bytes: int,
    storage_bucket: str,
) -> PublicJobAdmission | None:
    """Return ``None`` for legacy voice kinds, which keep the pre-admission path.

    ``voice_validation`` is admitted for a clone candidate only. A design
    revision is not a candidate and raises ``voice_draft_not_candidate``; it
    never returns ``None`` or a half-admitted Job.
    """
    if job.kind not in ("asr", "tts", "voice_validation"):
        return None
    intent = _intent(job)
    try:
        configuration = json.loads(job.submission.effective_configuration_json)
        # Voice generation and validation pin the single ``voice`` model slot;
        # only ASR/TTS use a slot named after the Job kind.
        reference = configuration["models"]["voice" if job.kind == "voice_validation" else job.kind]
        model_id, revision = reference["id"], reference["revision"]
    except ValueError, TypeError, KeyError, RecursionError:
        raise PublicJobAdmissionRejectedError("job_unavailable") from None
    binding_revision_id, binding_revision, definition = _binding(cursor, model_id, revision)
    block: dict[str, Any] | None
    if job.kind == "voice_validation":
        # No binding execution block exists for voice work and this slice must
        # not borrow ASR/TTS defaults. The measurement budget is server-owned.
        timeout = VOICE_VALIDATION_TIMEOUT_SECONDS
        execution: dict[str, Any] = {
            "kind": "voice_validation",
            "timeout_seconds": timeout,
        }
    else:
        block = _execution_block(definition, job.kind)
        timeout = block["timeout_seconds"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 600
        ):
            raise PublicJobAdmissionRejectedError("job_binding_incomplete")
        execution = block
    quota = _quota(cursor, job.workspace_id)
    instance = _instance(cursor, binding_revision_id, model_id)
    plan = _runtime_plan(cursor, instance)
    if (
        plan.binding.binding_revision_id != binding_revision_id
        or plan.binding.manifest_id
        != definition.get("execution_recipe", {}).get("capacity_manifest_id")
        or plan.binding.pool_id != instance["pool_id"]
        or plan.binding.gateway_id != instance["gateway_id"]
        or plan.binding.target_node_id != instance["host_node_id"]
        or instance["manifest_id"] != plan.binding.manifest_id
    ):
        raise PublicJobAdmissionRejectedError("job_binding_incomplete")
    # A pool without a live owner identity cannot even become an OwnerAuthority; that
    # is the disabled class, not capacity. ``pool_state`` is decided by the converged
    # predicate, which the report runs too, so a draining pool answers one code on
    # both surfaces instead of the loader's old 429 (#863).
    if not isinstance(instance["owner_id"], str) or int(instance["fencing_token"]) < 1:
        raise PublicJobAdmissionRejectedError("engine_resource_disabled")
    deadline_at_us = now_us + max(1, round(timeout * 1_000_000))
    # A1/A3 join admission_operations.operation_id = speech_jobs.job_id.
    identity = AdmissionIdentity(job.job_id, "attempt-" + job.job_id.removeprefix("job-"))
    lease_id = "lease-" + job.job_id.removeprefix("job-")
    stage = StageAllocation(
        "stage-" + job.job_id.removeprefix("job-"),
        lease_id,
        instance["pool_id"],
        instance["host_node_id"],
        instance["gateway_id"],
        instance["instance_id"],
        int(instance["instance_generation"]),
        int(instance["instance_version"]),
        1,
    )
    authority = OwnerAuthority(
        instance["pool_id"], instance["owner_id"], int(instance["fencing_token"])
    )
    if job.kind == "asr":
        assert block is not None
        audio = _audio(cursor, job.workspace_id, intent, storage_bucket)
        parameters = _asr_parameters(block, intent)
        probe = _empty_admission(
            identity, job, instance, quota, plan, stage, authority, deadline_at_us
        )
        gateway = build_asr_submit(
            _admitted(probe),
            audio.audio,
            media_type=audio.media_type,
            sample_rate_hz=audio.sample_rate_hz,
            channels=audio.channels,
            parameters=parameters,
        )
        business_value = {
            "kind": "asr_file",
            "request_id": job.job_id,
            "workspace_id": job.workspace_id,
            "model": model_id,
            "binding_publication_id": binding_revision_id,
            "audio": {
                "asset_or_artifact_id": audio.audio.asset_id,
                "content_sha256": audio.audio.content_sha256,
                "byte_length": audio.audio.byte_length,
                "media_type": audio.media_type,
                "sample_rate_hz": audio.sample_rate_hz,
                "channels": audio.channels,
            },
            "parameters": parameters,
            "gateway_request": gateway,
        }
    elif job.kind == "tts":
        assert block is not None
        voice = _voice(cursor, intent)
        pronunciation, pronunciation_source = _pronunciation(cursor, intent, block)
        merged = {**block, **_overrides(intent, "tts")}
        if (
            merged.get("text_commit_mode") != "explicit"
            or merged.get("audio_delivery") != "complete"
        ):
            raise PublicJobAdmissionRejectedError("job_binding_incomplete")
        planner = merged.get("planner")
        if not isinstance(planner, dict):
            planner = block["planner"]
        request_input = intent["input"]
        if "text_asset_id" in request_input:
            raise PublicJobAdmissionRejectedError("job_text_asset_unsupported")
        text = request_input.get("text")
        if not isinstance(text, str):
            raise PublicJobAdmissionRejectedError("job_unavailable")
        plan_value = SpeechPlan(
            request_id=job.job_id,
            model_id=model_id,
            workspace_id=job.workspace_id,
            binding_revision_id=binding_revision_id,
            workspace_policy_revision=int(quota["workspace_policy_revision"]),
            text=text,
            voice_id=voice["id"],
            voice_revision_id=voice["revision_id"],
            voice_revision=voice["revision"],
            response_format=SpeechFormat(intent["output"]["audio_format"]),
            sample_rate_hz=block["sample_rate_hz"],
            channels=1,
            speed=merged["speed"],
            instructions=merged["instructions"] or "",
            language=merged["language"],
            seed=merged["seed"],
            planner_max_chars=planner["max_chars"],
            planner_max_wait_ms=planner["max_wait_ms"],
            max_audio_ms=block["max_output_audio_ms"],
            timeout_seconds=timeout,
        )
        custom = _custom_voice(
            cursor,
            voice,
            workspace_id=job.workspace_id,
            binding_id=model_id,
            binding_revision=str(binding_revision),
            storage_bucket=storage_bucket,
            now_us=now_us,
        )
        parameters = _tts_parameters(block, intent, pronunciation_source)
        if custom is None:
            voice_kind, voice_material = "builtin", None
        else:
            voice_kind, voice_material = "custom", custom.voice_material
            # The Base lane has no instructions, speed control or pronunciation
            # pass: refusing them is deterministic instead of silently dropping
            # caller intent the published clone path cannot honour.
            if _custom_tts_unsupported(
                instructions=merged["instructions"],
                speed=merged["speed"],
                pronunciation=pronunciation,
                pronunciation_source=pronunciation_source,
            ):
                raise PublicJobAdmissionRejectedError("unsupported_parameter")
            parameters["reference_text"] = custom.reference_text
        resources = {
            "voice": {"id": voice["id"], "revision": str(voice["revision"])},
            "voice_material": voice_material,
            "vocabulary": [],
            "pronunciation": pronunciation,
            "voice_kind": voice_kind,
            "vocabulary_source": None,
            "pronunciation_source": pronunciation_source,
        }
        probe = _empty_admission(
            identity, job, instance, quota, plan, stage, authority, deadline_at_us
        )
        gateway = build_tts_submit_from_parameters(
            plan_value, _admitted(probe), resources=resources, parameters=parameters
        )
        business_value = {
            "kind": "tts_file",
            "plan": asdict(plan_value),
            "voice": {
                "id": voice["id"],
                "revision": voice["revision"],
                "revision_id": voice["revision_id"],
            },
            "gateway_request": gateway,
        }
        public_pin = intent.get("model_binding")
        if isinstance(public_pin, dict):
            business_value["model_binding"] = public_pin
    else:
        draft_id, draft_revision = _validation_target(intent)
        # Same serialization point as the custom-voice path. A pinned create
        # already holds FOR UPDATE from ``_lock_validation_target``, but an
        # unpinned create reaches this read first; taking FOR SHARE here covers
        # both and keeps deletion from removing the revision between this read and
        # the intent insert.
        cursor.execute("SELECT 1 FROM speech_voice_drafts WHERE draft_id=%s FOR SHARE", (draft_id,))
        # A revision that is gone is a binding the caller can fix by pinning a new
        # one, not a store that refused the read, so it must not answer
        # ``job_unavailable`` -- that code is reserved for a genuinely unavailable
        # store (see the contract's "Job store refusing the read or the write") and
        # it tells the client not to retry. The custom-voice sibling above answers
        # ``job_binding_incomplete`` for the same condition.
        cursor.execute(
            "SELECT r.kind, r.input_json, r.binding_json, j.workspace_id "
            "FROM speech_voice_draft_revisions AS r "
            "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
            "WHERE r.draft_id=%s AND r.revision=%s",
            (draft_id, draft_revision),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise PublicJobAdmissionRejectedError("job_binding_incomplete")
        row = rows[0]
        if row["workspace_id"] != job.workspace_id or row["kind"] != "clone":
            raise PublicJobAdmissionRejectedError("voice_draft_not_candidate")
        try:
            draft_input = json.loads(row["input_json"])
            draft_binding = json.loads(row["binding_json"])
        except ValueError, TypeError, RecursionError:
            raise PublicJobAdmissionRejectedError("job_unavailable") from None
        if (
            not isinstance(draft_input, dict)
            or not isinstance(draft_binding, dict)
            or draft_input.get("source_use_confirmed") is not True
            or draft_binding.get("id") != model_id
            or str(draft_binding.get("revision")) != str(binding_revision)
        ):
            raise PublicJobAdmissionRejectedError("voice_draft_not_candidate")
        reference = _voice_reference(cursor, job.workspace_id, draft_input, storage_bucket)
        reference_text = draft_input.get("reference_text")
        if reference_text is not None and (
            not isinstance(reference_text, str) or not 1 <= len(reference_text) <= 10000
        ):
            raise PublicJobAdmissionRejectedError("voice_draft_not_candidate")
        probe = _empty_admission(
            identity, job, instance, quota, plan, stage, authority, deadline_at_us
        )
        gateway = build_voice_validation_submit(
            _admitted(probe),
            reference.audio,
            reference_sample_rate_hz=reference.sample_rate_hz,
            reference_channels=reference.channels,
            reference_text=reference_text,
            language=VALIDATION_PROBE_LANGUAGE,
            preview_text=validation_probe_text(job.job_id),
        )
        command = cast(dict[str, Any], gateway["command"])
        business_value = {
            "kind": "voiceclone",
            "request_id": job.job_id,
            "workspace_id": job.workspace_id,
            "model": model_id,
            "binding_publication_id": binding_revision_id,
            "validation": {
                "draft_id": draft_id,
                "draft_revision": draft_revision,
                "validation_set_revision": intent["input"]["validation_set_revision"],
            },
            "reference_audio": command["reference_audio"],
            "reference_text": reference_text,
            "preview_text": command["preview_text"],
            "language": VALIDATION_PROBE_LANGUAGE,
            "source_use_confirmed": True,
            "gateway_request": gateway,
        }
    business = business_command_from_value(business_value, max_command_bytes=max_command_bytes)
    request = AdmissionRequest(
        identity=identity,
        workspace_id=job.workspace_id,
        binding_revision_id=binding_revision_id,
        manifest_id=plan.binding.manifest_id,
        recipe_digest=canonical_digest(
            {
                "binding_revision_id": binding_revision_id,
                "manifest_id": plan.binding.manifest_id,
                "execution": execution,
            }
        ),
        policy_revision_id=quota["policy_revision"],
        expected_quota_version=int(quota["version"]),
        service_class=plan.service_class,
        authorities=(authority,),
        stages=(stage,),
        deadline_at_us=deadline_at_us,
        job=CreateJob(
            job.job_id,
            identity.operation_id,
            identity.attempt_id,
            "event-" + job.job_id.removeprefix("job-"),
            canonical_digest(
                {
                    "job_id": job.job_id,
                    "operation_id": identity.operation_id,
                    "attempt_id": identity.attempt_id,
                    "request_digest": business.request_digest,
                }
            ),
            business.request_digest,
        ),
    )
    approved = ApprovedAdmissionRecipe(
        binding_revision_id,
        plan.binding.manifest_id,
        request.recipe_digest,
        quota["policy_revision"],
        plan.service_class,
        (ApprovedStage(stage.stage_id, 1, plan),),
        deadline_at_us,
        frozenset({"concurrent_attempts"}),
    )
    prepared = prepare_admission(
        AdmitRecipe(request, business, approved), max_command_bytes=max_command_bytes
    )
    return PublicJobAdmission(prepared, lease_id, deadline_at_us)


def _validation_target(intent: dict[str, Any]) -> tuple[str, str]:
    request_input = intent.get("input")
    if not isinstance(request_input, dict):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    draft_id = request_input.get("voice_draft_id")
    revision = request_input.get("draft_revision")
    if not isinstance(draft_id, str) or not isinstance(revision, str):
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return draft_id, revision


def _voice_reference(
    cursor: _Cursor, workspace_id: str, draft_input: dict[str, Any], bucket: str
) -> _AudioFacts:
    asset_id = draft_input.get("reference_audio_asset_id")
    if not isinstance(asset_id, str):
        raise PublicJobAdmissionRejectedError("asset_not_ready")
    cursor.execute(
        "SELECT asset_state, purpose, object_key, sha256, bytes, media_type, "
        "sample_rate_hz, channels FROM speech_assets "
        "WHERE workspace_id=%s AND asset_id=%s",
        (workspace_id, asset_id),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        # Missing and cross-workspace assets are the same deterministic 422
        # candidate refusal; they are not a storage outage.
        raise PublicJobAdmissionRejectedError("asset_not_ready")
    row = rows[0]
    if row["asset_state"] != "ready" or row["purpose"] != "voice_reference":
        raise PublicJobAdmissionRejectedError("asset_not_ready")
    try:
        audio = StagedAudio(
            asset_id,
            workspace_id,
            bucket,
            row["object_key"],
            row["sha256"],
            int(row["bytes"]),
        )
        media_type = row["media_type"]
        sample_rate = int(row["sample_rate_hz"])
        channels = int(row["channels"])
    except ValueError, TypeError, KeyError:
        raise PublicJobAdmissionRejectedError("asset_not_ready") from None
    if media_type != "audio/wav" or sample_rate not in (16_000, 24_000, 48_000):
        raise PublicJobAdmissionRejectedError("asset_not_ready")
    if channels < 1 or channels > 8:
        raise PublicJobAdmissionRejectedError("asset_not_ready")
    return _AudioFacts(audio, media_type, sample_rate, channels)


def _empty_admission(
    identity: AdmissionIdentity,
    job: NewPublicJob,
    instance: dict[str, Any],
    quota: dict[str, Any],
    plan: ApprovedRuntimePlan,
    stage: StageAllocation,
    authority: OwnerAuthority,
    deadline_at_us: int,
) -> AdmissionRequest:
    """The identity-only envelope used to build the gateway document before admission."""
    return AdmissionRequest(
        identity=identity,
        workspace_id=job.workspace_id,
        binding_revision_id=plan.binding.binding_revision_id,
        manifest_id=plan.binding.manifest_id,
        recipe_digest=canonical_digest({"identity": identity.operation_id}),
        policy_revision_id=quota["policy_revision"],
        expected_quota_version=int(quota["version"]),
        service_class=plan.service_class,
        authorities=(authority,),
        stages=(stage,),
        deadline_at_us=deadline_at_us,
        job=CreateJob(
            job.job_id,
            identity.operation_id,
            identity.attempt_id,
            "event-probe",
            "sha256:" + "0" * 64,
            "sha256:" + "0" * 64,
        ),
    )


def rejection_code(code: str) -> str:
    """Map an admission refusal to the public error class that states its recovery.

    Capacity refusals share the 429 ``job_capacity_exhausted`` answer; every other
    code is indexed in ``_PUBLIC_CODES``. A silent default is what turned an elapsed
    readiness receipt into "Job storage is unavailable." for a healthy store
    (#828), so an unclassified refusal never borrows the outage answer: it answers
    the declared 500 defect code, which fails loudly and stays contract-valid
    (#828 review M1).
    """
    if code in CAPACITY_REJECTION_CODES:
        return "job_capacity_exhausted"
    try:
        return _PUBLIC_CODES[code]
    except KeyError:
        # An admission code without a public answer is a deployment defect, not a
        # storage fault: answer with the declared defect code so the client sees a
        # contract-valid envelope instead of a bare 500 (#828 review M1).
        raise PublicJobAdmissionRejectedError("admission_state_invalid") from None


# The probe command a readiness read builds carries only deployment facts: the
# binding, plan, pool, node and quota are the stored rows a create would read, and
# the identities below are constant so no request-shaped fact can enter the
# verdict. The report never commits it.
_PROBE_WORKSPACE_DEFAULT = "workspace-default"
_PROBE_OPERATION = "probe-admission-readiness-op"
_PROBE_ATTEMPT = "probe-admission-readiness-attempt"
_PROBE_JOB = "probe-admission-readiness-job"
_PROBE_EVENT = "probe-admission-readiness-event"
_PROBE_STAGE = "probe-admission-readiness-stage"
_PROBE_LEASE = "probe-admission-readiness-lease"
_PROBE_BUSINESS = '{"probe":"admission-readiness"}'

# A probe command is built from the rows admission itself reads, so a stored
# disagreement it surfaces is a state mismatch rather than a request-shaped
# ``recipe_not_admitted``: nothing about the caller's body can produce it.
_PROBE_STATE_CODES: dict[str, AdmissionReadinessCause] = {
    "recipe_mismatch": AdmissionReadinessCause.ENGINE_STATE_MISMATCH,
}


def _readiness_workspace_state(cursor: _Cursor, workspace_id: str) -> str | None:
    cursor.execute(
        "SELECT workspace_state FROM speech_workspaces WHERE workspace_id=%s",
        (workspace_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return str(rows[0]["workspace_state"])


def _readiness_candidates(cursor: _Cursor) -> list[dict[str, Any]]:
    """The one instance per binding that a create would pin, in binding order.

    A create is asked for a binding and pins the first ``ready`` instance of that
    binding by ``(instance_id, instance_generation)``. The report has no binding
    argument, so it asks that same question of every binding: ``DISTINCT ON``
    keeps the first instance of each, and the rows come back in
    ``binding_revision_id`` order so the reported cause is deterministic.

    Reading every ready instance instead is the defect the readiness report was
    rewritten for (#857 third round, #863): with the pinned instance's node
    quarantined and a healthy sibling behind the same binding, the report probed
    the sibling and answered ``admitting`` while every create for that binding was
    refused. An instance the create path would never select (not ready, no prepare
    intent) is not a candidate in either path.
    """
    cursor.execute(
        "SELECT DISTINCT ON (i.binding_revision_id) i.binding_revision_id, i.instance_id, "
        "i.instance_generation, i.host_node_id, i.pool_id "
        "FROM engine_instances AS i "
        "JOIN runtime_instance_heads AS h ON h.instance_id = i.instance_id "
        "AND h.current_generation = i.instance_generation "
        "WHERE i.instance_state='ready' AND i.instance_generation IS NOT NULL "
        "AND h.current_prepare_intent_id IS NOT NULL "
        "ORDER BY i.binding_revision_id, i.instance_id, i.instance_generation"
    )
    return cursor.fetchall()


def _readiness_one(cursor: _Cursor, query: str, parameters: tuple[object, ...]) -> dict[str, Any]:
    cursor.execute(query, parameters)
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise PublicJobAdmissionRejectedError("job_unavailable")
    return rows[0]


def _readiness_runtime(
    cursor: _Cursor, candidate: dict[str, Any], *, now_us: int
) -> tuple[AdmissionRuntime, RuntimeHost | None, str]:
    """Decode one candidate exactly as the admission transaction decodes it.

    Returns the admission facts, the host row (``None`` when it is missing, which
    a create answers as a reconciliation refusal) and the readiness window state
    the public per-instance row reports. ``missing``/``invalid`` are reported
    rather than raised: a stored intent admission cannot decode is a fact about
    the deployment, and dropping it would let the report answer ``admitting``
    while every create for that instance is refused (#857).
    """
    instance_row = _readiness_one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id=%s AND instance_generation=%s",
        (candidate["instance_id"], candidate["instance_generation"]),
    )
    head = _readiness_one(
        cursor,
        "SELECT * FROM runtime_instance_heads WHERE instance_id=%s AND current_generation=%s",
        (candidate["instance_id"], candidate["instance_generation"]),
    )
    prepare_id = head["current_prepare_intent_id"]
    if prepare_id is None:
        raise PublicJobAdmissionRejectedError("job_capacity_exhausted")
    intent = _readiness_one(
        cursor,
        "SELECT * FROM runtime_instance_intents WHERE target_node_id=%s AND intent_id=%s",
        (instance_row["host_node_id"], prepare_id),
    )
    record = _record(intent)
    instance = _runtime_instance(instance_row, record)
    if record.observation_document is None:
        raise PublicJobAdmissionRejectedError("job_unavailable")
    observation = decode_observation(record.observation_document).observation
    if record.readiness_document is None:
        readiness_state = READINESS_MISSING
        readiness = None
    else:
        readiness = validate_stored_readiness(
            record.readiness_document, record.request, record.approved_plan
        )
        readiness_state = readiness.expiry_state(
            now_us, warning_us=DEFAULT_READINESS_WARNING_SECONDS * 1_000_000
        )
    cursor.execute(
        "SELECT COALESCE(SUM(l.reserved_slots), 0) AS slots FROM admission_leases AS l "
        "JOIN admission_attempts AS a ON a.operation_id = l.operation_id "
        "AND a.attempt_id = l.attempt_id WHERE l.instance_id = %s "
        "AND l.instance_generation = %s AND l.lease_state <> 'released' "
        "AND a.service_class <> 'realtime'",
        (instance.instance_id, instance.instance_generation),
    )
    batch = int(cursor.fetchall()[0]["slots"])
    if readiness is None:
        raise PublicJobAdmissionRejectedError("job_unavailable")
    cursor.execute(
        "SELECT * FROM host_nodes WHERE host_node_id=%s",
        (instance_row["host_node_id"],),
    )
    host_rows = cursor.fetchall()
    host = _host(host_rows[0]) if host_rows else None
    return (
        AdmissionRuntime(instance, _head(head), observation, readiness, batch),
        host,
        readiness_state,
    )


def _probe_command(
    *,
    workspace_id: str,
    runtime: AdmissionRuntime,
    pool: AdmissionPool,
    quota: AdmissionQuota,
    now_us: int,
) -> AdmitRecipe:
    """Build the command a create would hand admission, from stored rows only."""
    instance, plan = runtime.instance, runtime.instance.plan
    binding = plan.binding
    duration_us = 1
    deadline_at_us = now_us + duration_us
    stage = StageAllocation(
        _PROBE_STAGE,
        _PROBE_LEASE,
        binding.pool_id,
        binding.target_node_id,
        binding.gateway_id,
        instance.instance_id,
        instance.instance_generation,
        instance.version,
        1,
    )
    owner_id = pool.ownership.owner_id
    if owner_id is None:
        # A pool with no live owner identity cannot produce an authority; the caller
        # reports it as the state the deployment must reconcile.
        raise ValueError("pool ownership has no owner identity")
    authority = OwnerAuthority(pool.ownership.pool_id, owner_id, pool.ownership.fencing_token)
    business = OriginalBusinessCommand(
        _PROBE_BUSINESS,
        "sha256:" + hashlib.sha256(_PROBE_BUSINESS.encode()).hexdigest(),
    )
    request = AdmissionRequest(
        identity=AdmissionIdentity(_PROBE_OPERATION, _PROBE_ATTEMPT),
        workspace_id=workspace_id,
        binding_revision_id=binding.binding_revision_id,
        manifest_id=binding.manifest_id,
        recipe_digest=canonical_digest(
            {
                "binding_revision_id": binding.binding_revision_id,
                "manifest_id": binding.manifest_id,
                "probe": True,
            }
        ),
        policy_revision_id=quota.policy_revision_id,
        expected_quota_version=quota.version,
        service_class=plan.service_class,
        authorities=(authority,),
        stages=(stage,),
        deadline_at_us=deadline_at_us,
        job=CreateJob(
            _PROBE_JOB,
            _PROBE_OPERATION,
            _PROBE_ATTEMPT,
            _PROBE_EVENT,
            canonical_digest({"probe": "admission-readiness-event"}),
            business.request_digest,
        ),
    )
    approved = ApprovedAdmissionRecipe(
        binding.binding_revision_id,
        binding.manifest_id,
        request.recipe_digest,
        quota.policy_revision_id,
        plan.service_class,
        (ApprovedStage(stage.stage_id, 1, plan),),
        deadline_at_us,
        frozenset({"concurrent_attempts"}),
    )
    return AdmitRecipe(request, business, approved)


def _instance_report(
    runtime: AdmissionRuntime, readiness_state: str, now_us: int
) -> ReadinessInstanceReport:
    instance = runtime.instance
    return ReadinessInstanceReport(
        instance.instance_id,
        instance.instance_generation,
        readiness_state,
        runtime.readiness.expires_at_us,
        (runtime.readiness.expires_at_us - now_us) // 1_000_000,
        instance.plan.max_slots,
        instance.held_slots,
        instance.quarantined_slots,
    )


def load_admission_readiness(
    cursor: _Cursor, workspace_id: str, *, now_us: int
) -> AdmissionReadiness:
    """Classify one instant by calling the create path's own predicate.

    Every comparison lives in ``decide_create_admission``: this function only reads
    the rows admission reads, builds the same command and names the one refusal it
    returned. A condition that refuses a create therefore cannot be reported as
    ``admitting``, and the report never recomputes a window or a counter (#863).

    The probe set is the create path's own selection: the first ready instance of
    each binding (``_readiness_candidates``). The report answers ``admitting`` only
    when the predicate admits every one of those instances, because a create naming
    any of those bindings would pin exactly that instance; the first refusal in
    binding order is the reported cause.
    """
    identifier(workspace_id)
    state = _readiness_workspace_state(cursor, workspace_id)
    if state is None:
        return refused_readiness(
            AdmissionReadinessCause.WORKSPACE_NOT_FOUND,
            workspace_id=workspace_id,
            now_us=now_us,
        )
    if state != "active":
        return refused_readiness(
            AdmissionReadinessCause.WORKSPACE_DISABLED,
            workspace_id=workspace_id,
            now_us=now_us,
            workspace_state=state,
        )
    try:
        quota_row = _quota(cursor, workspace_id)
    except PublicJobAdmissionRejectedError as error:
        if error.code != "workspace_not_configured":
            raise
        return refused_readiness(
            AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED,
            workspace_id=workspace_id,
            now_us=now_us,
        )
    quota = _admission_quota_row(quota_row)
    candidates = _readiness_candidates(cursor)
    reports: list[ReadinessInstanceReport] = []
    refusal: str | None = None
    for candidate in candidates:
        try:
            runtime, host, readiness_state = _readiness_runtime(cursor, candidate, now_us=now_us)
        except (
            PublicJobAdmissionRejectedError,
            RuntimeRejectedError,
            ValueError,
            TypeError,
            KeyError,
        ):
            # A stored intent admission cannot decode is an engine-side state the
            # create path refuses; the per-instance row still reports what was seen.
            reports.append(
                ReadinessInstanceReport(
                    str(candidate["instance_id"]),
                    int(candidate["instance_generation"]),
                    READINESS_INVALID,
                    None,
                    None,
                    0,
                    0,
                    0,
                )
            )
            refusal = refusal or "reconciliation_required"
            continue
        reports.append(_instance_report(runtime, readiness_state, now_us))
        if host is None:
            refusal = refusal or "reconciliation_required"
            continue
        pool_row = _readiness_one(
            cursor,
            "SELECT * FROM resource_pools WHERE pool_id=%s",
            (runtime.instance.plan.binding.pool_id,),
        )
        pool = _admission_pool_row(pool_row)
        try:
            command = _probe_command(
                workspace_id=workspace_id, runtime=runtime, pool=pool, quota=quota, now_us=now_us
            )
        except ValueError:
            refusal = refusal or "reconciliation_required"
            continue
        verdict = decide_create_admission(
            command,
            quota=quota,
            pools=(pool,),
            hosts=(host,),
            runtimes=(runtime,),
            now_us=now_us,
        )
        if isinstance(verdict, Refused) and refusal is None:
            # The candidates are in binding order, so the first refusal seen is the
            # cause the report names; a later refusal cannot override it.
            refusal = verdict.code
    if refusal is None:
        if candidates:
            return admitted_readiness(
                workspace_id=workspace_id,
                now_us=now_us,
                instances=tuple(reports),
                quota=quota,
            )
        # No ready instance at all: the create path selects nothing and answers the
        # capacity refusal, so the report names the same class.
        refusal = "job_capacity_exhausted"
    cause = _PROBE_STATE_CODES.get(refusal) or cause_for_refusal(refusal)
    return refused_readiness(
        cause,
        workspace_id=workspace_id,
        now_us=now_us,
        instances=tuple(reports),
        quota=quota,
        workspace_state=state,
    )


__all__ = [
    "PublicJobAdmission",
    "PublicJobAdmissionRejectedError",
    "build_public_job_admission",
    "load_admission_readiness",
    "rejection_code",
]
