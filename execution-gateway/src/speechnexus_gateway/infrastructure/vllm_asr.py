"""Fixed private vLLM speech endpoint with bounded, verified terminal SSE responses."""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import math
import re
import secrets
import wave
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from speechnexus_gateway.domain.asr import (
    AsrAudio,
    AsrCallError,
    AsrExecutionTarget,
    AsrFailure,
    AsrPolicy,
    AsrSegment,
    AsrText,
    AsrWord,
)
from speechnexus_gateway.infrastructure.private_engine_endpoint import (
    PrivateEngineEndpoint,
    load_controlled_endpoint_descriptor,
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_EVENT_BYTES = 65536
_MAX_TEXT_BYTES = 65536
_MAX_EVENTS = 8192


@dataclass(frozen=True, slots=True)
class VllmAsrEndpoint(PrivateEngineEndpoint):
    """A configured private IP literal for the fixed vLLM ASR endpoint."""

    execution_target: AsrExecutionTarget | None = None

    def __post_init__(self) -> None:
        PrivateEngineEndpoint.__post_init__(self)
        if self.execution_target is not None and (
            self.execution_target.binding_revision_id != self.binding_revision_id
            or self.bearer_token is None
            or re.fullmatch(r"[0-9a-f]{64}", self.bearer_token) is None
        ):
            raise ValueError("a controlled ASR target requires its native generation credential")


def load_controlled_asr_endpoint(path: Path) -> VllmAsrEndpoint:
    """Read one private, bounded deployment descriptor; caller input cannot select it."""
    try:
        descriptor = load_controlled_endpoint_descriptor(path)
        target = descriptor.target
        if set(target) != {item.name for item in fields(AsrExecutionTarget)}:
            raise ValueError
        return VllmAsrEndpoint(
            address=descriptor.address,
            port=descriptor.port,
            served_model=descriptor.served_model,
            binding_revision_id=target["binding_revision_id"],
            bearer_token=descriptor.bearer_token,
            execution_target=AsrExecutionTarget(**target),
        )
    except OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError:
        raise ValueError("invalid private controlled ASR deployment descriptor") from None


def _invalid() -> AsrCallError:
    return AsrCallError(AsrFailure.RESPONSE_INVALID, request_may_have_run=True)


def _limit() -> AsrCallError:
    return AsrCallError(AsrFailure.OUTPUT_LIMIT_EXCEEDED, request_may_have_run=True)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate ASR response field")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError("nonfinite ASR response value")


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise _invalid()
    return float(value)


class _TranscriptStream:
    def __init__(self, audio: AsrAudio, endpoint: VllmAsrEndpoint, policy: AsrPolicy) -> None:
        self._audio = audio
        self._endpoint = endpoint
        self._policy = policy
        self._buffer = bytearray()
        self._data: list[bytes] = []
        self._event_bytes = 0
        self._total_bytes = 0
        self._events = 0
        self._text_bytes = 0
        self._text: list[str] = []
        self._finish: str | None = None
        self._tokens: int | None = None
        self._language: str | None = None
        self._duration_ms: int | None = None
        self._segments: tuple[AsrSegment, ...] = ()
        self._words: tuple[AsrWord, ...] = ()
        self.done = False

    @property
    def _clip_ms(self) -> int:
        return round(len(self._audio.pcm16) / 2 * 1000 / 16000)

    def push(self, chunk: bytes) -> None:
        self._total_bytes += len(chunk)
        if self._total_bytes > _MAX_RESPONSE_BYTES:
            raise _limit()
        self._buffer.extend(chunk)
        while (end := self._buffer.find(b"\n")) >= 0:
            line = bytes(self._buffer[:end]).removesuffix(b"\r")
            del self._buffer[: end + 1]
            if len(line) > _MAX_EVENT_BYTES:
                raise _limit()
            if not line:
                if self._data:
                    self._event(b"\n".join(self._data))
                self._data.clear()
                self._event_bytes = 0
            elif line.startswith(b":"):
                continue
            elif line.startswith(b"data:") and not self.done:
                data = line[5:].removeprefix(b" ")
                self._event_bytes += len(data)
                if self._event_bytes > _MAX_EVENT_BYTES:
                    raise _limit()
                self._data.append(data)
            else:
                raise _invalid()
        if len(self._buffer) > _MAX_EVENT_BYTES:
            raise _limit()

    def _event(self, data: bytes) -> None:
        self._events += 1
        if self._events > _MAX_EVENTS:
            raise _limit()
        if data == b"[DONE]":
            if self._finish is None or self._tokens is None:
                raise _invalid()
            self.done = True
            return
        try:
            document = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
        except ValueError, UnicodeError, RecursionError:
            raise _invalid() from None
        if (
            not isinstance(document, dict)
            or document.get("id") != "transcribe-" + self._audio.request_id
            or document.get("model") != self._endpoint.served_model
            or document.get("object") != "transcription.chunk"
            or not isinstance(document.get("choices"), list)
        ):
            raise _invalid()
        self._metadata(document)
        choices = document["choices"]
        usage = document.get("usage")
        if not choices:
            if self._finish is None or self._tokens is not None or not isinstance(usage, dict):
                raise _invalid()
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            total = usage.get("total_tokens")
            if type(prompt) is not int or type(completion) is not int or type(total) is not int:
                raise _invalid()
            if min(prompt, completion, total) < 0 or total != prompt + completion:
                raise _invalid()
            if completion > self._policy.max_completion_tokens:
                raise AsrCallError(
                    AsrFailure.OUTPUT_LIMIT_EXCEEDED,
                    request_may_have_run=True,
                    completion_tokens=completion,
                )
            self._tokens = completion
            return
        if len(choices) != 1 or self._finish is not None or usage is not None:
            raise _invalid()
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
            raise _invalid()
        content = choice["delta"].get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _invalid()
            try:
                self._text_bytes += len(content.encode("utf-8"))
            except UnicodeError:
                raise _invalid() from None
            if self._text_bytes > _MAX_TEXT_BYTES:
                raise _limit()
            self._text.append(content)
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish not in ("stop", "length"):
                raise _invalid()
            self._finish = finish

    def result(self) -> AsrText:
        if not self.done or self._tokens is None or self._buffer or self._data:
            raise _invalid()
        if self._finish == "length":
            raise AsrCallError(
                AsrFailure.OUTPUT_TRUNCATED,
                request_may_have_run=True,
                completion_tokens=self._tokens,
            )
        try:
            return AsrText(
                self._audio.request_id,
                "transcribe-" + self._audio.request_id,
                "".join(self._text),
                self._tokens,
                self._policy.digest,
                language=self._language,
                duration_ms=self._duration_ms,
                segments=self._segments,
                words=self._words,
            )
        except ValueError:
            raise _invalid() from None

    def _metadata(self, document: dict[str, Any]) -> None:
        """Carry only native evidence supplied by the engine; never synthesize it."""
        language = document.get("language")
        if language is not None:
            if type(language) is not str or (
                self._language is not None and self._language != language
            ):
                raise _invalid()
            self._language = language
        if "duration" in document or "duration_ms" in document:
            duration = self._engine_duration_ms(document)
            if duration is not None:
                if self._duration_ms is not None and self._duration_ms != duration:
                    raise _invalid()
                self._duration_ms = duration
        if document.get("segments") is not None:
            segments = self._engine_segments(document["segments"])
            if self._segments and self._segments != segments:
                raise _invalid()
            self._segments = segments
        if document.get("words") is not None:
            words = self._engine_words(document["words"])
            if self._words and self._words != words:
                raise _invalid()
            self._words = words

    def _engine_duration_ms(self, document: dict[str, Any]) -> int | None:
        explicit = document.get("duration_ms")
        seconds = document.get("duration")
        values: list[int] = []
        if explicit is not None:
            if type(explicit) is not int or explicit <= 0:
                raise _invalid()
            values.append(explicit)
        if seconds is not None:
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                raise _invalid()
            values.append(round(seconds * 1000))
        if not values:
            return None
        if len(set(values)) != 1:
            raise _invalid()
        # vLLM's duration usage rounds up to whole seconds; anything wider
        # contradicts the clip actually sent to the engine.
        if abs(values[0] - self._clip_ms) > 1000:
            raise _invalid()
        return values[0]

    def _engine_segments(self, value: Any) -> tuple[AsrSegment, ...]:
        if not isinstance(value, list):
            raise _invalid()
        clip_seconds = self._clip_ms / 1000
        segments: list[AsrSegment] = []
        previous_end = 0.0
        for raw in value:
            if not isinstance(raw, dict):
                raise _invalid()
            start = _seconds(raw.get("start"))
            end = _seconds(raw.get("end"))
            text = raw.get("text")
            if (
                start is None
                or end is None
                or not 0 <= start < end
                or end > clip_seconds + 1e-6
                or start + 1e-9 < previous_end
                or type(text) is not str
                or "\x00" in text
            ):
                raise _invalid()
            start_ms = round(start * 1000)
            end_ms = round(end * 1000)
            if start_ms >= end_ms:
                raise _invalid()
            tokens: tuple[int, ...] = ()
            raw_tokens = raw.get("tokens")
            if raw_tokens is not None:
                if not isinstance(raw_tokens, list) or any(
                    type(token) is not int or token < 0 for token in raw_tokens
                ):
                    raise _invalid()
                tokens = tuple(raw_tokens)
            for name in ("id", "seek"):
                number = raw.get(name)
                if number is not None and (type(number) is not int or number < 0):
                    raise _invalid()
            for name in ("avg_logprob", "compression_ratio", "temperature"):
                number = raw.get(name)
                if number is not None and (
                    type(number) not in (int, float) or not math.isfinite(number)
                ):
                    raise _invalid()
            no_speech_prob = raw.get("no_speech_prob")
            if no_speech_prob is not None and (
                type(no_speech_prob) not in (int, float)
                or not math.isfinite(no_speech_prob)
                or not 0 <= no_speech_prob <= 1
            ):
                raise _invalid()
            try:
                segments.append(
                    AsrSegment(
                        start_ms,
                        end_ms,
                        text,
                        id=raw.get("id"),
                        avg_logprob=raw.get("avg_logprob"),
                        compression_ratio=raw.get("compression_ratio"),
                        no_speech_prob=no_speech_prob,
                        seek=raw.get("seek"),
                        temperature=raw.get("temperature"),
                        tokens=tokens,
                    )
                )
            except ValueError:
                raise _invalid() from None
            previous_end = end
        return tuple(segments)

    def _engine_words(self, value: Any) -> tuple[AsrWord, ...]:
        if not isinstance(value, list):
            raise _invalid()
        clip_seconds = self._clip_ms / 1000
        words: list[AsrWord] = []
        previous_start = 0
        for raw in value:
            if not isinstance(raw, dict):
                raise _invalid()
            word = raw.get("word")
            start = _seconds(raw.get("start"))
            end = _seconds(raw.get("end"))
            if (
                type(word) is not str
                or "\x00" in word
                or start is None
                or end is None
                or not 0 <= start < end
                or end > clip_seconds + 1e-6
            ):
                raise _invalid()
            start_ms = round(start * 1000)
            end_ms = round(end * 1000)
            if start_ms < previous_start or start_ms >= end_ms:
                raise _invalid()
            try:
                words.append(AsrWord(word, start_ms, end_ms))
            except ValueError:
                raise _invalid() from None
            previous_start = start_ms
        return tuple(words)


def _multipart(audio: AsrAudio, endpoint: VllmAsrEndpoint, policy: AsrPolicy) -> tuple[bytes, str]:
    boundary = "nexus-" + secrets.token_hex(24)
    fields = {
        "model": endpoint.served_model,
        "response_format": "json",
        "stream": "true",
        "stream_include_usage": "true",
        "stream_continuous_usage_stats": "false",
        "temperature": str(policy.temperature),
        "max_completion_tokens": str(policy.max_completion_tokens),
        "prompt": policy.prompt,
    }
    if policy.language != "auto":
        fields["language"] = policy.language
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
        for name, value in fields.items()
    ]
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(audio.pcm16)
    parts.extend(
        [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode(),
            output.getvalue(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class VllmAsrRecognizer:
    """One async call, with no retry or redirect. Closing transport never attests engine stop."""

    def __init__(self, endpoint: VllmAsrEndpoint, *, transport: Any = None) -> None:
        try:
            self._httpx: Any = importlib.import_module("httpx")
        except ImportError:
            raise ValueError("the isolated ASR HTTP runtime is unavailable") from None
        self._endpoint = endpoint
        self._transport = transport

    @property
    def binding_revision_id(self) -> str:
        return self._endpoint.binding_revision_id

    def validate_execution_target(self, target: AsrExecutionTarget) -> None:
        if self._endpoint.execution_target is None or self._endpoint.execution_target != target:
            raise ValueError("configured native ASR generation does not match the admitted target")

    async def transcribe(
        self, audio: AsrAudio, policy: AsrPolicy, *, cancelled: Callable[[], bool]
    ) -> AsrText:
        if cancelled():
            raise AsrCallError(AsrFailure.CANCELLED, request_may_have_run=False)
        started = False

        async def call() -> AsrText:
            nonlocal started
            body, content_type = _multipart(audio, self._endpoint, policy)
            headers = {
                "Content-Type": content_type,
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
                "X-Request-ID": audio.request_id,
            }
            if self._endpoint.bearer_token is not None:
                headers["Authorization"] = "Bearer " + self._endpoint.bearer_token
            async with self._httpx.AsyncClient(
                base_url=self._endpoint.base_url,
                timeout=self._httpx.Timeout(policy.timeout_seconds, connect=5),
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                started = True
                async with client.stream(
                    "POST", "/v1/audio/transcriptions", headers=headers, content=body
                ) as response:
                    if (
                        response.status_code != 200
                        or response.headers.get("x-request-id") != audio.request_id
                        or response.headers.get("content-type", "").split(";")[0]
                        != "text/event-stream"
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        raise _invalid()
                    parser = _TranscriptStream(audio, self._endpoint, policy)
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        parser.push(chunk)
                        if parser.done:
                            break
                    return parser.result()

        async def cancellation() -> None:
            # The synchronous execution owner exposes a polling hook, not an asyncio event.
            while not cancelled():  # noqa: ASYNC110
                await asyncio.sleep(0.025)

        work = asyncio.create_task(call())
        cancel = asyncio.create_task(cancellation())
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                done, _ = await asyncio.wait((work, cancel), return_when=asyncio.FIRST_COMPLETED)
                if cancel in done:
                    await cancel
                    raise AsrCallError(AsrFailure.CANCELLED, request_may_have_run=started)
                return await work
        except TimeoutError:
            raise AsrCallError(AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=started) from None
        except self._httpx.HTTPError:
            raise AsrCallError(AsrFailure.TRANSPORT_UNKNOWN, request_may_have_run=started) from None
        finally:
            work.cancel()
            cancel.cancel()
            await asyncio.gather(work, cancel, return_exceptions=True)
