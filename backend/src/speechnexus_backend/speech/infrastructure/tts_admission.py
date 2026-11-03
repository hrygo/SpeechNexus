"""Freeze and atomically admit one synchronous TTS execution and private command."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from speechnexus_contracts.execution import parse
from speechnexus_contracts.execution_models import SubmitExecutionRequest

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.speech_synthesis import (
    SpeechUnavailableError,
    SpeechUnsupportedError,
)
from speechnexus_backend.speech.application.transcription import (
    AdmittedExecution,
    TranscriptionUnavailableError,
)
from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRequest,
    AdmitRecipe,
    ApprovedAdmissionRecipe,
    StageAllocation,
)
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat, SpeechPlan
from speechnexus_backend.speech.domain.transcription import TranscriptionPlan
from speechnexus_backend.speech.infrastructure.admission_codec import business_command_from_value
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.transcription_admission import (
    LiveTranscriptionFacts,
    PostgresTranscriptionAdmissionFacts,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import TranscriptionRecipe
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit


@dataclass(frozen=True, slots=True)
class TtsRecipe:
    model_id: str
    stage_id: str
    instance_id: str
    reserved_slots: int
    approved: ApprovedAdmissionRecipe


class TtsFacts(Protocol):
    def read(self, plan: SpeechPlan, recipe: TtsRecipe) -> LiveTranscriptionFacts: ...


class PostgresTtsFacts:
    """Read the same live coordination snapshot as ASR; writing rechecks it."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        self._reader = PostgresTranscriptionAdmissionFacts(pool, timeout_seconds=timeout_seconds)

    def read(self, plan: SpeechPlan, recipe: TtsRecipe) -> LiveTranscriptionFacts:
        try:
            # That reader consumes only workspace_id, approved and instance_id;
            # both immutable input records expose those same attributes.
            return self._reader.read(
                cast(TranscriptionPlan, plan), cast(TranscriptionRecipe, recipe)
            )
        except TranscriptionUnavailableError:
            raise SpeechUnavailableError("Speech admission facts are unavailable") from None


class OriginalTtsCommands(Protocol):
    def read(self, identity: AdmissionIdentity) -> dict[str, Any] | None: ...


