"""The real ASGI surface, real decoder child and the frozen private statuses."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from speechnexus_contracts.audio_inspection import parse as parse_receipt
from tests.unit.inspector.support import (
    TOKEN,
    BlockingRunner,
    call_asgi,
    float32_wav,
    inspection_headers,
    pcm_wav,
    service,
    settings,
    wait_until,
)

from speechnexus_asset_inspector.app import create_app
from speechnexus_asset_inspector.runner import RunOutcome
from speechnexus_asset_inspector.service import InspectorService

FAILURE_KEYS = {"schema_version", "status"}


def failure(payload: bytes) -> dict[str, object]:
    value: dict[str, object] = json.loads(payload)
    assert set(value) == FAILURE_KEYS
    assert value["schema_version"] == 1
    return value


async def post(
    app: object,
    body: bytes,
    *,
    token: str = TOKEN,
    deadline_ms: int = 10_000,
    max_duration_ms: int = 120_000,
    content_length: int | None = None,
    content_type: str | None = "application/octet-stream",
    request_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = inspection_headers(
        body,
        token=token,
        deadline_ms=deadline_ms,
        max_duration_ms=max_duration_ms,
        content_length=content_length,
        content_type=content_type,
        request_id=request_id,
    )
    return await call_asgi(app, headers=headers, body=body)


async def test_valid_wav_returns_complete_contract_receipt(tmp_path: Path) -> None:
    svc = service(tmp_path)
    app = create_app(svc)
    body = pcm_wav(24_000, channels=2, sample_rate_hz=48_000)

    status, headers, payload = await post(app, body, request_id="synthetic-request-1")

    assert status == 200
    assert headers["content-type"] == "application/json"
    assert headers["x-request-id"] == "synthetic-request-1"
    receipt = parse_receipt("AudioInspectionSuccess", json.loads(payload))
    assert receipt["status"] == "complete"
    assert receipt["schema_version"] == 1
    assert receipt["recipe_version"] == "pcm-decode-1"
    assert receipt["media_type"] == "audio/wav"
    assert receipt["encoded_bytes"] == len(body)
    assert receipt["sample_rate_hz"] == 48_000
    assert receipt["channels"] == 2
    assert receipt["source_samples"] == 24_000
    assert receipt["duration_ms"] == 500
    assert svc.in_flight == 0


async def test_truncated_wav_is_invalid_media(tmp_path: Path) -> None:
    app = create_app(service(tmp_path))
    status, _, payload = await post(app, pcm_wav(800)[:-40])
    assert status == 422
    assert failure(payload)["status"] == "invalid_media"


async def test_inconsistent_riff_length_is_invalid_media(tmp_path: Path) -> None:
    app = create_app(service(tmp_path))
    damaged = bytearray(pcm_wav(800))
    damaged[4:8] = (1).to_bytes(4, "little")
    status, _, payload = await post(app, bytes(damaged))
    assert status == 422
    assert failure(payload)["status"] == "invalid_media"


async def test_non_pcm_wav_is_invalid_media(tmp_path: Path) -> None:
    app = create_app(service(tmp_path))
    status, _, payload = await post(app, float32_wav(800))
    assert status == 422
    assert failure(payload)["status"] == "invalid_media"


async def test_duration_over_request_budget_is_media_limit_exceeded(tmp_path: Path) -> None:
    app = create_app(service(tmp_path))
    status, _, payload = await post(app, pcm_wav(32_000), max_duration_ms=1_000)
    assert status == 422
    assert failure(payload)["status"] == "media_limit_exceeded"


async def test_encoded_over_cap_is_encoded_too_large(tmp_path: Path) -> None:
    app = create_app(service(tmp_path, value=settings(max_encoded_bytes=1_024)))
    status, _, payload = await post(app, pcm_wav(2_000))
    assert status == 413
    assert failure(payload)["status"] == "encoded_too_large"


@pytest.mark.parametrize("token", [None, "wrong-but-well-formed-token"])
async def test_missing_or_wrong_bearer_is_unauthorized(tmp_path: Path, token: str | None) -> None:
    app = create_app(service(tmp_path))
    body = pcm_wav(800)
    headers = inspection_headers(body)
    if token is None:
        headers = [(name, value) for name, value in headers if name != b"authorization"]
    else:
        headers = [
            (name, value) if name != b"authorization" else (name, f"Bearer {token}".encode())
            for name, value in headers
        ]
    status, _, payload = await call_asgi(app, headers=headers, body=body)
    assert status == 401
    assert failure(payload)["status"] == "unauthorized"


@pytest.mark.parametrize("content_type", [None, "audio/wav", "application/json"])
async def test_content_type_mismatch_is_a_bad_request(
    tmp_path: Path, content_type: str | None
) -> None:
    app = create_app(service(tmp_path))
    status, _, payload = await post(app, pcm_wav(800), content_type=content_type)
    assert status == 400
    assert failure(payload)["status"] == "bad_request"


async def test_content_length_mismatch_is_bad_request(tmp_path: Path) -> None:
    app = create_app(service(tmp_path))
    body = pcm_wav(800)
    headers = inspection_headers(body, content_length=len(body) + 1)
    status, _, payload = await call_asgi(app, headers=headers, body=body)
    assert status == 400
    assert failure(payload)["status"] == "bad_request"

    headers = inspection_headers(body, content_length=len(body) - 1)
    status, _, payload = await call_asgi(app, headers=headers, body=body)
    assert status == 400
    assert failure(payload)["status"] == "bad_request"


async def test_missing_content_length_is_bad_request(tmp_path: Path) -> None:
    app = create_app(service(tmp_path))
    body = pcm_wav(800)
    headers = [
        (name, value) for name, value in inspection_headers(body) if name != b"content-length"
    ]
    status, _, payload = await call_asgi(app, headers=headers, body=body)
    assert status == 400
    assert failure(payload)["status"] == "bad_request"


@pytest.mark.parametrize(
    "name,value",
    [
        (b"x-nexus-deadline-ms", b"not-a-number"),
        (b"x-nexus-deadline-ms", b"0"),
        (b"x-nexus-max-duration-ms", b"-1"),
        (b"x-request-id", b"bad\x01value"),
    ],
)
async def test_malformed_budget_or_correlation_header_is_bad_request(
    tmp_path: Path, name: bytes, value: bytes
) -> None:
    app = create_app(service(tmp_path))
    body = pcm_wav(800)
    headers = inspection_headers(body)
    headers = [
        (header, value) if header == name else (header, current) for header, current in headers
    ]
    if not any(header == name for header, _ in headers):
        headers.append((name, value))
    status, _, payload = await call_asgi(app, headers=headers, body=body)
    assert status == 400
    assert failure(payload)["status"] == "bad_request"


async def test_third_concurrent_request_is_busy_without_queue(tmp_path: Path) -> None:
    blocking = BlockingRunner()
    svc = service(tmp_path, runner_impl=blocking)
    app = create_app(svc)
    body = pcm_wav(80)

    first = asyncio.create_task(post(app, body))
    second = asyncio.create_task(post(app, body))
    try:
        await wait_until(lambda: svc.in_flight == 2)
        async with asyncio.timeout(2):
            status, _, payload = await post(app, body)
        assert status == 429
        assert failure(payload)["status"] == "busy"
    finally:
        blocking.release.set()
        results = await asyncio.gather(first, second)
    assert [result[0] for result in results] == [200, 200]
    assert svc.in_flight == 0


async def test_expired_wall_clock_kills_child_and_returns_deadline(tmp_path: Path) -> None:
    app = create_app(service(tmp_path, value=settings(max_wall_seconds=0.05)))
    body = pcm_wav(16_000)
    status, _, payload = await post(app, body, deadline_ms=50)
    assert status == 504
    assert failure(payload)["status"] == "deadline_exceeded"


async def test_receipt_over_output_cap_is_unavailable(tmp_path: Path) -> None:
    app = create_app(service(tmp_path, value=settings(output_bytes=8, max_receipt_bytes=8)))
    status, _, payload = await post(app, pcm_wav(800))
    assert status == 503
    assert failure(payload)["status"] == "unavailable"


async def test_unready_service_reports_retryable_busy_without_decoding(
    tmp_path: Path,
) -> None:
    counting = CountingRunner()
    svc = InspectorService(settings(), runtime_root=tmp_path, runner=counting)
    app = create_app(svc)
    status, _, payload = await post(app, pcm_wav(80))
    assert status == 429
    assert failure(payload)["status"] == "busy"
    assert counting.calls == 0
    assert svc.in_flight == 0


async def test_health_and_readiness_expose_contract_facts(tmp_path: Path) -> None:
    svc = InspectorService(settings(), runtime_root=tmp_path, runner=BlockingRunner())
    app = create_app(svc)

    status, _, payload = await call_asgi(app, headers=[], method="GET", path="/healthz")
    assert status == 200
    assert json.loads(payload) == {"status": "ok"}

    status, _, payload = await call_asgi(app, headers=[], method="GET", path="/readyz")
    assert status == 503
    assert json.loads(payload) == {"status": "unavailable"}

    svc.mark_ready()
    status, _, payload = await call_asgi(app, headers=[], method="GET", path="/readyz")
    assert status == 200
    assert json.loads(payload) == {
        "contract_version": 1,
        "recipe_version": "pcm-decode-1",
        "max_encoded_bytes": 4 * 1024 * 1024,
        "max_wall_seconds": 20.0,
        "in_flight": 0,
    }


async def test_startup_self_check_decodes_builtin_wav(tmp_path: Path) -> None:
    svc = service(tmp_path)
    assert await svc.self_check()
    assert svc.in_flight == 0


async def test_self_check_failure_exits_the_process(tmp_path: Path) -> None:
    exact_settings = settings(cpu_seconds=1)
    svc = InspectorService(exact_settings, runtime_root=tmp_path, runner=BrokenRunner())
    exits: list[int] = []
    app = create_app(svc, exit_process=exits.append)
    async with app.router.lifespan_context(app):
        await wait_until(lambda: bool(exits))
    assert exits == [1]
    assert not svc.ready


class CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **_kwargs: object) -> RunOutcome:
        self.calls += 1
        return RunOutcome("unavailable")


class BrokenRunner:
    async def run(self, **_kwargs: object) -> RunOutcome:
        return RunOutcome("unavailable")


def test_service_process_does_not_import_the_decoder_stack() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys, speechnexus_asset_inspector.app; "
            "print(any(name in sys.modules for name in ('av', 'numpy')))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "False"
