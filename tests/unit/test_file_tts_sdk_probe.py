"""Synthetic HTTP transport tests; not a live TTS acceptance."""

import importlib.util
import io
import json
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import httpx2
import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/dev-env/verify-file-tts-sdk.py"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("file_tts_sdk_probe", PATH)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * 2400)
    return buffer.getvalue()


def args(
    tmp_path: Path, *, url: str = "http://127.0.0.1:8000/v1", deadline: str | None = None
) -> list[str]:
    deadline = deadline or (datetime.now(UTC) + timedelta(minutes=3)).isoformat()
    return [
        "--base-url",
        url,
        "--output-dir",
        str(tmp_path / "output"),
        "--deadline-at",
        deadline,
        "--binding-id",
        "binding-test",
        "--workspace-id",
        "workspace-test",
        "--voice-id",
        "voice-test",
        "--voice-revision",
        "1",
        "--timeout-seconds",
        "1",
    ]


def test_actual_sdk_success_and_repeat_guard(tmp_path: Path) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            200, headers={"content-type": "audio/wav", "x-request-id": "req-test"}, content=wav()
        )

    transport = httpx2.MockTransport(handler)
    m = module()
    assert m.main(args(tmp_path), transport=transport) == 0
    output = tmp_path / "output"
    result = json.loads((output / "result.json").read_bytes())
    assert result["frames"] == 2400 and (output / "audio.wav").read_bytes() == wav()
    assert (output.stat().st_mode & 0o077) == 0 and (
        output / "audio.wav"
    ).stat().st_mode & 0o077 == 0
    body = json.loads(calls[0].content)
    assert body["model"] == "binding-test" and body["nexus"]["voice_revision"] == "1"
    assert m.main(args(tmp_path), transport=transport) != 0 and len(calls) == 1


@pytest.mark.parametrize("kind", ["error", "timeout", "truncated", "oversize"])
def test_failures_record_sanitized_result(tmp_path: Path, kind: str) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if kind == "timeout":
            raise httpx2.ReadTimeout("private-marker", request=request)
        if kind == "error":
            return httpx2.Response(
                503, json={"error": {"code": "backend_unavailable", "message": "private-marker"}}
            )
        return httpx2.Response(200, content=wav()[:-1] if kind == "truncated" else b"x" * (1048577))

    assert module().main(args(tmp_path), transport=httpx2.MockTransport(handler)) != 0
    output = tmp_path / "output"
    result = (output / "result.json").read_text()
    assert "private-marker" not in result and not (output / "audio.wav").exists()
    assert len(calls) == 1


def test_expired_deadline_never_posts(tmp_path: Path) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(500)

    transport = httpx2.MockTransport(handler)
    deadline = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert module().main(args(tmp_path, deadline=deadline), transport=transport) != 0
    assert not calls


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/v1",
        "http://user:secret@127.0.0.1/v1",
        "http://192.0.2.4/v1",
        "http://127.0.0.1/v1?x=1",
        "http://127.0.0.1/v2",
    ],
)
def test_remote_or_invalid_url_never_posts(tmp_path: Path, url: str) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(500)

    assert (
        module().main(
            args(tmp_path, url=url),
            transport=httpx2.MockTransport(handler),
        )
        != 0
    )
    assert not calls


def test_slow_stream_is_bounded_by_total_timeout(tmp_path: Path) -> None:
    import time
    from collections.abc import Iterator

    class SlowStream(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            time.sleep(0.6)
            yield b"RIFF"
            time.sleep(0.6)
            yield b"more"

    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, stream=SlowStream())

    assert module().main(args(tmp_path), transport=httpx2.MockTransport(handler)) != 0
    result = json.loads((tmp_path / "output" / "result.json").read_bytes())
    assert result["kind"] == "timeout" and len(calls) == 1


def test_default_timeout_and_original_request_identity(tmp_path: Path) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, headers={"x-request-id": "server-id"}, content=wav())

    arguments = args(tmp_path, deadline=(datetime.now(UTC) + timedelta(seconds=40)).isoformat())[
        :-2
    ]
    assert module().main(arguments, transport=httpx2.MockTransport(handler)) == 0
    started = json.loads((tmp_path / "output" / "started.json").read_bytes())
    assert len(calls) == 1
    assert started["request_id"] == calls[0].headers["x-request-id"]
    assert started["client_reference"] == json.loads(calls[0].content)["nexus"]["client_reference"]
    assert started["client_reference"] != started["request_id"]


@pytest.mark.parametrize("status,content", [(201, wav()), (200, wav()[:-1])])
def test_invalid_response_preserves_status_and_request_id(
    tmp_path: Path, status: int, content: bytes
) -> None:
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(
            status, headers={"x-request-id": "response-id"}, content=content
        )
    )
    assert module().main(args(tmp_path), transport=transport) != 0
    result = json.loads((tmp_path / "output" / "result.json").read_bytes())
    assert result["status"] == status and result["request_id"] == "response-id"
    assert not (tmp_path / "output" / "audio.wav").exists()


def test_insufficient_default_deadline_never_posts(tmp_path: Path) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(500)

    transport = httpx2.MockTransport(handler)
    arguments = args(tmp_path, deadline=(datetime.now(UTC) + timedelta(seconds=5)).isoformat())[:-2]
    assert module().main(arguments, transport=transport) != 0
    assert not calls


@pytest.mark.parametrize("kind", ["relative", "repo", "symlink"])
def test_output_path_must_resolve_outside_repository(tmp_path: Path, kind: str) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(500)

    transport = httpx2.MockTransport(handler)
    repo = PATH.parents[2]
    if kind == "relative":
        target = "relative-output"
    elif kind == "repo":
        target = str(repo / "unused-probe-output")
    else:
        link = tmp_path / "repo-link"
        link.symlink_to(repo, target_is_directory=True)
        target = str(link / "unused-probe-output")
    arguments = args(tmp_path)
    arguments[arguments.index("--output-dir") + 1] = target
    assert module().main(arguments, transport=transport) != 0
    assert not calls
    assert not (repo / "unused-probe-output").exists()


def test_odd_pcm_data_chunk_is_rejected(tmp_path: Path) -> None:
    original = wav()[:44]
    malformed = bytearray(original + b"\x00\x00\x01\x00")
    malformed[4:8] = (len(malformed) - 8).to_bytes(4, "little")
    malformed[40:44] = (3).to_bytes(4, "little")
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, content=bytes(malformed)))
    assert module().main(args(tmp_path), transport=transport) != 0
    assert not (tmp_path / "output" / "audio.wav").exists()