class PostgresOriginalTtsCommands:
    """Read the immutable admission body, checking its digest before recovery."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Recovery requires a bounded read")
        self._pool, self._timeout = pool, timeout_seconds

    def read(self, identity: AdmissionIdentity) -> dict[str, Any] | None:
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                row = connection.execute(
                    "SELECT request_document, request_digest, initial_attempt_id "
                    "FROM admission_operations WHERE operation_id=%s",
                    (identity.operation_id,),
                ).fetchone()
            if row is None:
                return None
            raw = row["request_document"]
            if (
                not isinstance(raw, str)
                or row["initial_attempt_id"] != identity.attempt_id
                or "sha256:" + hashlib.sha256(raw.encode()).hexdigest() != row["request_digest"]
            ):
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return cast(dict[str, Any], value)
        except Exception:
            raise SpeechUnavailableError("Original speech command is unavailable") from None


class PostgresTtsAdmission:
    """Return only a persisted command with its original admission identity."""

    def __init__(
        self,
        facts: TtsFacts,
        service: AdmissionService,
        originals: OriginalTtsCommands,
        recipe: TtsRecipe,
        *,
        max_command_bytes: int = 65536,
        default_language: str | None = None,
        close: Callable[[], None] | None = None,
    ) -> None:
        if not 1024 <= max_command_bytes <= 1048576:
            raise ValueError("Speech command limit must be bounded")
        self._facts, self._service, self._originals, self._recipe = (
            facts,
            service,
            originals,
            recipe,
        )
        self._max_bytes = max_command_bytes
        self._default_language = default_language
        self._close = close

    def close(self) -> None:
        if self._close is not None:
            close, self._close = self._close, None
            close()

    def admit(self, plan: SpeechPlan) -> SubmitExecutionRequest:
        identity, job_id, event_id, event_digest, submit_id = _identities(plan.request_id)
        original = self._originals.read(identity)
        if original is not None:
            if plan.language is None:
                try:
                    plan = replace(plan, language=original["plan"]["language"])
                except KeyError, TypeError, ValueError:
                    raise SpeechUnavailableError(
                        "Original speech language is unavailable"
                    ) from None
            return self._recover(plan, identity, original)
        if plan.language is None:
            if self._default_language is None:
                raise SpeechUnavailableError("Speech default language is not configured")
            try:
                plan = replace(plan, language=self._default_language)
            except ValueError:
                raise SpeechUnavailableError("Speech default language is invalid") from None
        if len(plan.text) > plan.planner_max_chars:
            raise SpeechUnsupportedError(
                "The file TTS planner cannot accept this input.", param="input"
            )
        recipe = self._recipe
        if (
            plan.model_id != recipe.model_id
            or plan.binding_revision_id != recipe.approved.binding_revision_id
        ):
            raise SpeechUnavailableError("Speech binding does not match admission recipe")
        facts = self._facts.read(plan, recipe)
        binding = recipe.approved.stages[0].runtime_plan.binding
        deadline = min(
            facts.now_us + max(1, round(plan.timeout_seconds * 1000000)),
            recipe.approved.admission_expires_at_us,
        )
        stage = StageAllocation(
            recipe.stage_id,
            "lease-" + identity.operation_id.removeprefix("operation-"),
            binding.pool_id,
            facts.host_node_id,
            facts.gateway_id,
            recipe.instance_id,
            facts.instance_generation,
            facts.instance_version,
            recipe.reserved_slots,
        )
        authority = OwnerAuthority(binding.pool_id, facts.owner_id, facts.fencing_token)
        admitted = AdmittedExecution(
            submit_id,
            stage.lease_id,
            identity.operation_id,
            identity.attempt_id,
            stage.stage_id,
            plan.workspace_id,
            stage.pool_id,
            stage.host_node_id,
            stage.gateway_id,
            stage.instance_id,
            stage.instance_generation,
            recipe.approved.binding_revision_id,
            recipe.approved.policy_revision_id,
            authority.fencing_token,
            deadline,
        )
        gateway = build_tts_submit(plan, admitted)
        business = business_command_from_value(
            {
                "kind": "tts_file",
                "plan": asdict(plan),
                "voice": {
                    "id": plan.voice_id,
                    "revision": plan.voice_revision,
                    "revision_id": plan.voice_revision_id,
                },
                "gateway_request": gateway,
            },
            max_command_bytes=self._max_bytes,
        )
        request = AdmissionRequest(
            identity,
            plan.workspace_id,
            recipe.approved.binding_revision_id,
            recipe.approved.manifest_id,
            recipe.approved.recipe_digest,
            recipe.approved.policy_revision_id,
            facts.quota_version,
            recipe.approved.service_class,
            (authority,),
            (stage,),
            deadline,
            CreateJob(
                job_id,
                identity.operation_id,
                identity.attempt_id,
                event_id,
                event_digest,
                business.request_digest,
            ),
        )
        result = self._service.admit(AdmitRecipe(request, business, recipe.approved))
        if result.outcome == "unknown":
            result = self._service.query(identity)
        if result.outcome not in ("committed", "replayed") or result.fact is None:
            raise SpeechUnavailableError("Speech admission outcome is unavailable")
        # Reload after commit so the returned command is exactly the stored body.
        original = self._originals.read(identity)
        if original is None:
            raise SpeechUnavailableError("Speech admission has no original command")
        return self._recover(plan, identity, original)

    def _recover(
        self, plan: SpeechPlan, identity: AdmissionIdentity, original: dict[str, Any]
    ) -> SubmitExecutionRequest:
        return OriginalTtsRecovery(self._originals, self._service)._recover(
            plan, identity, original
        )

    def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]:
        return OriginalTtsRecovery(self._originals, self._service).recover(request_id)


class OriginalTtsRecovery:
    """Recover a committed operation without mutable catalog, defaults or admission recipe."""

    def __init__(self, originals: OriginalTtsCommands, service: AdmissionService) -> None:
        self._originals, self._service = originals, service

    def admit(self, plan: SpeechPlan) -> SubmitExecutionRequest:
        raise SpeechUnavailableError("Recovery cannot admit new speech")

    def _recover(
        self, plan: SpeechPlan, identity: AdmissionIdentity, original: dict[str, Any]
    ) -> SubmitExecutionRequest:
        try:
            stored_plan = dict(original["plan"])
            stored_plan["response_format"] = SpeechFormat(stored_plan["response_format"])
            if original["kind"] != "tts_file" or SpeechPlan(**stored_plan) != plan:
                raise ValueError
            request = parse("SubmitExecutionRequest", original["gateway_request"])
            if (request["operation_id"], request["attempt_id"]) != (
                identity.operation_id,
                identity.attempt_id,
            ):
                raise ValueError
            result = self._service.query(identity)
            if result.outcome not in ("committed", "replayed") or result.fact is None:
                raise ValueError
            if len(result.fact.reservations) != 1:
                raise ValueError
            stage = result.fact.reservations[0].stage
            if any(
                request[key] != getattr(stage, key)
                for key in (
                    "stage_id",
                    "lease_id",
                    "pool_id",
                    "host_node_id",
                    "gateway_id",
                    "instance_id",
                    "instance_generation",
                )
            ):
                raise ValueError
            deadline = datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
            delta = deadline - datetime(1970, 1, 1, tzinfo=UTC)
            deadline_us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
            admitted = AdmittedExecution(
                request["submit_intent_id"],
                request["lease_id"],
                request["operation_id"],
                request["attempt_id"],
                request["stage_id"],
                request["workspace_id"],
                request["pool_id"],
                request["host_node_id"],
                request["gateway_id"],
                request["instance_id"],
                request["instance_generation"],
                request["binding_revision_id"],
                request["policy_revision_id"],
                request["fencing_token"],
                deadline_us,
            )
            if (
                request != build_tts_submit(plan, admitted)
                or request["submit_intent_id"] != _identities(plan.request_id)[4]
            ):
                raise ValueError
            return request
        except ValueError, TypeError, KeyError:
            raise SpeechUnavailableError("Original speech command cannot be recovered") from None

    def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]:
        """Recover after restart without re-resolving current publications or defaults."""
        identity, _, _, _, _ = _identities(request_id)
        original = self._originals.read(identity)
        if original is None:
            raise SpeechUnavailableError("Original speech admission is unavailable")
        try:
            value = dict(original["plan"])
            value["response_format"] = SpeechFormat(value["response_format"])
            plan = SpeechPlan(**value)
            if plan.request_id != request_id:
                raise ValueError
            return plan, self._recover(plan, identity, original)
        except ValueError, TypeError, KeyError:
            raise SpeechUnavailableError("Original speech plan cannot be recovered") from None


def _identities(request_id: str) -> tuple[AdmissionIdentity, str, str, str, str]:
    source = ("synchronous-speech:" + request_id).encode()
    key = hashlib.sha256(source).hexdigest()[:48]
    return (
        AdmissionIdentity("operation-" + key, "attempt-" + key),
        "job-" + key,
        "event-" + key,
        "sha256:" + hashlib.sha256(source + b":event").hexdigest(),
        "submit-" + key,
    )
