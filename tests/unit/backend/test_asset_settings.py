"""Explicit asset runtime limits, network policy and secret-safe validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from speechnexus_backend.settings import Settings
from tests.unit.backend.test_health import _settings


@pytest.mark.parametrize(
    "changes",
    [
        {"asset_default_ttl_seconds": 59},
        {"asset_default_ttl_seconds": 31536001},
        {"asset_max_bytes": 4 * 1024 * 1024 + 1},
        {"asset_upload_concurrency": 5},
        {"asset_receive_timeout_seconds": 0},
        {"asset_maintenance_batch_size": 101},
        {"database_schema": "speech;DROP"},
        {"asset_allowed_hosts": ("*",)},
        {"asset_allowed_hosts": ("user@trusted.test",)},
        {"asset_allowed_hosts": ("http://trusted.test",)},
        {"asset_allowed_hosts": ("trusted.test:8000",)},
        {"asset_allowed_origins": ("null",)},
        {"asset_allowed_origins": ("https://trusted.test/path",)},
        {"asset_allowed_origins": ("https://user:private@trusted.test",)},
    ],
)
def test_invalid_runtime_configuration_does_not_silently_fall_back(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**(_settings().model_dump() | changes))


def test_credentials_are_paired_and_validation_does_not_print_input() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(
            **(
                _settings().model_dump()
                | {"object_storage_access_key": SecretStr("private_test_key")}
            )
        )
    assert "private_test_key" not in str(caught.value)


def test_retention_omission_and_network_defaults_are_explicit() -> None:
    settings = _settings()
    assert settings.asset_default_ttl_seconds is None
    assert settings.asset_allowed_hosts == ("localhost", "127.0.0.1", "::1")
    assert settings.asset_allowed_origins == ()
    configured = Settings(
        **(
            settings.model_dump()
            | {
                "asset_allowed_hosts": ("trusted.test", "::1"),
                "asset_allowed_origins": ("https://trusted.test",),
                "asset_default_ttl_seconds": 604800,
            }
        )
    )
    assert configured.asset_default_ttl_seconds == 604800
