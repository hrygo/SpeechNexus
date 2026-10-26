"""Real synthetic decoding and the private worker's bounded, content-free receipt."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from speechnexus_gateway.domain.media import AudioDecodeLimits, AudioSourceFormat, MediaError
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_media import inspection
from speechnexus_media.decoder import PyAvAudioDecoder as SharedDecoder
from speechnexus_media.domain import MediaCancelledError, MediaLimitError
from speechnexus_media.domain import MediaError as SharedError

LIMITS = AudioDecodeLimits(max_encoded_bytes=65536, max_duration_ms=1000)
ARGS = [
    "--media-type",
    "audio/wav",
    "--max-encoded-bytes",
    "65536",
    "--max-duration-ms",
    "1000",
    "--timeout-seconds",
    "10",
]


def wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as encoded:
        encoded.setnchannels(2)
        encoded.setsampwidth(2)
        encoded.setframerate(48000)
        encoded.writeframes(b"\x01\x00\x02\x00" * 4800)
    return output.getvalue()


def test_gateway_and_shared_media_keep_exception_and_decoder_identity() -> None:
    # Existing B callers must catch exceptions from the relocated codec without
    # translating into a second domain type or changing their recovery path.
    assert MediaError is SharedError
    assert PyAvAudioDecoder is SharedDecoder
    stream = PyAvAudioDecoder().open(
        io.BytesIO(b"not-a-container"),
        source_format=AudioSourceFormat("audio/wav"),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    with pytest.raises(MediaError):
        list(stream)
    assert stream.summary is None


def test_real_decode_binds_complete_source_facts_to_digest_without_pcm() -> None:
    content = wav()
    source = io.BytesIO(content)
    facts = inspection.inspect_audio(
        source,
        source_format=AudioSourceFormat("audio/wav"),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    assert facts == {
        "schema_version": 1,
        "status": "complete",
        "recipe_version": "pcm-decode-1",
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "encoded_bytes": len(content),
        "media_type": "audio/wav",
        "sample_rate_hz": 48000,
        "channels": 2,
        "source_samples": 4800,
        "duration_ms": 100,
    }
    assert len(json.dumps(facts).encode()) < 4096
    assert not source.closed


@pytest.mark.parametrize("kind", ["corrupt", "encoded_limit", "duration_limit", "cancelled"])
def test_failure_never_returns_partial_facts(kind: str) -> None:
    content = b"synthetic-private-input" if kind == "corrupt" else wav()
    limits = LIMITS
    if kind == "encoded_limit":
        limits = replace(limits, max_encoded_bytes=20)
    if kind == "duration_limit":
        limits = replace(limits, max_duration_ms=50)
    error = MediaCancelledError if kind == "cancelled" else MediaError
    with pytest.raises(error):
        inspection.inspect_audio(
            io.BytesIO(content),
            source_format=AudioSourceFormat("audio/wav"),
            limits=limits,
            cancelled=lambda: kind == "cancelled",
        )


def test_deadline_also_covers_input_hashing() -> None:
    ticks = iter((0.0, 31.0))
    with pytest.raises(MediaLimitError, match="deadline"):
        inspection.inspect_audio(
            io.BytesIO(wav()),
            source_format=AudioSourceFormat("audio/wav"),
            limits=LIMITS,
            cancelled=lambda: False,
            monotonic=lambda: next(ticks),
        )


@pytest.mark.parametrize("fix_outer_length", [False, True])
def test_truncated_wav_is_rejected_even_when_codec_reaches_eof(fix_outer_length: bool) -> None:
    content = bytearray(wav()[:-20])
    if fix_outer_length:
        # The RIFF length can be consistent while its data chunk still claims
        # 4800 frames. The inherited decoder accepts EOF after only 4795 frames.
        struct.pack_into("<I", content, 4, len(content) - 8)
    with pytest.raises(MediaError):
        inspection.inspect_audio(
            io.BytesIO(content),
            source_format=AudioSourceFormat("audio/wav"),
            limits=LIMITS,
            cancelled=lambda: False,
        )


def test_unqualified_container_profile_cannot_claim_complete() -> None:
    with pytest.raises(MediaError, match="not qualified"):
        inspection.inspect_audio(
            io.BytesIO(wav()),
            source_format=AudioSourceFormat("audio/flac"),
            limits=LIMITS,
            cancelled=lambda: False,
        )


@pytest.mark.parametrize("kind", ["valid", "corrupt", "missing", "pcm_without_metadata"])
def test_private_command_prints_one_bounded_receipt_without_paths_or_vendor_errors(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "synthetic-private-name"
    if kind != "missing":
        source.write_bytes(b"synthetic-private-input" if kind == "corrupt" else wav())
    monkeypatch.setattr(inspection, "_INPUT", source)
    args = ARGS.copy()
    if kind == "pcm_without_metadata":
        args[1] = "audio/pcm"
    status = inspection.main(args)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1 and len(captured.out.encode()) < 4096
    assert "synthetic-private" not in captured.out and "pcm16" not in captured.out
    report = json.loads(captured.out)
    if kind == "valid":
        assert status == 0 and report["status"] == "complete" and report["source_samples"] == 4800
    else:
        assert status != 0
        assert report == {
            "schema_version": 1,
            "status": "unavailable" if kind == "missing" else "invalid_media",
        }
