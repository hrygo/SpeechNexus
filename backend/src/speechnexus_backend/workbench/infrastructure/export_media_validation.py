"""Bounded, local decoding before export bytes enter the object-write lifecycle.

This checks media integrity and the count of fully decoded .wav/.mp3 members,
not a fixed unit/revision manifest or perceptual quality. Recognizable WAV/MP3
markers in metadata are rejected, without claiming general hidden-content
detection. Deadlines are cooperative: a native codec call must return before
cancellation can join the thread and finish.
"""

from __future__ import annotations

import asyncio
import math
import stat
import struct
import threading
import time
import zipfile
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO

import av

from speechnexus_backend.workbench.application.export_worker import ProducedExport
from speechnexus_backend.workbench.domain.exports import ExportJob

_CHUNK_BYTES = 64 * 1024
_MEDIA_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "zip": "application/zip"}


class ExportMediaValidationError(ValueError):
    """A deliberately content-free failure exposed only inside the worker."""

    def __init__(self) -> None:
        super().__init__("export media validation failed")


@dataclass(frozen=True)
class ExportValidationLimits:
    """Explicit per-validation limits, supplied by the composition root.

    Audio seconds and unpacked bytes are cumulative across archive members;
    archive entries include directories and optional non-audio metadata.
    These limits are not hard process memory/CPU quotas.
    """

    max_output_bytes: int
    max_unpacked_bytes: int
    max_archive_entries: int
    max_audio_seconds: float
    timeout_seconds: float

    def __post_init__(self) -> None:
        for value in (self.max_output_bytes, self.max_unpacked_bytes, self.max_archive_entries):
            if type(value) is not int or value <= 0:
                raise ValueError("invalid export validation limit")
        for seconds in (self.max_audio_seconds, self.timeout_seconds):
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("invalid export validation limit")


@dataclass
class _Budget:
    limits: ExportValidationLimits
    stopped: threading.Event
    deadline: float
    audio_seconds: Fraction = Fraction(0)
    unpacked_bytes: int = 0

    def check(self) -> None:
        if self.stopped.is_set() or time.monotonic() >= self.deadline:
            raise ExportMediaValidationError


class _CheckedBytesIO(BytesIO):
    def __init__(self, content: bytes, budget: _Budget) -> None:
        super().__init__(content)
        self._budget = budget

    def read(self, size: int | None = -1) -> bytes:
        self._budget.check()
        return super().read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        self._budget.check()
        return super().seek(offset, whence)


