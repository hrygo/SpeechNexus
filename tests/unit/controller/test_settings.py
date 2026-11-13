"""Production node identities fail closed before constructing runtime dependencies."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from speechnexus_node_controller.settings import Settings


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use only synthetic settings, without a checkout .env or inherited credentials."""
    monkeypatch.chdir(tmp_path)
    for name in os.environ:
        if name.startswith("SPEECHNEXUS_CONTROLLER_"):
            monkeypatch.delenv(name)


@pytest.fixture
def required_settings(tmp_path: Path) -> dict[str, Any]:
    return {
        "component_token": "synthetic-controller-component-token",
        "approved_templates_file": tmp_path / "templates.json",
        "intent_store_path": tmp_path / "intents.sqlite3",
    }


@pytest.mark.parametrize("node_id", ["node-cube-01", "node-studio-02", "a-1", "n" * 128])
def test_production_preserves_explicit_node_identity(
    required_settings: dict[str, Any], node_id: str
) -> None:
    settings = Settings(environment="production", node_id=node_id, **required_settings)

    assert settings.node_id == node_id
    assert not settings.intent_store_path.exists()


def test_production_loads_explicit_identity_from_environment(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", "production")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODE_ID", "node-cube-01")

    settings = Settings(**required_settings)

    assert settings.environment == "production"
    assert settings.node_id == "node-cube-01"


def test_production_loads_explicit_identity_from_dotenv(
    required_settings: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "SPEECHNEXUS_CONTROLLER_ENVIRONMENT=production\n"
        "SPEECHNEXUS_CONTROLLER_NODE_ID=node-cube-01\n",
        encoding="utf-8",
    )

    assert Settings(**required_settings).node_id == "node-cube-01"


def test_production_rejects_missing_identity(required_settings: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="explicit non-default node_id"):
        Settings(environment="production", **required_settings)


@pytest.mark.parametrize("source", ["missing", "default"])
def test_production_rejects_missing_or_default_environment_identity(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", "production")
    if source == "default":
        monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODE_ID", "node-local-1")

    with pytest.raises(ValidationError, match="explicit non-default node_id"):
        Settings(**required_settings)


def test_a_misspelled_identity_key_is_refused_by_name(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SPEECHNEXUS_CONTROLLER_NODEID`` used to be dropped without a word (#674)."""
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", "production")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODEID", "node-cube-01")

    with pytest.raises(ValidationError) as error:
        Settings(**required_settings)

    message = str(error.value)
    assert "SPEECHNEXUS_CONTROLLER_NODEID" in message
    assert "SPEECHNEXUS_CONTROLLER_NODE_ID" in message


def test_production_rejects_explicit_development_default(
    required_settings: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="explicit non-default node_id"):
        Settings(environment="production", node_id="node-local-1", **required_settings)


def test_production_identity_error_does_not_render_component_credential(
    required_settings: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(environment="production", **required_settings)

    assert required_settings["component_token"] not in str(error.value)


@pytest.mark.parametrize(
    "node_id",
    ["", " ", "ab", "node 1", "NODE-1", "node/1", "node-1 ", "node-1\n", "n" * 129],
)
def test_production_rejects_invalid_identity(
    required_settings: dict[str, Any], node_id: str
) -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", node_id=node_id, **required_settings)


@pytest.mark.parametrize("environment", ["prod", "Production", ""])
def test_invalid_environment_cannot_bypass_production_validation(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", environment)
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODE_ID", "node-cube-01")

    with pytest.raises(ValidationError):
        Settings(**required_settings)


@pytest.mark.parametrize("environment", ["development", "test", "staging"])
def test_nonproduction_retains_implicit_and_explicit_development_default(
    required_settings: dict[str, Any], environment: str
) -> None:
    assert Settings(environment=environment, **required_settings).node_id == "node-local-1"
    assert (
        Settings(environment=environment, node_id="node-local-1", **required_settings).node_id
        == "node-local-1"
    )


def test_omitted_environment_retains_development_behavior(
    required_settings: dict[str, Any],
) -> None:
    settings = Settings(**required_settings)

    assert settings.environment == "development"
    assert settings.node_id == "node-local-1"
