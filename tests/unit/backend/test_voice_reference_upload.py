"""`voice_reference` uploads are inspected as audio, end to end in-process.

The public wire is exercised here: an HTTP client talks to the app, the app runs
`AssetService`, the configured `HttpAudioAssetInspector` speaks the frozen
private protocol through an injected transport, and that transport answers with
the real decode child's verdict for the exact bytes it received. Storage is an
in-memory fake, so this proves the inspection path and its public errors, not
PostgreSQL or object-storage behavior.

The regression this file locks: before #592 the adapter answered every
non-`asr_input` purpose with `Inspection(available=False)`, so a real inspected
deployment refused every reference upload as `503 asset_inspection_unavailable`
and voice cloning could not obtain material at all.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from speechnexus_contracts import parse

from speechnexus_asset_inspector.protocol import failure_body
from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.assets import (
    AssetMetadata,
    AssetObjects,
    AssetService,
)
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.assets import (
    AssetPolicy,
    AssetRecord,
    UploadIntent,
    UploadResult,
    VerifiedObject,
    begin_asset_write,
    publish_asset,
    reserve_asset,
)
from speechnexus_backend.speech.infrastructure.audio_inspection_http import (
    HttpAudioAssetInspector,
)
from tests.fake.readiness import StaticProbe
from tests.unit.backend.test_audio_inspection_http import decoded, pcm_wav
from tests.unit.backend.test_health import _settings

URL = "http://asset-inspector.invalid:8080"
TOKEN = SecretStr("inspection-token-0001")


class _Metadata:
    """The real domain transitions in memory; no database and no I/O."""

    def __init__(self) -> None:
        self.record: AssetRecord | None = None

    def reserve(
        self,
        intent: UploadIntent,
        *,
        asset_id: str,
        object_key: str,
        inspection: Any,
        policy: AssetPolicy,
    ) -> UploadResult:
        self.record = reserve_asset(
            intent,
            inspection,
            policy,
            asset_id=asset_id,
            object_key=object_key,
            now_us=0,
        )
        return UploadResult("created", self.record)

    def begin_write(self, asset_id: str, writer_id: str) -> UploadResult:
        assert self.record is not None
        self.record = begin_asset_write(self.record, writer_id, 1)
        return UploadResult("created", self.record)

    def publish(self, proof: VerifiedObject) -> UploadResult:
        assert self.record is not None
        self.record = publish_asset(self.record, proof, now_us=2, workspace_active=True)
        return UploadResult("created", self.record)

    def get(self, asset_id: str) -> tuple[AssetRecord | None, int]:
        return self.record, 3


class _Objects:
    def check_writes_available(self) -> None:
        return None

    def put_verified(self, asset: AssetRecord, body: Any) -> VerifiedObject:
        body.seek(0)
        assert body.read()
        return VerifiedObject(
            asset.asset_id,
            asset.object_key,
            "storage-version-1",
            asset.bytes,
            asset.sha256,
            asset.writer_id or "",
        )


_DECODED = object()


def _inspector(*, transport: object = _DECODED, address: str = URL) -> HttpAudioAssetInspector:
    return HttpAudioAssetInspector(
        address,
        TOKEN,
        timeout_seconds=15,
        wall_seconds=10,
        max_duration_ms=120_000,
        max_input_bytes=25 * 1024 * 1024,
        max_receipt_bytes=4096,
        _transport=(
            httpx.MockTransport(decoded) if transport is _DECODED else cast(Any, transport)
        ),
    )


@asynccontextmanager
async def _api(
    policy: AssetPolicy, *, inspector: HttpAudioAssetInspector | None = None
) -> AsyncIterator[AsyncClient]:
    assembled = inspector if inspector is not None else _inspector()
    service = AssetService(
        cast(AssetMetadata, _Metadata()),
        cast(AssetObjects, _Objects()),
        assembled,
        policy,
    )
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",)}),
        readiness=ReadinessService([StaticProbe("synthetic")], timeout_seconds=1),
        assets=service,
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        assembled.close()


async def _upload(client: AsyncClient, content: bytes) -> httpx.Response:
    return await client.post(
        "/v1/nexus/assets",
        files={"file": ("reference.wav", content)},
        data={"purpose": "voice_reference"},
        headers={"X-Request-ID": "trace-reference"},
    )


async def test_a_qualified_reference_becomes_a_ready_voice_reference_asset() -> None:
    async with _api(AssetPolicy(60)) as client:
        response = await _upload(client, pcm_wav())

    assert response.status_code == 201
    asset = parse("Asset", response.json())
    # The decoded facts are the receipt's, not a guess from the filename.
    assert (asset["purpose"], asset["status"], asset["media_type"]) == (
        "voice_reference",
        "ready",
        "audio/wav",
    )
    assert (asset["duration_ms"], asset["sample_rate_hz"], asset["channels"]) == (100, 48000, 2)


async def test_a_malformed_reference_is_422_invalid_asset_audio() -> None:
    async with _api(AssetPolicy(60)) as client:
        response = await _upload(client, pcm_wav()[:-64])

    assert response.status_code == 422
    error = parse("ErrorResponse", response.json())["error"]
    assert (error["code"], error["type"], error["retryable"]) == (
        "invalid_asset_audio",
        "invalid_request_error",
        False,
    )
    # Transport replay stays suppressed for a permanent content verdict.
    assert response.headers["X-Should-Retry"] == "false"


async def test_a_reference_over_the_configured_ceiling_is_422_asset_audio_too_long() -> None:
    # 1.5 s of valid audio against the smallest admissible ceiling (1 s).
    async with _api(AssetPolicy(60, max_reference_duration_ms=1_000)) as client:
        response = await _upload(client, pcm_wav(samples=72_000))

    assert response.status_code == 422
    error = parse("ErrorResponse", response.json())["error"]
    assert (error["code"], error["type"], error["retryable"]) == (
        "asset_audio_too_long",
        "invalid_request_error",
        False,
    )
    assert response.headers["X-Should-Retry"] == "false"


async def test_a_reference_under_the_configured_ceiling_is_accepted() -> None:
    # The control for the rejection above: the same ceiling admits a 100 ms clip,
    # so the 422 is the clip's duration and not a broken configuration.
    async with _api(AssetPolicy(60, max_reference_duration_ms=1_000)) as client:
        response = await _upload(client, pcm_wav())

    assert response.status_code == 201
    assert parse("Asset", response.json())["duration_ms"] == 100


@pytest.mark.parametrize("ceiling", [999, 60_001])
def test_the_reference_ceiling_is_bounded_by_the_clone_range(ceiling: int) -> None:
    # A ceiling below the shortest admissible reference would refuse every clone
    # input; one above the longest would admit clips no clone accepts.
    with pytest.raises(ValueError):
        AssetPolicy(60, max_reference_duration_ms=ceiling)


async def test_an_unreachable_inspector_is_the_non_retryable_503_over_the_wire() -> None:
    """A port with no listener is a dead configuration, not capacity.

    Before this change the same request answered `retryable: true` together with
    `X-Should-Retry: false`, inviting clients to loop on a deployment that can
    never succeed. Both flags now say the same thing: do not retry.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    # The socket is closed below, so nothing listens on that port any more.
    inspector = _inspector(transport=None, address=f"http://127.0.0.1:{port}")
    async with _api(AssetPolicy(60), inspector=inspector) as client:
        response = await _upload(client, pcm_wav())

    assert response.status_code == 503
    error = parse("ErrorResponse", response.json())["error"]
    assert (error["code"], error["param"], error["retryable"]) == (
        "asset_inspection_unavailable",
        None,
        False,
    )
    assert response.headers["X-Should-Retry"] == "false"


async def test_a_busy_inspector_is_retryable_but_still_suppresses_transport_replay() -> None:
    """`retryable: true` with `X-Should-Retry: false` is deliberate, not a bug.

    `error.retryable` answers whether a *new* logical upload may succeed, and an
    inspector that answered "busy" said exactly that. `X-Should-Retry` answers
    whether an SDK may replay *this* transport request, whose body may already be
    consumed, so it stays `false` on every Asset 503.
    """

    def busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=failure_body("busy"),
            headers={"Content-Type": "application/json"},
        )

    inspector = _inspector(transport=httpx.MockTransport(busy))
    async with _api(AssetPolicy(60), inspector=inspector) as client:
        response = await _upload(client, pcm_wav())

    assert response.status_code == 503
    error = parse("ErrorResponse", response.json())["error"]
    assert (error["code"], error["retryable"]) == ("asset_inspection_busy", True)
    assert response.headers["X-Should-Retry"] == "false"