class ExportMediaValidator:
    def __init__(self, limits: ExportValidationLimits) -> None:
        self._limits = limits

    async def validate_export(self, job: ExportJob, produced: ProducedExport) -> None:
        stopped = threading.Event()
        budget = _Budget(self._limits, stopped, time.monotonic() + self._limits.timeout_seconds)
        task = asyncio.create_task(asyncio.to_thread(self._validate, job, produced, budget))
        try:
            # wait() leaves the thread task alive so it can be stopped and joined.
            done, _ = await asyncio.wait({task}, timeout=self._limits.timeout_seconds)
            if not done:
                raise ExportMediaValidationError
            task.result()
        except asyncio.CancelledError:
            stopped.set()
            await self._join(task)
            raise
        except Exception:
            stopped.set()
            if await self._join(task):
                raise asyncio.CancelledError from None
            raise ExportMediaValidationError from None

    @staticmethod
    async def _join(task: asyncio.Task[None]) -> bool:
        # Even repeated cancellation must not strand validation in the executor.
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        return cancelled

    def _validate(self, job: ExportJob, produced: ProducedExport, budget: _Budget) -> None:
        budget.check()
        if (
            produced.media_type != _MEDIA_TYPES[job.format]
            or not produced.content
            or len(produced.content) > self._limits.max_output_bytes
        ):
            raise ExportMediaValidationError
        # Capture decoder diagnostics locally; no audio-derived text is logged.
        with av.logging.Capture(local=True):
            if job.format == "zip":
                self._validate_archive(produced.content, job.progress_total, budget)
            else:
                self._decode_audio(produced.content, job.format, budget)
        budget.check()

    def _decode_audio(self, content: bytes, audio_format: str, budget: _Budget) -> None:
        budget.check()
        if audio_format == "wav":
            self._check_wave(content, budget)
        with av.open(_CheckedBytesIO(content, budget), mode="r", format=audio_format) as container:
            if len(container.streams) != 1 or container.streams[0].type != "audio":
                raise ExportMediaValidationError
            stream = container.streams.audio[0]
            if audio_format == "mp3" and stream.codec_context.name not in ("mp3", "mp3float"):
                raise ExportMediaValidationError
            samples = 0
            for packet in container.demux(stream):
                budget.check()
                if packet.is_corrupt:
                    raise ExportMediaValidationError
                for frame in packet.decode():
                    budget.check()
                    if not isinstance(frame, av.AudioFrame) or frame.sample_rate <= 0:
                        raise ExportMediaValidationError
                    samples += frame.samples
                    budget.audio_seconds += Fraction(frame.samples, frame.sample_rate)
                    if budget.audio_seconds > Fraction(str(budget.limits.max_audio_seconds)):
                        raise ExportMediaValidationError
            if samples == 0:
                raise ExportMediaValidationError

    @staticmethod
    def _check_wave(content: bytes, budget: _Budget) -> None:
        # FFmpeg can decode truncated WAVs. Check RIFF chunk sizes independently.
        if (
            len(content) < 12
            or content[:4] != b"RIFF"
            or content[8:12] != b"WAVE"
            or struct.unpack_from("<I", content, 4)[0] + 8 != len(content)
        ):
            raise ExportMediaValidationError
        position, alignment, data_size = 12, 0, 0
        while position < len(content):
            budget.check()
            if position + 8 > len(content):
                raise ExportMediaValidationError
            kind, size = struct.unpack_from("<4sI", content, position)
            start = position + 8
            end = start + size
            if end + (size % 2) > len(content):
                raise ExportMediaValidationError
            if kind == b"fmt ":
                if size < 16 or alignment:
                    raise ExportMediaValidationError
                alignment = struct.unpack_from("<H", content, start + 12)[0]
            elif kind == b"data":
                if not alignment or not size or size % alignment or data_size:
                    raise ExportMediaValidationError
                data_size = size
            position = end + (size % 2)
        if not alignment or not data_size:
            raise ExportMediaValidationError

    def _validate_archive(self, content: bytes, expected: int, budget: _Budget) -> None:
        self._check_directory(content, budget)
        names: set[str] = set()
        audio_count = 0
        with zipfile.ZipFile(_CheckedBytesIO(content, budget)) as archive:
            entries = archive.infolist()
            if len(entries) > budget.limits.max_archive_entries:
                raise ExportMediaValidationError
            # Reject declared inflation before allocating any member buffer.
            if sum(entry.file_size for entry in entries) > budget.limits.max_unpacked_bytes:
                raise ExportMediaValidationError
            for entry in entries:
                budget.check()
                self._check_entry(entry, names)
                data = bytearray()
                extension = entry.filename.rsplit(".", 1)[-1].lower()
                is_audio = not entry.is_dir() and extension in ("wav", "mp3")
                first_chunk = True
                with archive.open(entry) as member:
                    while chunk := member.read(_CHUNK_BYTES):
                        budget.check()
                        budget.unpacked_bytes += len(chunk)
                        if budget.unpacked_bytes > budget.limits.max_unpacked_bytes:
                            raise ExportMediaValidationError
                        if first_chunk and not is_audio and self._has_audio_marker(chunk):
                            # Conservative rejection of disguised audio; a marker
                            # is never considered successful decoding or counted.
                            raise ExportMediaValidationError
                        first_chunk = False
                        if is_audio:
                            data.extend(chunk)
                if is_audio:
                    self._decode_audio(bytes(data), extension, budget)
                    audio_count += 1
                    if audio_count > expected:
                        raise ExportMediaValidationError
        if audio_count != expected:
            raise ExportMediaValidationError

    @staticmethod
    def _has_audio_marker(prefix: bytes) -> bool:
        return (
            (len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE")
            or prefix.startswith(b"ID3")
            or (len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0)
        )

    @staticmethod
    def _check_entry(entry: zipfile.ZipInfo, names: set[str]) -> None:
        name = entry.orig_filename
        path = name[:-1] if name.endswith("/") else name
        parts = path.split("/")
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if (
            not path
            or "\\" in name
            or ":" in name
            or any(ord(character) < 32 for character in name)
            or any(part in ("", ".", "..") for part in parts)
            or path in names
            or entry.flag_bits & 1
            or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
            or (kind == stat.S_IFDIR and not entry.is_dir())
            or (kind == stat.S_IFREG and entry.is_dir())
            or (entry.is_dir() and entry.file_size)
        ):
            raise ExportMediaValidationError
        names.add(path)

    @staticmethod
    def _check_directory(content: bytes, budget: _Budget) -> None:
        # Bound actual central-directory records before ZipFile allocates one
        # object per entry; a forged EOCD count cannot bypass the entry budget.
        end = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
        if end < 0 or end + 22 > len(content):
            raise ExportMediaValidationError
        disk, start_disk, disk_count, count, size, offset, comment = struct.unpack_from(
            "<4H2IH", content, end + 4
        )
        directory_end = end
        locator = end - 20
        if locator >= 0 and content[locator : locator + 4] == b"PK\x06\x07":
            locator_disk, record, disks = struct.unpack_from("<IQI", content, locator + 4)
            if locator_disk or disks != 1 or record + 56 > locator:
                raise ExportMediaValidationError
            if content[record : record + 4] != b"PK\x06\x06":
                raise ExportMediaValidationError
            record_size = struct.unpack_from("<Q", content, record + 4)[0]
            if record_size < 44 or record + 12 + record_size != locator:
                raise ExportMediaValidationError
            disk64, start_disk64, disk_count64, count64, size64, offset64 = struct.unpack_from(
                "<II4Q", content, record + 16
            )
            if (
                disk64
                or start_disk64
                or disk_count64 != count64
                or disk_count not in (65535, disk_count64)
                or count not in (65535, count64)
                or size not in (0xFFFFFFFF, size64)
                or offset not in (0xFFFFFFFF, offset64)
            ):
                raise ExportMediaValidationError
            disk_count, count, size, offset = disk_count64, count64, size64, offset64
            directory_end = record
        elif count == 65535 or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            raise ExportMediaValidationError
        if (
            disk
            or start_disk
            or disk_count != count
            or count > budget.limits.max_archive_entries
            or end + 22 + comment != len(content)
            or offset + size != directory_end
        ):
            raise ExportMediaValidationError
        position, actual = offset, 0
        while position < directory_end:
            budget.check()
            if position + 46 > directory_end or content[position : position + 4] != b"PK\x01\x02":
                raise ExportMediaValidationError
            name, extra, entry_comment = struct.unpack_from("<3H", content, position + 28)
            position += 46 + name + extra + entry_comment
            actual += 1
            if actual > budget.limits.max_archive_entries:
                raise ExportMediaValidationError
        if position != directory_end or actual != count:
            raise ExportMediaValidationError
