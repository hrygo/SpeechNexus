"""The acceptance entry reads the gateway lanes the way the gateway does.

`deploy/compose.yaml` renders the three resident TTS lanes as `${VAR-}`, so a
default render has `SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE=""` and
`..._VOICE_CLONE_CONFIG_FILE=""`: the keys exist, no lane is configured. The
gateway already reads a blank optional value as "not configured"
(`settings.py`'s `blank_optional_values_are_unconfigured`), and this entry must
agree, or a default deployment fails its own dry run before reaching `tools`
(#1075).

The other direction matters just as much: a lane whose path is non-empty but
outside the private mount must still be refused, so skipping blanks cannot turn
into skipping the path rule.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "scripts/dev-env/reference-acceptance.py"
PRIVATE = "/run/speechnexus-private"


def entry_module() -> Any:
    spec = importlib.util.spec_from_file_location("reference_acceptance_lanes", ENTRY)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARNESS: Any = entry_module()


def config_with(**environment: str) -> dict[str, object]:
    """The compose-config shape the lane helpers read, without a Docker run."""
    return {"services": {"gateway": {"environment": dict(environment)}}}


def context(env: dict[str, str] | None = None) -> Any:
    return HARNESS.Context(
        env={"SPEECHNEXUS_PRIVATE_CONFIG_DIR": "deploy/private"} if env is None else env,
        env_file=ROOT / "deploy" / "compose.env.example",
        compose_file=ROOT / "deploy" / "compose.yaml",
        project="sn-lane-guard",
        runner=None,
        dry_run=True,
        gate_mode="compose",
        repo_root=ROOT,
    )


def test_a_default_render_names_only_the_lane_it_fills() -> None:
    # `deploy/compose.env.example`'s default: CustomVoice filled, both voice lanes
    # blank. One lane must come back, not three.
    config = config_with(
        SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE=f"{PRIVATE}/gateway-file-tts.json",
        SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE="",
        SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_CONFIG_FILE="",
    )
    assert HARNESS.gateway_lane_config_paths(config) == [f"{PRIVATE}/gateway-file-tts.json"]


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_blank_values_are_not_lanes(blank: str) -> None:
    config = config_with(
        SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE=blank,
        SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE=f"{PRIVATE}/gateway-file-voice-design.json",
        SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_CONFIG_FILE=blank,
    )
    assert HARNESS.gateway_lane_config_paths(config) == [
        f"{PRIVATE}/gateway-file-voice-design.json"
    ]


def test_padding_around_a_path_is_not_part_of_the_path() -> None:
    config = config_with(
        SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE=f"  {PRIVATE}/gateway-file-tts.json  ",
    )
    assert HARNESS.gateway_lane_config_paths(config) == [f"{PRIVATE}/gateway-file-tts.json"]


def test_only_the_named_keys_are_read() -> None:
    config = config_with(
        SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE=f"{PRIVATE}/gateway-file-tts.json",
        SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_ACCESS_KEY="not-a-lane.json",
        SPEECHNEXUS_OTHER_CONFIG_FILE=f"{PRIVATE}/not-a-lane.json",
    )
    assert HARNESS.gateway_lane_config_paths(config) == [f"{PRIVATE}/gateway-file-tts.json"]


def test_a_default_render_is_not_a_missing_lane() -> None:
    # All three blank means "no lane configured", and that is the branch that must
    # still report it - the failure just has to be about the deployment, not about
    # an empty path that was never a path.
    config = config_with(
        SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE="",
        SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE="",
        SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_CONFIG_FILE="",
    )
    assert HARNESS.gateway_lane_config_paths(config) == []
    with pytest.raises(HARNESS.StepFailureError) as raised:
        HARNESS.gateway_lane_configs(context(), config)
    assert "没有配置任何 file lane 配置文件" in str(raised.value)


def test_a_lane_outside_the_private_mount_is_still_refused() -> None:
    # The other direction: a non-empty path the mount does not serve stays a
    # failure, so "skip blanks" cannot be read as "skip the path rule".
    config = config_with(SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE="/tmp/not-private.json")
    with pytest.raises(HARNESS.StepFailureError) as raised:
        HARNESS.gateway_lane_configs(context(), config)
    assert f"私有配置路径不在 {PRIVATE} 下: /tmp/not-private.json" == str(raised.value)


def test_a_blank_lane_does_not_hide_the_next_illegal_one() -> None:
    config = config_with(
        SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE="",
        SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE="/tmp/not-private.json",
    )
    with pytest.raises(HARNESS.StepFailureError) as raised:
        HARNESS.gateway_lane_configs(context(), config)
    assert "/tmp/not-private.json" in str(raised.value)
