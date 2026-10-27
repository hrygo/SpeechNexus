"""Resolve live coordination facts and admit one synchronous ASR execution.

The private recipe supplies immutable authority (binding, manifest, runtime plan,
policy and stage shape). Quota version, pool owner/fencing token and the current
engine generation/version are mutable deployment facts and are read immediately
before the existing complete-recipe admission transaction. A concurrent change
therefore becomes an explicit admission conflict, never a fabricated identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.transcription import (
    AdmittedExecution,
    StagedAudio,
    TranscriptionOutcomeUnknownError,
    TranscriptionUnavailableError,
)
from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRequest,
    AdmitRecipe,
    StageAllocation,
)
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from speechnexus_backend.speech.domain.transcription import TranscriptionPlan
from speechnexus_backend.speech.infrastructure.admission_codec import (
    business_command_from_value,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    TranscriptionRecipe,
)


@dataclass(frozen=True, slots=True)
class LiveTranscriptionFacts:
    """The mutable rows admission is about to re-check and version-update."""

    now_us: int
    quota_version: int
    quota_policy_revision_id: str
    pool_version: int
    pool_service_class: str
    owner_id: str
    fencing_token: int
    instance_generation: int
    instance_version: int
    host_node_id: str
    gateway_id: str


class TranscriptionAdmissionFacts(Protocol):
    def read(
        self, plan: TranscriptionPlan, recipe: TranscriptionRecipe
    ) -> LiveTranscriptionFacts: ...


class PostgresTranscriptionAdmissionFacts:
    """One repeatable-read snapshot; the writing admission transaction revalidates it."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Transcription facts require a bounded read deadline")
        self._pool = pool
        self._timeout = float(timeout_seconds)

    def read(self, plan: TranscriptionPlan, recipe: TranscriptionRecipe) -> LiveTranscriptionFacts:
        binding = recipe.approved.stages[0].runtime_plan.binding
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                quota = connection.execute(
                    "SELECT version, policy_revision FROM workspace_quotas WHERE workspace_id = %s",
                    (plan.workspace_id,),
                ).fetchone()
                pool = connection.execute(
                    "SELECT version, service_class, owner_id, fencing_token "
                    "FROM resource_pools WHERE pool_id = %s",
                    (binding.pool_id,),
                ).fetchone()
                instance = connection.execute(
                    "SELECT h.current_generation, i.instance_generation, i.version, "
                    "i.host_node_id, i.pool_id, i.gateway_id, i.binding_revision_id, "
                    "i.manifest_id, i.approved_template_id, i.config_digest "
                    "FROM runtime_instance_heads AS h JOIN engine_instances AS i "
                    "ON i.instance_id = h.instance_id "
                    "AND i.instance_generation = h.current_generation "
                    "WHERE h.instance_id = %s",
                    (recipe.instance_id,),
                ).fetchone()
                clock = connection.execute(
                    "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) "
                    "AS now_us"
                ).fetchone()
        except Exception:
            raise TranscriptionUnavailableError(
                "Transcription admission facts are unavailable"
            ) from None
        if quota is None or pool is None or instance is None or clock is None:
            raise TranscriptionUnavailableError("Transcription admission facts are not provisioned")
        if (
            int(instance["current_generation"]) != int(instance["instance_generation"])
            or (instance["host_node_id"], instance["pool_id"], instance["gateway_id"])
            != (binding.target_node_id, binding.pool_id, binding.gateway_id)
            or instance["binding_revision_id"] != recipe.approved.binding_revision_id
            or instance["manifest_id"] != recipe.approved.manifest_id
            or instance["approved_template_id"] != binding.approved_template_id
            or instance["config_digest"] != binding.config_digest
            or quota["policy_revision"] != recipe.approved.policy_revision_id
            or pool["service_class"] != recipe.approved.service_class
            or pool["owner_id"] is None
            or int(pool["fencing_token"]) < 1
        ):
            raise TranscriptionUnavailableError(
                "Transcription admission facts disagree with the private recipe"
            )
        return LiveTranscriptionFacts(
            now_us=int(clock["now_us"]),
            quota_version=int(quota["version"]),
            quota_policy_revision_id=str(quota["policy_revision"]),
            pool_version=int(pool["version"]),
            pool_service_class=str(pool["service_class"]),
            owner_id=str(pool["owner_id"]),
            fencing_token=int(pool["fencing_token"]),
            instance_generation=int(instance["instance_generation"]),
            instance_version=int(instance["version"]),
            host_node_id=str(instance["host_node_id"]),
            gateway_id=str(instance["gateway_id"]),
        )


