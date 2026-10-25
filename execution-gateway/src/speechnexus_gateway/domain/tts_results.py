"""Bounded encoded TTS output, separate from metering, stopping and publication."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from speechnexus_gateway.domain.execution_inputs import ExecutionObjectReference
from speechnexus_gateway.domain.execution_results import (
    MAX_AUDIO_RESULT_BYTES,
    ExecutionAudioMetadata,
    ExecutionAudioStagingResult,
    validate_audio_payload,
)
from speechnexus_gateway.domain.tts import TtsDelivery, TtsDeliveryFormat


def validate_tts_result_identity(workspace_id: str, request_digest: str) -> None:
    if (
        type(workspace_id) is not str
        or re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", workspace_id) is None
        or type(request_digest) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", request_digest) is None
    ):
        raise TtsResultError("tts_result_invalid")


class TtsResultError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class PreparedTtsResult:
    workspace_id: str
    request_digest: str
    body: bytes = field(repr=False)
    media_type: str
    audio: ExecutionAudioMetadata
    _content_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            validate_tts_result_identity(self.workspace_id, self.request_digest)
            if type(self.body) is not bytes or not 1 <= len(self.body) <= MAX_AUDIO_RESULT_BYTES:
                raise ValueError
            digest = "sha256:" + hashlib.sha256(self.body).hexdigest()
            object.__setattr__(self, "_content_sha256", digest)
            validate_audio_payload(
                media_type=self.media_type,
                audio=self.audio,
                byte_length=len(self.body),
                content_sha256=self._content_sha256,
            )
        except TypeError, ValueError:
            raise TtsResultError("tts_result_invalid") from None

    @classmethod
    def from_delivery(
        cls,
        *,
        workspace_id: str,
        request_digest: str,
        delivery: TtsDelivery,
        source_pcm_samples: int,
    ) -> PreparedTtsResult:
        if delivery.media_type != delivery.format.media_type:
            raise TtsResultError("tts_result_invalid")
        return cls(
            workspace_id,
            request_digest,
            delivery.payload,
            delivery.media_type,
            ExecutionAudioMetadata(
                delivery.sample_rate_hz,
                delivery.channels,
                source_pcm_samples,
                delivery.duration_ms,
                delivery.source_pcm_digest,
            ),
        )

    def reference(self, bucket: str) -> ExecutionObjectReference:
        digest = self._content_sha256
        token = hashlib.sha256((self.request_digest + "\n" + digest).encode("ascii")).hexdigest()
        extension = next(
            (target.value for target in TtsDeliveryFormat if target.media_type == self.media_type),
            None,
        )
        if extension is None:
            raise TtsResultError("tts_result_invalid")
        return ExecutionObjectReference(
            "tts-result-" + token,
            self.workspace_id,
            bucket,
            "private/execution-results/" + token + "/speech." + extension,
            digest,
            len(self.body),
        )

    def receipt(self, bucket: str, storage_version: str) -> ExecutionAudioStagingResult:
        return ExecutionAudioStagingResult(
            self.request_digest,
            self.reference(bucket),
            storage_version,
            self.media_type,
            self.audio,
        )


class TtsResultStore(Protocol):
    async def stage(
        self, result: PreparedTtsResult, *, cancelled: Callable[[], bool]
    ) -> ExecutionAudioStagingResult: ...
