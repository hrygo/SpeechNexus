"""Native media support stays within the frozen execution-input contract."""

from pathlib import Path

import pytest

from speechnexus_gateway.domain.media import AudioSourceFormat, MediaError
from tests.contract.test_p0_public_contracts import _load_json, _validator

_COMMAND_SCHEMA = Path("contracts/internal/execution-command.schema.json")
_MEDIA_TYPES = ["audio/wav", "audio/flac", "audio/mpeg", "audio/ogg", "audio/pcm"]


@pytest.mark.parametrize("media_type", _MEDIA_TYPES)
@pytest.mark.parametrize("rate,channels", [(8000, 1), (192000, 8)])
def test_supported_source_declarations_fit_the_execution_input(
    media_type: str, rate: int, channels: int
) -> None:
    source = AudioSourceFormat(media_type, rate, channels)
    document = _load_json(_COMMAND_SCHEMA)
    assert document["$defs"]["AudioInput"]["properties"]["media_type"]["enum"] == _MEDIA_TYPES
    _validator({"$defs": document["$defs"], "$ref": "#/$defs/AudioInput"}).validate(
        {
            "storage": {
                "asset_or_artifact_id": "asset-synthetic-1",
                "workspace_id": "workspace-synthetic-1",
                "storage_bucket": "synthetic-inputs",
                "object_key": "synthetic/input",
                "content_sha256": "sha256:" + "0" * 64,
                "byte_length": 32000,
            },
            "media_type": source.media_type,
            "sample_rate_hz": source.sample_rate_hz,
            "channels": source.channels,
        }
    )


@pytest.mark.parametrize("rate,channels", [(384000, 1), (48000, 32)])
def test_broader_wire_formats_are_rejected_by_the_unqualified_native_subset(
    rate: int, channels: int
) -> None:
    with pytest.raises(MediaError, match="unsupported source audio format"):
        AudioSourceFormat("audio/wav", rate, channels)
