"""Real synthetic media, archive integrity, and bounded validation lifetime."""

from __future__ import annotations

import asyncio
import math
import struct
import threading
import time
import wave
import zipfile
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, cast

import av
import pytest

from speechnexus_backend.workbench.application.export_worker import ProducedExport
from speechnexus_backend.workbench.domain.exports import ExportJob
from speechnexus_backend.workbench.infrastructure.export_media_validation import (
    ExportMediaValidationError,
    ExportMediaValidator,
    ExportValidationLimits,
)

LIMITS = ExportValidationLimits(
    max_output_bytes=1_000_000,
    max_unpacked_bytes=2_000_000,
    max_archive_entries=20,
    max_audio_seconds=10,
    timeout_seconds=5,
)


def wav(*, frames: int = 800) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(
            b"".join(struct.pack("<h", int(1000 * math.sin(i / 10))) for i in range(frames))
        )
    return output.getvalue()


def mp3() -> bytes:
    output = BytesIO()
    with (
        av.open(BytesIO(wav()), mode="r", format="wav") as source,
        av.open(output, "w", format="mp3") as destination,
    ):
        stream = destination.add_stream("libmp3lame", rate=8000)
        stream.layout = "mono"
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=8000)
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted):
                    destination.mux(packet)
        for packet in stream.encode(None):
            destination.mux(packet)
    return output.getvalue()


def archive(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, content in entries:
            bundle.writestr(name, content)
    return output.getvalue()


def job(audio_format: str = "wav", *, total: int = 1) -> ExportJob:
    return ExportJob.create(
        export_id="export_media",
        book_id="book_media",
        export_format=audio_format,
        manifest_revision=1,
        progress_total=total,
        now=datetime(2026, 10, 26, tzinfo=UTC),
    )


async def validate(
    content: bytes,
    audio_format: str = "wav",
    *,
    total: int = 1,
    media_type: str | None = None,
    limits: ExportValidationLimits = LIMITS,
) -> None:
    await ExportMediaValidator(limits).validate_export(
        job(audio_format, total=total),
        ProducedExport(
            content,
            media_type
            or {"wav": "audio/wav", "mp3": "audio/mpeg", "zip": "application/zip"}[audio_format],
        ),
    )


async def test_valid_wav_mp3_and_zip_with_metadata_decode() -> None:
    await validate(wav())
    await validate(mp3(), "mp3")
    await validate(
        archive(
            [
                ("audio/", b""),
                ("audio/one.wav", wav()),
                ("two.mp3", mp3()),
                ("manifest.json", b"{}"),
            ]
        ),
        "zip",
        total=2,
    )


@pytest.mark.parametrize("content", [b"", b"private synthetic garbage", wav()[:-1], wav(frames=0)])
async def test_invalid_or_empty_wav_is_rejected_without_content_in_error(content: bytes) -> None:
    with pytest.raises(ExportMediaValidationError) as error:
        await validate(content)
    assert str(error.value) == "export media validation failed"
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("content", "audio_format", "media_type"),
    [
        (wav(), "wav", "audio/mpeg"),
        (wav(), "mp3", "audio/mpeg"),
        (mp3(), "wav", "audio/wav"),
        (b"garbage", "mp3", "audio/mpeg"),
        (wav(), "zip", "application/zip"),
    ],
)
async def test_format_and_media_type_must_match(
    content: bytes, audio_format: str, media_type: str
) -> None:
    with pytest.raises(ExportMediaValidationError):
        await validate(content, audio_format, media_type=media_type)


async def test_mp3_with_attached_picture_is_rejected() -> None:
    def png_chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    image = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + png_chunk(b"IEND", b"")
    )
    picture = b"\x00image/png\x00\x03\x00" + image
    tag = b"APIC" + struct.pack(">I", len(picture)) + b"\x00\x00" + picture
    syncsafe = bytes((len(tag) >> shift) & 127 for shift in (21, 14, 7, 0))
    content = b"ID3\x03\x00\x00" + syncsafe + tag + mp3()
    with av.open(BytesIO(content), mode="r", format="mp3") as container:
        assert any(stream.type == "video" for stream in container.streams)
    with pytest.raises(ExportMediaValidationError):
        await validate(content, "mp3")


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [("manifest.json", b"{}")],
        [("one.wav", wav()[:-1])],
        [("one.mp3", b"garbage")],
        [("one.wav", wav()), ("two.wav", wav())],
    ],
)
async def test_zip_requires_exact_number_of_valid_audio_members(
    entries: list[tuple[str, bytes]],
) -> None:
    with pytest.raises(ExportMediaValidationError):
        await validate(archive(list(entries)), "zip")


@pytest.mark.parametrize(
    "name",
    [
        "../one.wav",
        "/one.wav",
        "C:/one.wav",
        "dir\\one.wav",
        "./one.wav",
        "a//one.wav",
        "a/../one.wav",
    ],
)
async def test_unsafe_archive_paths_are_rejected(name: str) -> None:
    with pytest.raises(ExportMediaValidationError):
        await validate(archive([(name, wav())]), "zip")


async def test_duplicate_archive_members_are_rejected() -> None:
    with pytest.warns(UserWarning, match="Duplicate"):
        content = archive([("one.wav", wav()), ("one.wav", wav())])
    with pytest.raises(ExportMediaValidationError):
        await validate(content, "zip", total=2)


async def test_special_nodes_are_rejected() -> None:
    info = zipfile.ZipInfo("one.wav")
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    with pytest.raises(ExportMediaValidationError):
        await validate(archive([(info, wav())]), "zip")


