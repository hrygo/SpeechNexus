"""Actual TCP proves fixed routing, multipart transfer and environment proxy isolation."""

import asyncio
from contextlib import suppress

import pytest

from speechnexus_gateway.domain.asr import AsrCallError, AsrPolicy
from speechnexus_gateway.infrastructure.vllm_asr import VllmAsrEndpoint, VllmAsrRecognizer
from tests.unit.gateway.test_vllm_asr import AUDIO, response_bytes


@pytest.mark.parametrize("redirect", [False, True])
async def test_real_tcp_uses_only_the_configured_endpoint_and_never_redirects(
    monkeypatch: pytest.MonkeyPatch, redirect: bool
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    requests: list[tuple[bytes, bytes]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            values = dict(
                line.lower().split(b":", 1) for line in headers.split(b"\r\n")[1:] if b":" in line
            )
            length = int(values[b"content-length"].strip())
            body = await reader.readexactly(length)
            requests.append((headers, body))
            content = response_bytes()
            status = b"307 Temporary Redirect" if redirect else b"200 OK"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: text/event-stream\r\nX-Request-ID: request-1\r\n"
                + b"Location: /another-engine\r\n"
                + f"Content-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
                + content
            )
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = VllmAsrRecognizer(VllmAsrEndpoint("127.0.0.1", port, "synthetic-asr", "binding-1"))
        if redirect:
            with pytest.raises(AsrCallError):
                await client.transcribe(
                    AUDIO, AsrPolicy("config-1", language="en"), cancelled=lambda: False
                )
        else:
            result = await client.transcribe(
                AUDIO, AsrPolicy("config-1", language="en"), cancelled=lambda: False
            )
            assert result.text == "合成 synthetic."
        assert len(requests) == 1
        headers, body = requests[0]
        assert headers.startswith(b"POST /v1/audio/transcriptions HTTP/1.1\r\n")
        assert b'name="language"\r\n\r\nen' in body and b"RIFF" in body
