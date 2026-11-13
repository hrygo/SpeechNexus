"""Gateway configuration pairs refuse a half-configured launch, naming the keys.

Issue #660: the read-only query pool belongs to the execution-control lifespan. A
``SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL`` configured without a component
token used to be accepted and never opened, so an operator could believe a
separate reader identity was in use while the gateway ran without control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from speechnexus_gateway.settings import Settings

CONTROL = "postgresql://sn_gateway@127.0.0.1:5432/speechnexus"
QUERY = "postgresql://sn_gateway_query@127.0.0.1:5432/speechnexus"
TOKEN = SecretStr("synthetic-gateway-component-token")


def _settings(**changes: Any) -> Settings:
    return Settings(control_database_url=SecretStr(CONTROL), **changes)


def test_a_query_dsn_without_execution_control_names_both_keys() -> None:
    with pytest.raises(ValueError) as caught:
        _settings(control_query_database_url=SecretStr(QUERY))

    message = str(caught.value)
    assert "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL" in message
    assert "SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN" in message
    assert "lock directory" in message


def test_the_query_dsn_is_accepted_with_execution_control(tmp_path: Path) -> None:
    settings = _settings(
        component_token=TOKEN,
        ingress_lock_directory=tmp_path,
        node_id="node-1",
        gateway_id="gateway-1",
        control_query_database_url=SecretStr(QUERY),
    )

    assert settings.control_query_database_url is not None
    assert settings.execution_control_enabled


PARTIAL_PAIRS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "component-token-without-lock-directory",
        {"component_token": TOKEN},
        "component token and a lock directory",
    ),
    (
        "file-asr-config-without-object-credentials",
        {"file_asr_config_file": Path("/run/speechnexus-private/file-asr.json")},
        "file ASR requires a configuration and both object credentials",
    ),
    (
        "file-tts-object-key-without-configuration",
        {"file_tts_object_access_key": SecretStr("test-key")},
        "file TTS requires a configuration and both object credentials",
    ),
    (
        "recovery-config-without-node-credential",
        {"recovery_config_file": Path("/run/speechnexus-private/recovery.json")},
        "recovery requires both a configuration file and a node credential",
    ),
)


@pytest.mark.parametrize(
    ("case", "changes", "expected"),
    [(case, changes, expected) for case, changes, expected in PARTIAL_PAIRS],
    ids=[case for case, _, _ in PARTIAL_PAIRS],
)
def test_a_partial_file_engine_configuration_is_refused(
    case: str, changes: dict[str, Any], expected: str
) -> None:
    """Every gateway pair refuses alone, whatever the missing half is."""
    with pytest.raises(ValueError, match=expected):
        _settings(**changes)


def test_blank_optional_values_are_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KEY=`` means "not configured": the gateway must still start (#660 review FP-2)."""
    monkeypatch.setenv("SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL", CONTROL)
    for name in (
        "CONTROL_QUERY_DATABASE_URL",
        "COMPONENT_TOKEN",
        "INGRESS_LOCK_DIRECTORY",
        "RECOVERY_CONFIG_FILE",
        "RECOVERY_NODE_COMPONENT_TOKEN",
        "FILE_ASR_CONFIG_FILE",
        "FILE_TTS_OBJECT_ACCESS_KEY",
    ):
        monkeypatch.setenv("SPEECHNEXUS_GATEWAY_" + name, "")

    settings = Settings()

    assert settings.control_query_database_url is None
    assert settings.component_token is None
    assert settings.execution_control_enabled is False


def test_a_blank_query_dsn_does_not_hide_a_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-blank query DSN is a real configuration and still names the token."""
    monkeypatch.setenv("SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL", CONTROL)
    monkeypatch.setenv("SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL", QUERY)
    monkeypatch.setenv("SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN", "")

    with pytest.raises(ValueError) as caught:
        Settings()

    message = str(caught.value)
    assert "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL" in message
    assert "SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN" in message