async def test_crc_corruption_and_truncated_zip_are_rejected() -> None:
    content = bytearray(archive([("one.wav", wav())]))
    central = content.index(b"PK\x01\x02")
    content[central + 16] ^= 1
    with pytest.raises(ExportMediaValidationError):
        await validate(bytes(content), "zip")
    with pytest.raises(ExportMediaValidationError):
        await validate(archive([("one.wav", wav())])[:-8], "zip")


async def test_encrypted_member_flag_is_rejected() -> None:
    content = bytearray(archive([("one.wav", wav())]))
    central = content.index(b"PK\x01\x02")
    content[6] |= 1
    content[central + 8] |= 1
    with pytest.raises(ExportMediaValidationError):
        await validate(bytes(content), "zip")


@pytest.mark.parametrize(
    "limits",
    [
        replace(LIMITS, max_output_bytes=10),
        replace(LIMITS, max_unpacked_bytes=10),
        replace(LIMITS, max_archive_entries=1),
        replace(LIMITS, max_audio_seconds=0.15),
    ],
)
async def test_archive_limits_include_metadata_and_cumulative_audio(
    limits: ExportValidationLimits,
) -> None:
    with pytest.raises(ExportMediaValidationError):
        await validate(
            archive([("one.wav", wav()), ("two.wav", wav()), ("metadata", b"a" * 3000)]),
            "zip",
            total=2,
            limits=limits,
        )


async def test_single_audio_size_and_decoded_duration_limits() -> None:
    with pytest.raises(ExportMediaValidationError):
        await validate(wav(), limits=replace(LIMITS, max_output_bytes=100))
    with pytest.raises(ExportMediaValidationError):
        await validate(wav(), limits=replace(LIMITS, max_audio_seconds=0.09))
    await validate(
        wav(), limits=replace(LIMITS, max_output_bytes=len(wav()), max_audio_seconds=0.1)
    )


@pytest.mark.parametrize(
    "field",
    [
        "max_output_bytes",
        "max_unpacked_bytes",
        "max_archive_entries",
        "max_audio_seconds",
        "timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, True])
def test_limits_require_finite_positive_values(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        replace(LIMITS, **cast(dict[str, Any], {field: value}))


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_join_validation_thread(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    started, exited = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def slow_decode(self: Any, content: bytes, audio_format: str, budget: Any) -> None:
        loop.call_soon_threadsafe(started.set)
        try:
            while True:
                budget.check()
                time.sleep(0.001)
        finally:
            exited.set()

    monkeypatch.setattr(ExportMediaValidator, "_decode_audio", slow_decode)
    task = asyncio.create_task(validate(wav(), limits=replace(LIMITS, timeout_seconds=0.1)))
    await started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ExportMediaValidationError):
        await task
    assert exited.is_set()


async def test_exact_cumulative_duration_boundary_is_accepted() -> None:
    await validate(
        archive([(f"{number}.wav", wav(frames=1)) for number in range(10)]),
        "zip",
        total=10,
        limits=replace(LIMITS, max_audio_seconds=0.00125),
    )


async def test_zip_with_valid_audio_still_cannot_fill_missing_item() -> None:
    with pytest.raises(ExportMediaValidationError):
        await validate(archive([("one.wav", wav()), ("manifest.json", b"{}")]), "zip", total=2)


@pytest.mark.parametrize("hidden", [wav(), mp3()], ids=["wav", "mp3"])
async def test_recognizable_audio_cannot_be_disguised_as_metadata(hidden: bytes) -> None:
    with pytest.raises(ExportMediaValidationError):
        await validate(archive([("one.wav", wav()), ("metadata.bin", hidden)]), "zip")


async def test_optional_binary_metadata_remains_accepted() -> None:
    await validate(archive([("one.wav", wav()), ("metadata.bin", b"\x00\x01\xffbinary")]), "zip")


def zip64_archive() -> bytes:
    content = bytearray(archive([("one.wav", wav())]))
    end = content.index(b"PK\x05\x06")
    size, offset = struct.unpack_from("<II", content, end + 12)
    record = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, size, offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
    struct.pack_into("<HHII", content, end + 8, 65535, 65535, 0xFFFFFFFF, 0xFFFFFFFF)
    return bytes(content[:end] + record + locator + content[end:])


async def test_small_valid_zip64_archive_is_accepted() -> None:
    await validate(zip64_archive(), "zip")


@pytest.mark.parametrize("field_offset", [24, 32, 40, 48])
async def test_zip64_forged_large_counts_sizes_or_offsets_are_rejected(field_offset: int) -> None:
    content = bytearray(zip64_archive())
    record = content.index(b"PK\x06\x06")
    struct.pack_into("<Q", content, record + field_offset, 2**63)
    with pytest.raises(ExportMediaValidationError):
        await validate(bytes(content), "zip")


async def test_repeated_cancellation_waits_for_thread_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, cleaning = asyncio.Event(), asyncio.Event()
    released, exited = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def slow_decode(self: Any, content: bytes, audio_format: str, budget: Any) -> None:
        loop.call_soon_threadsafe(started.set)
        try:
            while True:
                budget.check()
                time.sleep(0.001)
        finally:
            loop.call_soon_threadsafe(cleaning.set)
            released.wait(timeout=2)
            exited.set()

    monkeypatch.setattr(ExportMediaValidator, "_decode_audio", slow_decode)
    task = asyncio.create_task(validate(wav()))
    await started.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()
