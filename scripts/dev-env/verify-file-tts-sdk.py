#!/usr/bin/env python3
"""One-shot OpenAI SDK TTS check against an explicitly configured loopback API.

A successful run validates one WAV response, not production qualification.
Retain the output directory on all outcomes; never retry an unknown submission.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import signal
import time
import uuid
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import openai

MAX_BYTES = 1048576
TEXT = "A short English voice test."
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
ERROR_CODES = frozenset(
    {
        "unsupported_parameter",
        "invalid_request",
        "model_not_found",
        "voice_not_found",
        "profile_not_published",
        "pronunciation_not_found",
        "workspace_not_found",
        "workspace_disabled",
        "operation_timeout",
        "speech_output_limit_exceeded",
        "speech_output_invalid",
        "backend_unavailable",
    }
)


def private_write(directory: Path, name: str, data: bytes) -> None:
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def save(directory: Path, name: str, value: dict[str, object]) -> None:
    private_write(
        directory, name, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


def base_url(value: str) -> str:
    if "?" in value or "#" in value:
        raise ValueError("invalid loopback API origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("/v1", "/v1/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid loopback API origin")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid port")
    return value.rstrip("/")


def wav_facts(data: bytes) -> dict[str, object]:
    if (
        len(data) < 44
        or data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or int.from_bytes(data[4:8], "little") + 8 != len(data)
    ):
        raise ValueError("invalid WAV")
    offset = 12
    chunks_seen: set[bytes] = set()
    block_align = None
    data_size = None
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("truncated WAV chunk")
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        start = offset + 8
        end = start + size
        if chunk_id in chunks_seen or end + (size % 2) > len(data):
            raise ValueError("invalid WAV chunk")
        chunks_seen.add(chunk_id)
        if chunk_id == b"fmt ":
            if size < 16:
                raise ValueError("invalid WAV format chunk")
            block_align = int.from_bytes(data[start + 12 : start + 14], "little")
        elif chunk_id == b"data":
            data_size = size
        offset = end + (size % 2)
    if block_align != 2 or data_size is None or data_size % block_align:
        raise ValueError("incomplete PCM frame")
    with wave.open(io.BytesIO(data), "rb") as source:
        rate, channels, width, frames = (
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
            source.getnframes(),
        )
        if (
            source.getcomptype() != "NONE"
            or (rate, channels, width) != (24000, 1, 2)
            or not 0 < frames <= 240000
        ):
            raise ValueError("invalid WAV format")
        if len(source.readframes(frames)) != frames * 2:
            raise ValueError("truncated WAV")
    return {
        "format": "wav",
        "frames": frames,
        "sample_rate_hz": rate,
        "channels": channels,
        "sample_width_bytes": width,
        "duration_ms": frames * 1000 / rate,
        "byte_count": len(data),
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


@contextmanager
def total_timeout(seconds: float) -> Iterator[None]:
    """Bound the entire synchronous call, including a peer that trickles bytes."""
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("an existing process timer is active")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main(argv: list[str] | None = None, *, transport: httpx2.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "base-url",
        "output-dir",
        "deadline-at",
        "binding-id",
        "workspace-id",
        "voice-id",
        "voice-revision",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    options = parser.parse_args(argv)
    directory = Path(options.output_dir)
    try:
        repository = Path(__file__).resolve().parents[2]
        if not directory.is_absolute() or directory.resolve().is_relative_to(repository):
            raise ValueError("output directory must be absolute and outside the repository")
        directory.mkdir(mode=0o700)
    except OSError, ValueError:
        print("probe unavailable; output directory must be new and outside the repository")
        return 2
    result: dict[str, object] = {"outcome": "failed", "kind": "invalid_configuration"}
    try:
        if (
            directory.is_symlink()
            or directory.stat().st_uid != os.getuid()
            or directory.stat().st_mode & 0o077
        ):
            raise ValueError
        url = base_url(options.base_url)
        if any(
            not IDENTIFIER.fullmatch(v)
            for v in (options.binding_id, options.workspace_id, options.voice_id)
        ) or not re.fullmatch(r"[1-9][0-9]{0,18}", options.voice_revision):
            raise ValueError
        timeout = options.timeout_seconds
        if not 0 < timeout <= 300:
            raise ValueError
        deadline = datetime.fromisoformat(options.deadline_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline.utcoffset() != UTC.utcoffset(deadline):
            raise ValueError
        if (deadline - datetime.now(UTC)).total_seconds() < timeout:
            result["kind"] = "deadline"
            raise ValueError
        client_reference = "sdk-tts-" + uuid.uuid4().hex
        correlation_id = "sdk-tts-" + uuid.uuid4().hex
        request = {
            "model": options.binding_id,
            "input": TEXT,
            "voice": options.voice_id,
            "response_format": "wav",
            "speed": 1.0,
            "nexus": {
                "workspace_id": options.workspace_id,
                "voice_revision": options.voice_revision,
                "client_reference": client_reference,
                "overrides": {"tts": {"language": "en"}},
            },
        }
        encoded = json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        save(
            directory,
            "started.json",
            {
                "request_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                "client_reference": client_reference,
                "request_id": correlation_id,
                "identity_note": "correlation only; do not resend or assume execution identity",
                "binding_id": options.binding_id,
                "workspace_id": options.workspace_id,
                "voice_id": options.voice_id,
                "voice_revision": options.voice_revision,
                "sdk_version": openai.__version__,
            },
        )
        started = time.monotonic()
        with (
            total_timeout(timeout),
            httpx2.Client(
                trust_env=False, follow_redirects=False, timeout=timeout, transport=transport
            ) as http,
            openai.OpenAI(
                api_key="loopback-probe",
                base_url=url,
                http_client=http,
                max_retries=0,
                timeout=timeout,
            ) as client,
        ):
            if (deadline - datetime.now(UTC)).total_seconds() < timeout:
                result["kind"] = "deadline"
                raise ValueError
            with client.audio.speech.with_streaming_response.create(
                model=options.binding_id,
                input=TEXT,
                voice=options.voice_id,
                response_format="wav",
                speed=1.0,
                extra_body={"nexus": request["nexus"]},
                extra_headers={"X-Request-ID": correlation_id},
            ) as response:
                status = response.status_code
                request_id = response.headers.get("x-request-id")
                if not isinstance(request_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,128}", request_id
                ):
                    request_id = None
                result.update(kind="invalid_response", status=status, request_id=request_id)
                if status != 200:
                    raise ValueError("unexpected HTTP status")
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() - started >= timeout or datetime.now(UTC) >= deadline:
                        raise TimeoutError
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("audio too large")
                    chunks.append(chunk)
                audio = b"".join(chunks)
        facts = wav_facts(audio)
        private_write(directory, "audio.wav", audio)
        result = {
            "outcome": "success",
            "sdk_version": openai.__version__,
            "status": status,
            "request_id": request_id,
            **facts,
        }
    except openai.APIStatusError as error:
        code = error.code if isinstance(error.code, str) and error.code in ERROR_CODES else None
        request_id = error.response.headers.get("x-request-id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", request_id
        ):
            request_id = None
        result = {
            "outcome": "failed",
            "kind": "api_status",
            "status": error.status_code,
            "code": code,
            "request_id": request_id,
        }
    except openai.APITimeoutError, TimeoutError:
        result.update(kind="timeout")
    except openai.APIConnectionError:
        result.update(kind="connection")
    except ValueError, TypeError, OverflowError, UnicodeError, wave.Error, EOFError, OSError:
        if result["kind"] not in ("deadline", "invalid_response"):
            result["kind"] = "invalid_or_unavailable"
    except Exception:
        result.update(kind="unavailable")
    try:
        save(directory, "result.json", result)
    except OSError:
        print("probe unavailable; result could not be saved")
        return 2
    print("probe success" if result["outcome"] == "success" else "probe failed")
    return 0 if result["outcome"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
