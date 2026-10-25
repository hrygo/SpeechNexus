"""Small deterministic, decodable media; never real speech or model output."""

from io import BytesIO
from wave import open as open_wave
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from speechnexus_backend.workbench.infrastructure.export_media_validation import (
    ExportMediaValidator,
    ExportValidationLimits,
)


def export_validator() -> ExportMediaValidator:
    return ExportMediaValidator(ExportValidationLimits(5_000_000, 10_000_000, 100, 60, 3))


def synthetic_wav(*, frames: int = 320, rate: int = 16000) -> bytes:
    stream = BytesIO()
    with open_wave(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\x00\x00" * frames)
    return stream.getvalue()


def synthetic_export_zip(*, chapters: int = 2) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED) as output:
        for ordinal in range(chapters):
            output.writestr(ZipInfo(f"chapter-{ordinal + 1:03d}.wav"), synthetic_wav())
    return stream.getvalue()
