"""Synthetic text only; no external LLM, credentials, model downloads or inference."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import ssl
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from speechnexus_backend.workbench.application.llm import (
    DisabledTextLlm,
    LlmError,
    LlmProfile,
    LlmRequest,
)
from speechnexus_backend.workbench.infrastructure.openai_text_llm import (
    OpenAiTextLlm,
    TextLlmSettings,
)

PROFILE = LlmProfile(model="synthetic-model", template_revision=1, system_prompt="合成纪要模板")
TEXT = "\ufeff合成正文\r\n🙂e\u0301 "


def settings(**changes: Any) -> TextLlmSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "endpoint": "http://127.0.0.1:8100/v1/chat/completions",
        "profile": PROFILE,
        "qualification_reference": "synthetic-test-only",
    }
    return TextLlmSettings(**(values | changes))


def body(**changes: Any) -> dict[str, Any]:
    return {
        "model": PROFILE.model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": TEXT}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
    } | changes


async def run_response(response: httpx.Response, **changes: Any) -> Any:
    llm = OpenAiTextLlm(
        settings(**changes), transport_factory=lambda: httpx.MockTransport(lambda _: response)
    )
    return await llm.generate(LlmRequest(PROFILE, TEXT))


async def test_default_disabled_never_connects() -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("disabled LLM connected")

    llm = OpenAiTextLlm(TextLlmSettings(), transport_factory=lambda: httpx.MockTransport(forbidden))
    assert llm.profile is None and DisabledTextLlm().profile is None
    for port in (llm, DisabledTextLlm()):
        with pytest.raises(LlmError, match="disabled") as caught:
            await port.generate(LlmRequest(PROFILE, TEXT))
        assert caught.value.code == "disabled"


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": None},
        {"profile": None},
        {"qualification_reference": None},
        {"endpoint": "https://api.example.test/v1/chat/completions"},
        {"endpoint": "https://8.8.8.8/v1/chat/completions"},
        {"endpoint": "http://10.0.0.1/v1/chat/completions"},
        {"endpoint": "https://169.254.169.254/v1/chat/completions"},
        {"endpoint": "https://0.0.0.0/v1/chat/completions"},
        {"endpoint": "https://[::ffff:8.8.8.8]/v1/chat/completions"},
        {"endpoint": "http://a:b@127.0.0.1/v1/chat/completions"},
        {"endpoint": "http://127.0.0.1/v1/chat/completions?key=synthetic"},
        {"endpoint": "http://127.0.0.1/v1/chat/completions#fragment"},
        {"endpoint": "http://127.0.0.1/other"},
        {"deadline_seconds": 0},
        {"deadline_seconds": float("nan")},
        {"deadline_seconds": 121},
        {"max_response_bytes": True},
        {"max_response_bytes": 0},
        {"max_response_bytes": 8_000_001},
        {"api_key": "synthetic\nsecret"},
        {"enabled": 1},
    ],
)
def test_invalid_configuration_rejected_without_echo(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid configured text LLM") as caught:
        settings(**changes)
    assert "synthetic" not in str(caught.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://10.0.0.1/v1/chat/completions",
        "https://172.16.0.1/v1/chat/completions",
        "https://192.168.0.1/v1/chat/completions",
        "https://[fd00::1]/v1/chat/completions",
        "http://[::1]:8100/v1/chat/completions",
    ],
)
def test_private_numeric_endpoints_supported(endpoint: str) -> None:
    assert settings(endpoint=endpoint).endpoint == endpoint


def test_durable_binding_identity_pins_endpoint_profile_but_allows_credential_rotation() -> None:
    original = OpenAiTextLlm(settings())
    assert (
        original.binding_id == OpenAiTextLlm(settings(api_key="synthetic-rotated-key")).binding_id
    )
    assert (
        original.binding_id
        != OpenAiTextLlm(settings(endpoint="http://127.0.0.2:8100/v1/chat/completions")).binding_id
    )
    assert (
        original.binding_id
        != OpenAiTextLlm(settings(profile=replace(PROFILE, template_revision=2))).binding_id
    )
    assert OpenAiTextLlm().binding_id is None and DisabledTextLlm().binding_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"model": ""},
        {"model": "x" * 129},
        {"model": "synthetic\n"},
        {"template_revision": True},
        {"template_revision": 0},
        {"template_revision": 10**19},
        {"system_prompt": ""},
        {"system_prompt": "x\x00"},
        {"system_prompt": "\ud800"},
        {"max_input_characters": 0},
        {"max_output_characters": 1_000_001},
        {"max_output_tokens": False},
        {"max_output_tokens": 32_769},
    ],
)
def test_profile_validation_is_strict(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid text LLM profile"):
        replace(PROFILE, **changes)


@pytest.mark.parametrize("text", ["", "  \n", "a\x00b", "\udfff", "x" * 100_001])
def test_input_bounded_before_connect(text: str) -> None:
    with pytest.raises(LlmError, match="invalid_input"):
        LlmRequest(PROFILE, text)


async def test_wire_is_fixed_and_lossless_and_sensitive_repr_hidden() -> None:
    seen: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert str(request.url) == "http://127.0.0.1:8100/v1/chat/completions"
        assert (
            request.method == "POST" and request.headers["authorization"] == "Bearer synthetic-key"
        )
        assert json.loads(request.content) == {
            "model": PROFILE.model,
            "messages": [
                {"role": "system", "content": PROFILE.system_prompt},
                {"role": "user", "content": TEXT},
            ],
            "temperature": 0,
            "max_tokens": 2048,
            "stream": False,
            "n": 1,
        }
        return httpx.Response(200, json=body())

    configured = settings(api_key="synthetic-key")
    port = OpenAiTextLlm(configured, transport_factory=lambda: httpx.MockTransport(serve))
    request = LlmRequest(PROFILE, TEXT)
    result = await port.generate(request)
    assert result.text == TEXT and result.profile == PROFILE
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (9, 5)
    assert len(seen) == 1
    for value in (configured, port, request, result, PROFILE):
        assert TEXT not in repr(value) and "synthetic-key" not in repr(value)
        assert PROFILE.system_prompt not in repr(value)
        assert "127.0.0.1" not in repr(value)


async def test_mismatched_profile_never_connects() -> None:
    port = OpenAiTextLlm(
        settings(),
        transport_factory=lambda: httpx.MockTransport(lambda _: pytest.fail("connected")),
    )
    with pytest.raises(LlmError, match="profile_mismatch"):
        await port.generate(LlmRequest(replace(PROFILE, template_revision=2), TEXT))


@pytest.mark.parametrize(
    "payload",
    [
        body(model="different"),
        body(choices=[]),
        body(choices=[{}, {}]),
        body(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": TEXT},
                    "finish_reason": "length",
                }
            ]
        ),
        body(
            choices=[
                {"index": 0, "message": {"role": "tool", "content": TEXT}, "finish_reason": "stop"}
            ]
        ),
        body(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": TEXT, "tool_calls": [{}]},
                    "finish_reason": "stop",
                }
            ]
        ),
        body(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": TEXT, "function_call": {}},
                    "finish_reason": "stop",
                }
            ]
        ),
        body(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "stop",
                }
            ]
        ),
        body(usage={"prompt_tokens": True, "completion_tokens": 5, "total_tokens": 6}),
        body(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 9}),
        body(usage={"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1}),
        body(
            usage={
                "prompt_tokens": 1_000_000_001,
                "completion_tokens": 2,
                "total_tokens": 1_000_000_003,
            }
        ),
        body(usage=[]),
        [],
        None,
    ],
)
async def test_rejects_unsupported_or_invalid_response(payload: Any) -> None:
    with pytest.raises(LlmError, match="invalid_response"):
        await run_response(httpx.Response(200, json=payload))


@pytest.mark.parametrize(
    "raw", [b'{"model":"x","model":"y"}', b'{"x":NaN}', b"\xff", b"[[[[", b"[]"]
)
async def test_invalid_json_rejected(raw: bytes) -> None:
    with pytest.raises(LlmError, match="invalid_response"):
        await run_response(
            httpx.Response(200, content=raw, headers={"content-type": "application/json"})
        )


async def test_missing_usage_is_unknown_not_zero() -> None:
    value = body()
    value.pop("usage")
    assert (await run_response(httpx.Response(200, json=value))).usage is None


async def test_reported_completion_tokens_cannot_exceed_fixed_budget() -> None:
    value = body(usage={"prompt_tokens": 9, "completion_tokens": 2049, "total_tokens": 2058})
    with pytest.raises(LlmError, match="invalid_response"):
        await run_response(httpx.Response(200, json=value))


@pytest.mark.parametrize("status", [206, 301, 302, 307, 400, 401, 429, 500])
async def test_http_failure_is_single_attempt_and_redacted(status: int) -> None:
    calls = 0

    def serve(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="synthetic-secret-error", headers={"location": "https://8.8.8.8/"}
        )

    with pytest.raises(LlmError, match="unavailable") as caught:
        await OpenAiTextLlm(
            settings(), transport_factory=lambda: httpx.MockTransport(serve)
        ).generate(LlmRequest(PROFILE, TEXT))
    assert calls == 1 and "synthetic-secret-error" not in str(caught.value)


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/html"},
        {"content-type": "application/json", "content-encoding": "gzip"},
        {"content-type": "application/json", "content-range": "bytes 0-3/9"},
    ],
)
async def test_unsupported_media_is_not_consumed(headers: dict[str, str]) -> None:
    class Unread(httpx.AsyncByteStream):
        def __aiter__(self) -> AsyncIterator[bytes]:
            pytest.fail("unsupported response body was read")

    with pytest.raises(LlmError, match="invalid_response"):
        await run_response(httpx.Response(200, stream=Unread(), headers=headers))


async def test_response_bytes_and_text_are_bounded() -> None:
    with pytest.raises(LlmError, match="invalid_response"):
        await run_response(httpx.Response(200, json=body()), max_response_bytes=10)
    value = body(
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "x" * 20_001},
                "finish_reason": "stop",
            }
        ]
    )
    with pytest.raises(LlmError, match="invalid_response"):
        await run_response(httpx.Response(200, json=value))


async def test_total_deadline_and_network_errors_redacted() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    port = OpenAiTextLlm(
        settings(deadline_seconds=0.01), transport_factory=lambda: httpx.MockTransport(slow)
    )
    with pytest.raises(LlmError, match="unavailable"):
        await port.generate(LlmRequest(PROFILE, TEXT))

    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic-private-url")

    with pytest.raises(LlmError, match="unavailable") as caught:
        await OpenAiTextLlm(
            settings(), transport_factory=lambda: httpx.MockTransport(fail)
        ).generate(LlmRequest(PROFILE, TEXT))
    assert "synthetic-private-url" not in str(caught.value) and caught.value.__suppress_context__


async def test_cancellation_propagates_and_closes_stream() -> None:
    entered, closed = asyncio.Event(), asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self) -> None:
            closed.set()

    port = OpenAiTextLlm(
        settings(),
        transport_factory=lambda: httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=Waiting(), headers={"content-type": "application/json"}
            )
        ),
    )
    task = asyncio.create_task(port.generate(LlmRequest(PROFILE, TEXT)))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_real_loopback_http_ignores_proxy_and_preserves_the_fixed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    finished = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            assert head.startswith("POST /v1/chat/completions HTTP/1.1\r\n")
            length = next(
                int(line.split(":", 1)[1])
                for line in head.split("\r\n")
                if line.lower().startswith("content-length:")
            )
            seen.append(json.loads(await reader.readexactly(length)))
            raw = json.dumps(body(), ensure_ascii=False).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(raw)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + raw
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        llm = OpenAiTextLlm(settings(endpoint=f"http://127.0.0.1:{port}/v1/chat/completions"))
        assert (await llm.generate(LlmRequest(PROFILE, TEXT))).text == TEXT
        await asyncio.wait_for(finished.wait(), 1)
    assert len(seen) == 1 and seen[0]["messages"][1]["content"] == TEXT


async def test_transport_logs_hide_provider_details_without_muting_other_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    entered, release = asyncio.Event(), asyncio.Event()

    async def serve(_: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug("synthetic-private-header")
        entered.set()
        await release.wait()
        return httpx.Response(500, extensions={"reason_phrase": b"synthetic-private-status"})

    port = OpenAiTextLlm(settings(), transport_factory=lambda: httpx.MockTransport(serve))
    task = asyncio.create_task(port.generate(LlmRequest(PROFILE, TEXT)))
    await asyncio.wait_for(entered.wait(), 1)
    logging.getLogger("httpx").info("unrelated-httpx-event")
    release.set()
    with pytest.raises(LlmError):
        await task
    logging.getLogger("httpx").info("after-call-event")
    assert "synthetic-private" not in caplog.text
    assert "127.0.0.1" not in caplog.text
    assert "unrelated-httpx-event" in caplog.text and "after-call-event" in caplog.text


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    binary = shutil.which("openssl")
    assert binary is not None, "openssl is required for synthetic TLS qualification tests"
    directory = tmp_path_factory.mktemp("synthetic-llm-tls")
    result: dict[str, tuple[Path, Path]] = {}
    for name, address in (("valid", "127.0.0.1"), ("wrong-san", "127.0.0.2")):
        certificate, key = directory / (name + ".pem"), directory / (name + ".key")
        subprocess.run(
            [
                binary,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=Synthetic LLM TLS Test Only",
                "-addext",
                f"subjectAltName=IP:{address}",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        result[name] = certificate, key
    return result


@asynccontextmanager
async def tls_server(
    certificate: Path, key: Path
) -> AsyncIterator[tuple[str, list[str], asyncio.Event]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    seen: list[str] = []
    entered, release, idle = asyncio.Event(), asyncio.Event(), asyncio.Event()
    idle.set()
    active = 0

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal active
        active += 1
        idle.clear()
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            length = next(
                int(line.split(":", 1)[1])
                for line in head.split("\r\n")
                if line.lower().startswith("content-length:")
            )
            text = json.loads(await reader.readexactly(length))["messages"][1]["content"]
            seen.append(text)
            if text == "synthetic-waiting":
                entered.set()
                await release.wait()
            raw = json.dumps(body()).encode()
            writer.write(
                b"HTTP/1.1 200 synthetic-private-status\r\n"
                b"Content-Type: application/json\r\nX-Private: synthetic-private-header\r\n"
                b"Content-Length: "
                + str(len(raw)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + raw
            )
            await writer.drain()
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            active -= 1
            if active == 0:
                idle.set()

    async with await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context) as server:
        endpoint = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions"
        try:
            yield endpoint, seen, entered
        finally:
            release.set()
            await asyncio.wait_for(idle.wait(), 2)


async def test_explicit_internal_ca_verifies_tls_across_concurrent_sequential_and_cancelled_calls(
    tls_files: dict[str, tuple[Path, Path]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    certificate, key = tls_files["valid"]
    context = ssl.create_default_context(cafile=certificate)
    async with tls_server(certificate, key) as (endpoint, seen, entered):
        port = OpenAiTextLlm(settings(endpoint=endpoint, tls_context=context))
        for _ in range(2):
            assert (await port.generate(LlmRequest(PROFILE, TEXT))).text == TEXT
        results = await asyncio.gather(
            *(port.generate(LlmRequest(PROFILE, TEXT)) for _ in range(2))
        )
        assert len(results) == 2
        task = asyncio.create_task(port.generate(LlmRequest(PROFILE, "synthetic-waiting")))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await port.generate(LlmRequest(PROFILE, TEXT))).text == TEXT
    assert len(seen) == 6
    assert "synthetic-private" not in caplog.text
    assert not [
        record
        for record in caplog.records
        if record.name == "httpx" or record.name.startswith("httpcore.")
    ]


@pytest.mark.parametrize("kind", ["untrusted", "wrong-san"])
async def test_untrusted_or_wrong_ip_certificate_is_rejected(
    tls_files: dict[str, tuple[Path, Path]], kind: str
) -> None:
    certificate, key = tls_files["valid" if kind == "untrusted" else "wrong-san"]
    context = (
        ssl.create_default_context()
        if kind == "untrusted"
        else ssl.create_default_context(cafile=certificate)
    )
    async with tls_server(certificate, key) as (endpoint, seen, _):
        with pytest.raises(LlmError, match="unavailable"):
            await OpenAiTextLlm(settings(endpoint=endpoint, tls_context=context)).generate(
                LlmRequest(PROFILE, TEXT)
            )
    assert not seen


def test_insecure_tls_context_cannot_be_configured_or_mutated_into_a_call() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="invalid configured text LLM"):
        settings(tls_context=context)


async def test_mutated_tls_context_is_rejected_before_connection() -> None:
    context = ssl.create_default_context()
    configured = settings(tls_context=context)
    context.check_hostname = False
    with pytest.raises(LlmError, match="unavailable"):
        await OpenAiTextLlm(configured).generate(LlmRequest(PROFILE, TEXT))
