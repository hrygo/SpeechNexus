"""Fixed local/intranet OpenAI-compatible *text-only* non-streaming subset.

Only numeric loopback/RFC1918/ULA endpoints and /v1/chat/completions are supported.
Qualification references are operator inputs, not proof of network containment.
The deployment must constrain application, proxy, LLM and telemetry egress and
qualify any shared physical budget before enabling this adapter in production.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

import httpx

from speechnexus_backend.workbench.application.llm import (
    LlmCompletion,
    LlmError,
    LlmProfile,
    LlmRequest,
    LlmUsage,
    valid_text,
)

_PRIVATE_CALL: ContextVar[bool] = ContextVar("workbench_llm_private_transport", default=False)


class _PrivateTransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_CALL.get()


_TRANSPORT_FILTER = _PrivateTransportFilter()
# Producer logger names in the locked HTTPX/HTTPCore versions. Parent logger
# filters do not apply to propagated child records, so cover every producer.
_TRANSPORT_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


@contextmanager
def _private_transport_logs() -> Iterator[None]:
    # Permanent, idempotent filters; never toggle global levels/handlers. Context
    # isolation preserves logs from unrelated concurrent requests and tasks.
    for name in _TRANSPORT_LOGGERS:
        logging.getLogger(name).addFilter(_TRANSPORT_FILTER)
    token = _PRIVATE_CALL.set(True)
    try:
        yield
    finally:
        _PRIVATE_CALL.reset(token)


def _safe_tls(context: ssl.SSLContext | None) -> bool:
    return context is None or (
        isinstance(context, ssl.SSLContext)
        and context.check_hostname
        and context.verify_mode == ssl.CERT_REQUIRED
    )


def _endpoint(value: str) -> bool:
    try:
        url = urlsplit(value)
        address = ipaddress.ip_address(url.hostname or "")
        allowed = address.is_loopback or any(
            address in ipaddress.ip_network(network)
            for network in (
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "fc00::/7",
            )
        )
        return (
            allowed
            and url.scheme in {"http", "https"}
            and (address.is_loopback or url.scheme == "https")
            and url.username is None
            and url.password is None
            and url.path == "/v1/chat/completions"
            and not url.query
            and not url.fragment
            and not any(character.isspace() or ord(character) < 32 for character in value)
            and "%" not in value
            and (url.port is None or 1 <= url.port <= 65535)
            and "?" not in value
            and "#" not in value
        )
    except ValueError, TypeError:
        return False


@dataclass(frozen=True, slots=True)
class TextLlmSettings:
    """Trusted server composition only; never parse these fields from a user request."""

    enabled: bool = False
    endpoint: str | None = field(default=None, repr=False)
    profile: LlmProfile | None = None
    api_key: str | None = field(default=None, repr=False)
    qualification_reference: str | None = field(default=None, repr=False)
    deadline_seconds: float = 30
    max_response_bytes: int = 262_144
    tls_context: ssl.SSLContext | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.enabled) is not bool
            or not _safe_tls(self.tls_context)
            or type(self.deadline_seconds) not in {float, int}
            or not math.isfinite(self.deadline_seconds)
            or not 0 < self.deadline_seconds <= 120
            or type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= 8_000_000
            or (
                self.api_key is not None
                and (
                    not valid_text(self.api_key, 4096)
                    or not self.api_key.isascii()
                    or any(ord(c) < 33 or ord(c) == 127 for c in self.api_key)
                )
            )
            or (
                self.enabled
                and (
                    not isinstance(self.endpoint, str)
                    or not _endpoint(self.endpoint)
                    or self.profile is None
                    or not isinstance(self.qualification_reference, str)
                    or not valid_text(self.qualification_reference, 256)
                )
            )
        ):
            raise ValueError("invalid configured text LLM")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("non-JSON number")


def _completion(raw: bytes, profile: LlmProfile) -> LlmCompletion:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(value, dict) or value.get("model") != profile.model:
            raise ValueError("wrong model")
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("expected one choice")
        choice = choices[0]
        if (
            not isinstance(choice, dict)
            or choice.get("finish_reason") != "stop"
            or type(choice.get("index")) is not int
            or choice["index"] != 0
        ):
            raise ValueError("incomplete choice")
        message = choice.get("message")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), str)
            or any(key in message for key in ("tool_calls", "function_call"))
            or message.get("refusal") is not None
        ):
            raise ValueError("expected plain assistant text")
        usage = None
        if value.get("usage") is not None:
            reported = value["usage"]
            if not isinstance(reported, dict):
                raise ValueError("invalid usage")
            input_tokens, output_tokens = (
                reported.get("prompt_tokens"),
                reported.get("completion_tokens"),
            )
            if type(input_tokens) is not int or type(output_tokens) is not int:
                raise ValueError("invalid usage tokens")
            usage = LlmUsage(input_tokens, output_tokens)
            if output_tokens > profile.max_output_tokens:
                raise ValueError("completion exceeded configured token budget")
            total = reported.get("total_tokens")
            if type(total) is not int or total != usage.input_tokens + usage.output_tokens:
                raise ValueError("invalid usage total")
        return LlmCompletion(profile, message["content"], usage)
    except ValueError, TypeError, RecursionError:
        raise LlmError("invalid_response") from None


class OpenAiTextLlm:
    """One HTTP attempt with an owned client; no environment proxy, retries or fallback.

    A factory may only be injected by trusted composition (tests use synthetic
    transports). Each call owns/closes its own transport. No client or URL is
    accepted by a generation command. TLS trust remains server configuration.
    """

    def __init__(
        self,
        settings: TextLlmSettings | None = None,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._settings = settings if settings is not None else TextLlmSettings()
        self._transport_factory = transport_factory

    @property
    def binding_id(self) -> str | None:
        if self.profile is None:
            return None
        # No endpoint/credential is exposed or persisted. Credential rotation keeps
        # the same service identity; changing service/profile needs a new intent.
        raw = json.dumps(
            [self._settings.endpoint, self._settings.qualification_reference, asdict(self.profile)],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
        return "sha256:" + sha256(raw).hexdigest()

    @property
    def profile(self) -> LlmProfile | None:
        return self._settings.profile if self._settings.enabled else None

    async def generate(self, request: LlmRequest) -> LlmCompletion:
        configured = self._settings
        profile = self.profile
        if profile is None:
            raise LlmError("disabled")
        if request.profile != profile:
            raise LlmError("profile_mismatch")
        if not _safe_tls(configured.tls_context):
            # SSLContext is mutable even inside a frozen settings value.
            raise LlmError("unavailable")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if configured.api_key is not None:
            headers["Authorization"] = "Bearer " + configured.api_key
        try:
            with _private_transport_logs():
                async with (
                    asyncio.timeout(configured.deadline_seconds),
                    httpx.AsyncClient(
                        trust_env=False,
                        follow_redirects=False,
                        timeout=min(15, configured.deadline_seconds),
                        transport=self._transport_factory()
                        if self._transport_factory is not None
                        else None,
                        verify=configured.tls_context
                        if configured.tls_context is not None
                        else True,
                    ) as client,
                ):
                    # Endpoint and profile were fixed/validated by server configuration.
                    assert configured.endpoint is not None
                    async with client.stream(
                        "POST",
                        configured.endpoint,
                        headers=headers,
                        json={
                            "model": profile.model,
                            "messages": [
                                {"role": "system", "content": profile.system_prompt},
                                {"role": "user", "content": request.text},
                            ],
                            "temperature": 0,
                            "max_tokens": profile.max_output_tokens,
                            "stream": False,
                            "n": 1,
                        },
                    ) as response:
                        if response.status_code != 200:
                            raise LlmError("unavailable")
                        if (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                            != "application/json"
                            or response.headers.get("content-encoding", "identity").strip().lower()
                            != "identity"
                            or "content-range" in response.headers
                        ):
                            raise LlmError("invalid_response")
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(chunk) > configured.max_response_bytes - len(buffer):
                                raise LlmError("invalid_response")
                            buffer.extend(chunk)
                        return _completion(bytes(buffer), profile)
        except httpx.HTTPError, httpx.InvalidURL, TimeoutError:
            raise LlmError("unavailable") from None
