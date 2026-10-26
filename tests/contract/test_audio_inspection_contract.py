"""The actual isolated producer satisfies the canonical generated success contract."""

import io

from speechnexus_contracts.audio_inspection import parse

from speechnexus_media.domain import AudioDecodeLimits, AudioSourceFormat
from speechnexus_media.inspection import inspect_audio
from tests.unit.media.test_audio_inspection import wav


def test_actual_codec_producer_satisfies_the_generated_success_contract() -> None:
    value = inspect_audio(
        io.BytesIO(wav()),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    assert parse("AudioInspectionSuccess", value) is value