class PostgresTranscriptionAdmission:
    """Translate one plan into the existing complete-recipe admission command."""

    def __init__(
        self,
        facts: TranscriptionAdmissionFacts,
        service: AdmissionService,
        recipe: TranscriptionRecipe,
        *,
        max_command_bytes: int = 65_536,
        close: Callable[[], None] | None = None,
    ) -> None:
        if type(max_command_bytes) is not int or not 1_024 <= max_command_bytes <= 1_048_576:
            raise ValueError("Transcription business command limit must be bounded")
        self._facts = facts
        self._service = service
        self._recipe = recipe
        self._max_command_bytes = max_command_bytes
        self._close = close

    def admit(
        self, plan: TranscriptionPlan, *, submit_intent_id: str, audio: StagedAudio
    ) -> AdmittedExecution:
        if (
            plan.model != self._recipe.model_id
            or plan.binding_publication_id != self._recipe.binding_publication_id
            or audio.workspace_id != plan.workspace_id
        ):
            raise TranscriptionUnavailableError(
                "The private transcription recipe does not match the published binding"
            )
        facts = self._facts.read(plan, self._recipe)
        try:
            command, identity = self._command(plan, audio, facts)
        except ValueError, TypeError, KeyError, OverflowError:
            raise TranscriptionUnavailableError(
                "The private transcription recipe could not admit this request"
            ) from None
        result = self._service.admit(command)
        if result.outcome not in ("committed", "replayed"):
            if result.outcome == "unknown":
                # One read of the original identity is allowed; never submit a
                # second execution or create a fresh admission.
                result = self._service.query(identity)
            if result.outcome not in ("committed", "replayed"):
                code = result.error_code or result.outcome
                if result.outcome == "unknown":
                    raise TranscriptionOutcomeUnknownError(
                        "Transcription admission outcome is unknown"
                    )
                raise TranscriptionUnavailableError("Transcription admission was refused: " + code)
        if result.fact is None:
            raise TranscriptionOutcomeUnknownError("Admission returned no complete fact")
        stage = command.request.stages[0]
        authority = command.request.authorities[0]
        return AdmittedExecution(
            submit_intent_id=submit_intent_id,
            lease_id=stage.lease_id,
            operation_id=identity.operation_id,
            attempt_id=identity.attempt_id,
            stage_id=stage.stage_id,
            workspace_id=plan.workspace_id,
            pool_id=stage.pool_id,
            host_node_id=stage.host_node_id,
            gateway_id=stage.gateway_id,
            instance_id=stage.instance_id,
            instance_generation=stage.instance_generation,
            binding_revision_id=self._recipe.approved.binding_revision_id,
            policy_revision_id=self._recipe.approved.policy_revision_id,
            fencing_token=authority.fencing_token,
            deadline_at_us=command.request.deadline_at_us,
        )

    def close(self) -> None:
        if self._close is not None:
            self._close()
            self._close = None

    def _command(
        self, plan: TranscriptionPlan, audio: StagedAudio, facts: LiveTranscriptionFacts
    ) -> tuple[AdmitRecipe, AdmissionIdentity]:
        operation_id, attempt_id, job_id, event_id, event_digest = _identities(plan.request_id)
        identity = AdmissionIdentity(operation_id, attempt_id)
        deadline_at_us = min(
            facts.now_us + max(1, round(plan.timeout_seconds * 1_000_000)),
            self._recipe.approved.admission_expires_at_us,
        )
        stage = StageAllocation(
            self._recipe.stage_id,
            "lease-" + operation_id.removeprefix("operation-"),
            self._recipe.approved.stages[0].runtime_plan.binding.pool_id,
            facts.host_node_id,
            facts.gateway_id,
            self._recipe.instance_id,
            facts.instance_generation,
            facts.instance_version,
            self._recipe.reserved_slots,
        )
        business = business_command_from_value(
            {
                "kind": "asr_file",
                "request_id": plan.request_id,
                "workspace_id": plan.workspace_id,
                "model": plan.model,
                "binding_publication_id": plan.binding_publication_id,
                "audio": {
                    "asset_or_artifact_id": audio.asset_id,
                    "content_sha256": audio.content_sha256,
                    "byte_length": audio.byte_length,
                    "media_type": plan.audio_media_type,
                    "sample_rate_hz": plan.audio_sample_rate_hz,
                    "channels": plan.audio_channels,
                },
                "language": plan.language,
                "prompt": plan.prompt,
                "response_format": plan.response_format.value,
                "timestamp_granularities": list(plan.timestamp_granularities),
                "max_audio_ms": plan.max_audio_ms,
                "max_segment_ms": plan.max_segment_ms,
                "timeout_seconds": plan.timeout_seconds,
            },
            max_command_bytes=self._max_command_bytes,
        )
        request = AdmissionRequest(
            identity=identity,
            workspace_id=plan.workspace_id,
            binding_revision_id=self._recipe.approved.binding_revision_id,
            manifest_id=self._recipe.approved.manifest_id,
            recipe_digest=self._recipe.approved.recipe_digest,
            policy_revision_id=self._recipe.approved.policy_revision_id,
            expected_quota_version=facts.quota_version,
            service_class=self._recipe.approved.service_class,
            authorities=(
                OwnerAuthority(
                    self._recipe.approved.stages[0].runtime_plan.binding.pool_id,
                    facts.owner_id,
                    facts.fencing_token,
                ),
            ),
            stages=(stage,),
            deadline_at_us=deadline_at_us,
            job=CreateJob(
                job_id=job_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
                event_id=event_id,
                event_digest=event_digest,
                request_digest=business.request_digest,
            ),
        )
        return AdmitRecipe(request, business, self._recipe.approved), identity


def _identities(request_id: str) -> tuple[str, str, str, str, str]:
    source = ("synchronous-transcription:" + request_id).encode("utf-8")
    hexdigest = hashlib.sha256(source).hexdigest()[:48]
    event_digest = "sha256:" + hashlib.sha256(source + b":event").hexdigest()
    return (
        "operation-" + hexdigest,
        "attempt-" + hexdigest,
        "job-" + hexdigest,
        "event-" + hexdigest,
        event_digest,
    )


__all__ = [
    "LiveTranscriptionFacts",
    "PostgresTranscriptionAdmission",
    "PostgresTranscriptionAdmissionFacts",
    "TranscriptionAdmissionFacts",
]
