"""Resolve the file input of the frozen original request, without dispatching."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputError,
    ExecutionInputLimits,
    ExecutionObjectInputs,
    ExecutionObjectReference,
    VerifiedExecutionObject,
)
from speechnexus_gateway.domain.media import AudioSourceFormat
from speechnexus_gateway.domain.submission import SubmitRequest


@dataclass(frozen=True, slots=True)
class VerifiedVoiceCloneInput:
    request: SubmitRequest = field(repr=False)
    reference: ExecutionObjectReference = field(repr=False)
    payload: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedFileAsrInput:
    request: SubmitRequest = field(repr=False)
    source_format: AudioSourceFormat
    object_input: VerifiedExecutionObject = field(repr=False)


def _reference(value: object) -> ExecutionObjectReference:
    if not isinstance(value, dict) or set(value) != {
        "asset_or_artifact_id",
        "workspace_id",
        "storage_bucket",
        "object_key",
        "content_sha256",
        "byte_length",
    }:
        raise ExecutionInputError("execution_input_invalid")
    return ExecutionObjectReference(**value)


class FileExecutionInputs:
    def __init__(self, objects: ExecutionObjectInputs) -> None:
        self._objects = objects

    @asynccontextmanager
    async def open_asr(
        self,
        request: SubmitRequest,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[VerifiedFileAsrInput]:
        """No inference, result publishing, defaults or execution/stop facts are produced."""
        try:
            command = cast(dict[str, Any], request.command)
            if (
                command["kind"] != "asr"
                or command["task_kind"] != "asr_file"
                or command["stage_role"] != "asr_batch"
            ):
                raise ValueError
            audio = command["audio"]
            reference = _reference(audio["storage"])
            if reference.workspace_id != request.identity.workspace_id:
                raise ExecutionInputError("execution_input_scope_mismatch")
            if reference.byte_length > limits.max_bytes:
                raise ExecutionInputError("execution_input_limit_exceeded")
            source_format = AudioSourceFormat(
                audio["media_type"], audio["sample_rate_hz"], audio["channels"]
            )
        except ExecutionInputError:
            raise
        except ValueError, TypeError, KeyError:
            raise ExecutionInputError("execution_input_invalid") from None
        async with self._objects.open_verified(
            reference, limits=limits, cancelled=cancelled
        ) as obj:
            if obj.reference != reference:
                raise ExecutionInputError("execution_input_identity_mismatch")
            yield VerifiedFileAsrInput(request, source_format, obj)

    async def _read_reference(
        self,
        request: SubmitRequest,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> VerifiedVoiceCloneInput:
        if reference.workspace_id != request.identity.workspace_id:
            raise ExecutionInputError("execution_input_scope_mismatch")
        if reference.byte_length > limits.max_bytes:
            raise ExecutionInputError("execution_input_limit_exceeded")
        async with self._objects.open_verified(
            reference, limits=limits, cancelled=cancelled
        ) as obj:
            if obj.reference != reference:
                raise ExecutionInputError("execution_input_identity_mismatch")
            payload = await asyncio.to_thread(obj.stream.read, reference.byte_length + 1)
            if type(payload) is not bytes or len(payload) != reference.byte_length:
                raise ExecutionInputError("execution_input_identity_mismatch")
            return VerifiedVoiceCloneInput(request, reference, payload)

    async def read_voice_clone(
        self,
        request: SubmitRequest,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> VerifiedVoiceCloneInput:
        """Read one bounded clone reference; no inference or execution facts."""
        try:
            command = cast(dict[str, Any], request.command)
            if (
                command["kind"] != "voiceclone"
                or command["task_kind"] != "voice_clone"
                or command["stage_role"] != "voice_clone_base"
            ):
                raise ValueError
            reference = _reference(command["reference_audio"]["storage"])
        except ExecutionInputError:
            raise
        except ValueError, TypeError, KeyError:
            raise ExecutionInputError("execution_input_invalid") from None
        return await self._read_reference(request, reference, limits=limits, cancelled=cancelled)

    async def read_voice_material(
        self,
        request: SubmitRequest,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> VerifiedVoiceCloneInput:
        """Read the custom voice material of one frozen file-TTS command."""
        try:
            command = cast(dict[str, Any], request.command)
            if (
                command["kind"] != "tts"
                or command["task_kind"] != "tts_file"
                or command["stage_role"] != "tts_connector"
            ):
                raise ValueError
            reference = _reference(command["resources"]["voice_material"])
        except ExecutionInputError:
            raise
        except ValueError, TypeError, KeyError:
            raise ExecutionInputError("execution_input_invalid") from None
        return await self._read_reference(request, reference, limits=limits, cancelled=cancelled)
