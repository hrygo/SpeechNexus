"""Complete public Job admission built from frozen binding and request facts.

The public contract deliberately keeps only the caller's intent. This module is
the one producer that turns that intent into the complete effective command the
execution gateway consumes, using only facts frozen in the same transaction:
the binding revision definition, its execution defaults, the published voice or
pronunciation revision, the ready runtime plan and the stored asset row. It
never reads deployment settings for a value the binding must declare.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

from speechnexus_backend.speech.application.transcription import AdmittedExecution, StagedAudio
from speechnexus_backend.speech.application.voice_validation_report import (
    VALIDATION_PROBE_LANGUAGE,
)
from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRequest,
    AdmitRecipe,
    ApprovedAdmissionRecipe,
    ApprovedStage,
    StageAllocation,
)
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from speechnexus_backend.speech.domain.public_jobs import NewPublicJob, canonical_digest
from speechnexus_backend.speech.domain.runtime_intents import ApprovedRuntimePlan
from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat, SpeechPlan
from speechnexus_backend.speech.domain.transcription import TranscriptFormat
from speechnexus_backend.speech.infrastructure.admission_codec import (
    PreparedAdmission,
    business_command_from_value,
    prepare_admission,
)
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import RETIREMENT_TABLE
from speechnexus_backend.speech.infrastructure.runtime_codec import decode_record
from speechnexus_backend.speech.infrastructure.transcription_submit import build_asr_submit
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit_from_parameters
from speechnexus_backend.speech.infrastructure.voice_validation_submit import (
    VOICE_VALIDATION_TIMEOUT_SECONDS,
    build_voice_validation_submit,
    validation_probe_text,
)

_CAPACITY_CODES = frozenset({"capacity_exhausted", "quota_exceeded"})
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
    """A create must be refused with one stable public error code."""

    def __init__(self, code: str) -> None:
        self.code = code


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
    return _one(
        cursor,
        "SELECT q.policy_revision, q.version, q.held_attempts, q.quarantined_attempts, "
        "w.policy_revision AS workspace_policy_revision "
        "FROM workspace_quotas AS q JOIN speech_workspaces AS w "
        "ON w.workspace_id=q.workspace_id WHERE q.workspace_id=%s",
        (workspace_id,),
    )


def _instance(cursor: _Cursor, binding_revision_id: str) -> dict[str, Any]:
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
        # No ready instance is a capacity refusal, not an unavailable store.
        raise PublicJobAdmissionRejectedError("job_capacity_exhausted")
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
        raise PublicJobAdmissionRejectedError("job_unavailable") from None
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
    row = _one(
        cursor,
        "SELECT revision_id, revision FROM speech_publications "
        "WHERE kind='voice' AND object_id=%s AND revision=%s AND NOT EXISTS ("
        f"SELECT 1 FROM {RETIREMENT_TABLE} AS retired WHERE retired.kind='voice' "
        "AND retired.object_id=speech_publications.object_id "
        "AND retired.revision=speech_publications.revision "
        "AND retired.revision_id=speech_publications.revision_id)",
        (voice_id, str(revision)),
    )
    return {"id": voice_id, "revision": int(row["revision"]), "revision_id": row["revision_id"]}


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
    instance = _instance(cursor, binding_revision_id)
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
    if (
        instance["pool_state"] != "active"
        or not isinstance(instance["owner_id"], str)
        or int(instance["fencing_token"]) < 1
    ):
        raise PublicJobAdmissionRejectedError("job_capacity_exhausted")
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
        parameters = _tts_parameters(block, intent, pronunciation_source)
        resources = {
            "voice": {"id": voice["id"], "revision": str(voice["revision"])},
            "voice_material": None,
            "vocabulary": [],
            "pronunciation": pronunciation,
            "voice_kind": "builtin",
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
        row = _one(
            cursor,
            "SELECT r.kind, r.input_json, r.binding_json, j.workspace_id "
            "FROM speech_voice_draft_revisions AS r "
            "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
            "WHERE r.draft_id=%s AND r.revision=%s",
            (draft_id, draft_revision),
        )
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
    """Map an admission refusal to the stable public error class."""
    if code in _CAPACITY_CODES:
        return "job_capacity_exhausted"
    if code == "idempotency_conflict":
        return "idempotency_conflict"
    return "job_unavailable"


__all__ = [
    "PublicJobAdmission",
    "PublicJobAdmissionRejectedError",
    "build_public_job_admission",
    "rejection_code",
]
