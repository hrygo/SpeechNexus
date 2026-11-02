"""Synchronous TTS using a committed private command and original gateway identity."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from speechnexus_contracts.execution_canonical import effective_execution_request
from speechnexus_contracts.execution_models import ArtifactStorageReference, SubmitExecutionRequest

from speechnexus_backend.speech.application.speech_synthesis import (
    SpeechDeadlineExceededError,
    SpeechEngineError,
    SpeechOutputInvalidError,
    SpeechUnavailableError,
)
from speechnexus_backend.speech.domain.speech_synthesis import SpeechAudio, SpeechPlan
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayConnectionError,
    GatewayExecutionClient,
    GatewayRejectedError,
    GatewayResponseError,
    GatewayResultObjectError,
    GatewayServerError,
    GatewayTimeoutError,
)
from speechnexus_backend.speech.infrastructure.gateway_transcription import _GatewayLoop
from speechnexus_backend.speech.infrastructure.transcription_submit import command_digest


class TtsAdmission(Protocol):
    def admit(self, plan: SpeechPlan) -> SubmitExecutionRequest: ...

    def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]: ...


class TtsResultObjects(Protocol):
    def read(self, reference: ArtifactStorageReference, storage_version: str) -> bytes: ...


class TtsCancellationStore(Protocol):
    def request_cancellation(self, operation_id: str, reason: Any) -> None: ...


class GatewaySynthesizer:
    """Recover using the persisted submit identity; never release held capacity."""

    def __init__(
        self,
        gateway: GatewayExecutionClient,
        admission: TtsAdmission,
        results: TtsResultObjects,
        *,
        poll_seconds: float = 0.25,
        cancellation: TtsCancellationStore | None = None,
        settlement_runtime: Any | None = None,
    ) -> None:
        if not 0 < poll_seconds <= 30:
            raise ValueError("Speech polling must be bounded")
        self._gateway, self._admission, self._results = gateway, admission, results
        self._poll_seconds = float(poll_seconds)
        self._cancellation = cancellation
        self._settlement_runtime = settlement_runtime
        self._bridge = _GatewayLoop(gateway)

    def close(self) -> None:
        try:
            for resource in (self._admission, self._results):
                closer = getattr(resource, "close", None)
                if callable(closer):
                    closer()
        finally:
            try:
                self._bridge.close()
            finally:
                if self._settlement_runtime is not None:
                    self._settlement_runtime.close()

    def cancel(self, request_id: str, reason: str) -> None:
        from speechnexus_backend.speech.infrastructure.tts_admission import _identities

        if self._cancellation is None:
            raise SpeechUnavailableError("Speech cancellation is unavailable")
        if reason not in ("client_cancelled", "stream_disconnected", "deadline_exceeded"):
            raise ValueError("Invalid speech cancellation reason")
        try:
            self._cancellation.request_cancellation(_identities(request_id)[0].operation_id, reason)
        except Exception:
            raise SpeechUnavailableError("Speech cancellation is unavailable") from None

    def synthesize(self, plan: SpeechPlan) -> SpeechAudio:
        started = time.monotonic()
        return cast(SpeechAudio, self._bridge.call(lambda: self._run(plan, started)))

    def recover(self, request_id: str, *, timeout_seconds: float) -> SpeechAudio:
        started = time.monotonic()
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600
        ):
            raise ValueError("Speech recovery requires a bounded timeout")
        return cast(
            SpeechAudio,
            self._bridge.call(lambda: self._recover(request_id, started, float(timeout_seconds))),
        )

    async def _recover(
        self, request_id: str, started: float, timeout_seconds: float
    ) -> SpeechAudio:
        deadline = started + timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                plan, request = await asyncio.to_thread(self._admission.recover, request_id)
                return await self._execute(
                    plan, request, min(deadline, started + plan.timeout_seconds)
                )
        except TimeoutError:
            raise SpeechDeadlineExceededError from None

    async def _run(self, plan: SpeechPlan, started: float) -> SpeechAudio:
        deadline = started + plan.timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                request = await asyncio.to_thread(self._admission.admit, plan)
                return await self._execute(plan, request, deadline)
        except TimeoutError, SpeechDeadlineExceededError:
            if self._cancellation is not None:
                await asyncio.to_thread(self.cancel, plan.request_id, "deadline_exceeded")
            raise SpeechDeadlineExceededError from None

    async def _execute(
        self, plan: SpeechPlan, request: SubmitExecutionRequest, deadline: float
    ) -> SpeechAudio:
        try:
            at = datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError
            remaining = (at - datetime.now(UTC)).total_seconds()
            deadline = min(deadline, time.monotonic() + remaining)
            if deadline <= time.monotonic():
                raise SpeechDeadlineExceededError
            async with asyncio.timeout_at(deadline):
                return await self._drive(plan, request, deadline)
        except TimeoutError:
            raise SpeechDeadlineExceededError from None
        except ValueError:
            raise SpeechUnavailableError("Stored speech deadline is invalid") from None

    async def _drive(
        self, plan: SpeechPlan, request: SubmitExecutionRequest, deadline: float
    ) -> SpeechAudio:
        submit_id = request["submit_intent_id"]
        try:
            try:
                status: Mapping[str, Any] = await self._gateway.query(submit_id)
            except GatewayRejectedError as error:
                if error.status_code != 404:
                    raise
                # Only replay the complete command committed with this same identity.
                if time.monotonic() >= deadline:
                    raise SpeechDeadlineExceededError from None
                if plan.gateway_policy_digest is None or plan.native_speaker_id is None:
                    raise SpeechUnavailableError("Stored speech authority is unavailable") from None
                evidence = await self._gateway.query_tts_configuration_evidence()
                if (
                    evidence["gateway_policy_digest"] != plan.gateway_policy_digest
                    or evidence["native_speaker_id"] != plan.native_speaker_id
                    or evidence["voice_id"] != plan.voice_id
                    or evidence["voice_revision"] != str(plan.voice_revision)
                    or evidence["voice_revision_id"] != plan.voice_revision_id
                    or evidence["policy_revision_id"] != request["policy_revision_id"]
                    or evidence["binding_revision_id"] != request["binding_revision_id"]
                    or evidence["node_id"] != request["host_node_id"]
                    or evidence["gateway_id"] != request["gateway_id"]
                    or evidence["instance_id"] != request["instance_id"]
                    or evidence["instance_generation"] != request["instance_generation"]
                ):
                    raise SpeechUnavailableError(
                        "Gateway speech authority is inconsistent"
                    ) from None
                status = await self._gateway.submit(request)
            while True:
                if time.monotonic() >= deadline:
                    raise SpeechDeadlineExceededError
                if (
                    status.get("submit_intent_id") != submit_id
                    or status.get("command_digest") != request["command_digest"]
                    or any(
                        status.get(k) != request[k]
                        for k in (
                            "lease_id",
                            "operation_id",
                            "attempt_id",
                            "stage_id",
                            "workspace_id",
                            "pool_id",
                            "host_node_id",
                            "gateway_id",
                            "instance_id",
                            "instance_generation",
                            "binding_revision_id",
                            "policy_revision_id",
                            "voice_revision_id",
                            "fencing_token",
                        )
                    )
                ):
                    raise SpeechUnavailableError("Speech execution identity is ambiguous")
                state = status.get("execution_state")
                if state == "succeeded":
                    break
                if state in ("failed", "aborted"):
                    raise SpeechEngineError("Speech execution ended without audio")
                if state == "unknown_quarantined":
                    raise SpeechUnavailableError("Speech execution outcome is unknown")
                await asyncio.sleep(self._poll_seconds)
                status = await self._gateway.query(submit_id)
            receipt = await self._gateway.audio_staging_result(submit_id)
            if time.monotonic() >= deadline:
                raise SpeechDeadlineExceededError
            staging = receipt.get("staging_result")
            if (
                receipt.get("submit_intent_id") != submit_id
                or not isinstance(staging, Mapping)
                or receipt.get("request_digest") != staging.get("request_digest")
                or receipt.get("request_digest")
                != command_digest(effective_execution_request(request))
                or staging["reference"]["workspace_id"] != plan.workspace_id
            ):
                raise SpeechUnavailableError("Speech result identity is ambiguous")
            audio = staging["audio"]
            if staging["media_type"] != plan.response_format.media_type:
                raise SpeechOutputInvalidError("Speech result format differs from the request")
            if (
                audio["sample_rate_hz"] != plan.sample_rate_hz
                or audio["channels"] != plan.channels
                or audio["duration_ms"] > plan.max_audio_ms
                or staging["reference"]["byte_length"] > plan.max_payload_bytes
            ):
                raise SpeechOutputInvalidError("Speech result exceeds the request bounds")
            payload = await asyncio.to_thread(
                self._results.read, staging["reference"], staging["storage_version"]
            )
            if time.monotonic() >= deadline:
                raise SpeechDeadlineExceededError
            return SpeechAudio(
                payload,
                plan.response_format,
                plan.sample_rate_hz,
                plan.channels,
                audio["duration_ms"],
            )
        except (
            GatewayConnectionError,
            GatewayTimeoutError,
            GatewayServerError,
            GatewayResponseError,
            GatewayRejectedError,
            GatewayResultObjectError,
        ):
            raise SpeechUnavailableError("Speech execution outcome is unknown") from None
        except ValueError, TypeError, KeyError:
            raise SpeechOutputInvalidError("Speech result is invalid") from None
