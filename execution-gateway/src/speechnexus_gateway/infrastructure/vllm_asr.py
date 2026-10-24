"""Fixed private vLLM speech endpoint with bounded, verified terminal SSE responses."""

from __future__ import annotations

import asyncio
import importlib
import io
import ipaddress
import json
import re
import secrets
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from speechnexus_gateway.domain.asr import (
    AsrAudio,
    AsrCallError,
    AsrFailure,
    AsrPolicy,
    AsrText,
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_EVENT_BYTES = 65536
_MAX_TEXT_BYTES = 65536
_MAX_EVENTS = 8192


@dataclass(frozen=True, slots=True)
class VllmAsrEndpoint:
    address: str = field(repr=False)
    port: int
    served_model: str = field(repr=False)
    binding_revision_id: str
    bearer_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.address) is not str:
            raise ValueError("ASR endpoint must use a configured private IP literal")
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError:
            raise ValueError("ASR endpoint must use a configured private IP literal") from None
        if not (
            address.is_loopback
            or (
                address.is_private
                and not address.is_unspecified
                and not address.is_multicast
                and not address.is_link_local
                and not address.is_reserved
            )
        ):
            raise ValueError("ASR endpoint must remain on the configured deployment network")
        if (
            type(self.port) is not int
            or not 1 <= self.port <= 65535
            or type(self.served_model) is not str
            or _ID.fullmatch(self.served_model) is None
            or type(self.binding_revision_id) is not str
            or _ID.fullmatch(self.binding_revision_id) is None
            or (
                self.bearer_token is not None
                and (
                    type(self.bearer_token) is not str
                    or not 1 <= len(self.bearer_token) <= 4096
                    or any(not 33 <= ord(char) <= 126 for char in self.bearer_token)
                )
            )
        ):
            raise ValueError("invalid configured ASR endpoint")

    @property
    def base_url(self) -> str:
        address = f"[{self.address}]" if ":" in self.address else self.address
        return f"http://{address}:{self.port}"


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
        self.done = False

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
        return AsrText(
            self._audio.request_id,
            "transcribe-" + self._audio.request_id,
            "".join(self._text),
            self._tokens,
            self._policy.digest,
        )


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
