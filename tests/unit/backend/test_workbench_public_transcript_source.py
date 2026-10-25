"""Untrusted public Artifact metadata and raw content are bounded and verified."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError
from speechnexus_backend.workbench.infrastructure.public_transcript_source import (
    PublicTranscriptSource,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)
TEXT = "  合成转写\n"
CONTENT = TEXT.encode()


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, stall: bool = False) -> None:
        self.chunks, self.stall = chunks, stall
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class Service:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {
            "id": "artifact-source",
            "object": "nexus.artifact",
            "workspace_id": "workspace-one",
            "kind": "transcript",
            "status": "ready",
            "media_type": "text/plain; charset=utf-8",
            "bytes": len(CONTENT),
            "sha256": "sha256:" + hashlib.sha256(CONTENT).hexdigest(),
            "receipt_id": "receipt-source",
            "created_at": "2026-10-23T00:00:00Z",
        }
        self.metadata_raw: bytes | None = None
        self.content = Stream([CONTENT[:4], CONTENT[4:]])
        self.status = 200
        self.headers = {"content-type": "application/octet-stream", "content-length": "1"}
        self.requests: list[httpx.Request] = []
        self.metadata_stream: Stream | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "speech.invalid"
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path.endswith("/content"):
            return httpx.Response(self.status, headers=self.headers, stream=self.content)
        raw = (
            self.metadata_raw
            if self.metadata_raw is not None
            else json.dumps(self.metadata).encode()
        )
        self.metadata_stream = Stream([raw])
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=self.metadata_stream
        )


async def read(source: PublicTranscriptSource) -> str:
    return await source.read(
        artifact_id="artifact-source", receipt_id="receipt-source", workspace_id="workspace-one"
    )


async def test_generated_metadata_and_raw_text_do_not_trust_content_length() -> None:
    service = Service()
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        source = PublicTranscriptSource(client, now=lambda: NOW)
        assert await read(source) == TEXT
        assert not client.is_closed
    assert [r.url.path for r in service.requests] == [
        "/v1/nexus/artifacts/artifact-source",
        "/v1/nexus/artifacts/artifact-source/content",
    ]
    assert (
        service.content.closed
        and service.metadata_stream is not None
        and service.metadata_stream.closed
    )


@pytest.mark.parametrize("media_type", ["application/json", "application/json; charset=utf-8"])
async def test_imports_the_generated_transcription_shape_without_changing_text(
    media_type: str,
) -> None:
    service = Service()
    text = "  合成 e\u0301 😀\n "
    content = json.dumps({"text": text}, ensure_ascii=False).encode()
    service.metadata.update(
        media_type=media_type,
        bytes=len(content),
        sha256="sha256:" + hashlib.sha256(content).hexdigest(),
    )
    service.content = Stream([content[:7], content[7:]])
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        assert await read(PublicTranscriptSource(client, now=lambda: NOW)) == text
    assert service.content.closed


@pytest.mark.parametrize(
    "content",
    [
        b"[]",
        b"{}",
        b'{"text":1}',
        b'{"text":"x","extra":true}',
        b'{"text":"a","text":"b"}',
        b'{"text":"\\u0000"}',
        b'{"text":"\\ud800"}',
        b'{"text":"unfinished}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
async def test_rejects_invalid_json_transcription_before_import(content: bytes) -> None:
    service = Service()
    service.metadata.update(
        media_type="application/json",
        bytes=len(content),
        sha256="sha256:" + hashlib.sha256(content).hexdigest(),
    )
    service.content = Stream([content])
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        with pytest.raises(TranscriptSourceError, match=r"^source_invalid$"):
            await read(PublicTranscriptSource(client, now=lambda: NOW))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("id", "artifact-other", "source_invalid"),
        ("receipt_id", "receipt-other", "source_invalid"),
        ("receipt_id", None, "source_invalid"),
        ("workspace_id", "workspace-other", "source_invalid"),
        ("kind", "audio", "source_invalid"),
        ("status", "deleted", "source_invalid"),
        ("status", "expired", "source_expired"),
        ("expires_at", "2026-10-23T00:00:00Z", "source_expired"),
        ("bytes", -1, "source_invalid"),
        ("bytes", 4_000_001, "source_too_large"),
        ("media_type", "application/xml", "source_unsupported"),
        ("media_type", "text/plain; charset=latin1", "source_unsupported"),
        ("unexpected", "synthetic-private-content", "source_invalid"),
    ],
)
async def test_invalid_metadata_never_downloads_content(
    field: str, value: object, code: str
) -> None:
    service = Service()
    service.metadata[field] = value
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        with pytest.raises(TranscriptSourceError) as caught:
            await read(PublicTranscriptSource(client, now=lambda: NOW))
    assert caught.value.code == code
    assert str(caught.value) == code and caught.value.__cause__ is None
    assert len(service.requests) == 1


@pytest.mark.parametrize(
    "content,code",
    [
        (b"!" + CONTENT[1:], "source_invalid"),
        (b"a\x00b", "source_invalid"),
        (b"\xff", "source_invalid"),
        (b"x" * 30, "source_too_large"),
    ],
)
async def test_content_digest_and_declared_size_are_enforced(content: bytes, code: str) -> None:
    service = Service()
    service.content = Stream([content])
    if content in {b"\xff", b"a\x00b"}:
        service.metadata["bytes"] = len(content)
        service.metadata["sha256"] = "sha256:" + hashlib.sha256(content).hexdigest()
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        with pytest.raises(TranscriptSourceError) as caught:
            await read(PublicTranscriptSource(client, now=lambda: NOW))
    assert caught.value.code == code
    assert service.content.closed


@pytest.mark.parametrize(
    "status,headers,code",
    [
        (302, {"location": "https://outside.invalid/private"}, "source_unavailable"),
        (206, {"content-type": "application/octet-stream"}, "source_invalid"),
        (410, {}, "source_expired"),
        (
            200,
            {"content-type": "application/octet-stream", "content-encoding": "gzip"},
            "source_unsupported",
        ),
        (200, {"content-type": "text/html"}, "source_unsupported"),
    ],
)
async def test_redirect_partial_expired_and_encoded_responses_are_rejected(
    status: int, headers: dict[str, str], code: str
) -> None:
    service = Service()
    service.status, service.headers = status, headers
    async with httpx.AsyncClient(
        base_url="https://speech.invalid",
        transport=httpx.MockTransport(service.handle),
        follow_redirects=True,
    ) as client:
        with pytest.raises(TranscriptSourceError) as caught:
            await read(PublicTranscriptSource(client, now=lambda: NOW))
    assert caught.value.code == code
    assert len(service.requests) == 2 and service.content.closed


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"invalid-json-synthetic", "source_invalid"),
        (b"[" * 32000 + b"0" + b"]" * 32000, "source_invalid"),
        (b"x" * 65537, "source_too_large"),
    ],
    ids=["invalid-json", "deep-json", "oversize"],
)
async def test_metadata_stream_is_bounded_and_error_has_no_raw_content(
    raw: bytes, code: str
) -> None:
    service = Service()
    service.metadata_raw = raw
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        with pytest.raises(TranscriptSourceError) as caught:
            await read(PublicTranscriptSource(client, now=lambda: NOW))
    assert str(caught.value) == code
    assert (
        len(service.requests) == 1
        and service.metadata_stream is not None
        and service.metadata_stream.closed
    )


async def test_total_deadline_and_caller_cancellation_close_stalled_stream() -> None:
    for cancel in (False, True):
        service = Service()
        service.content = Stream([], stall=True)
        async with httpx.AsyncClient(
            base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
        ) as client:
            task = asyncio.create_task(
                read(
                    PublicTranscriptSource(
                        client, now=lambda: NOW, deadline_seconds=0.02 if not cancel else 10
                    )
                )
            )
            await asyncio.wait_for(service.content.entered.wait(), 1)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(TranscriptSourceError, match=r"^source_unavailable$"):
                    await task
            assert service.content.closed and not client.is_closed


async def test_character_limit_is_independent_of_byte_limit() -> None:
    service = Service()
    content = b"x" * 1_000_001
    service.metadata["bytes"] = len(content)
    service.metadata["sha256"] = "sha256:" + hashlib.sha256(content).hexdigest()
    service.content = Stream([content])
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        with pytest.raises(TranscriptSourceError, match=r"^source_too_large$"):
            await read(PublicTranscriptSource(client, now=lambda: NOW))
    assert service.content.closed


async def test_source_expiring_during_download_is_not_imported() -> None:
    service = Service()
    service.metadata["expires_at"] = (NOW + timedelta(seconds=1)).isoformat()
    clocks = iter((NOW, NOW + timedelta(seconds=2)))
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        with pytest.raises(TranscriptSourceError, match=r"^source_expired$"):
            await read(PublicTranscriptSource(client, now=lambda: next(clocks)))
    assert service.content.closed


async def test_transport_failure_excludes_address_and_original_exception() -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic-private-error", request=request)

    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(fail)
    ) as client:
        with pytest.raises(TranscriptSourceError) as caught:
            await read(PublicTranscriptSource(client))
    assert str(caught.value) == "source_unavailable" and caught.value.__cause__ is None


@pytest.mark.parametrize("deadline", [0, -1, 121, float("nan"), float("inf")])
async def test_invalid_configuration_fails_before_request(deadline: float) -> None:
    service = Service()
    async with httpx.AsyncClient(
        base_url="https://speech.invalid", transport=httpx.MockTransport(service.handle)
    ) as client:
        with pytest.raises(ValueError, match=r"^invalid configured transcript source$"):
            PublicTranscriptSource(client, deadline_seconds=deadline)
    assert not service.requests
