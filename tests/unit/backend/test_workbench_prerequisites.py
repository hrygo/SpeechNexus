"""Workbench optional zones are configured whole or not at all.

Issue #660: a lone ``SPEECHNEXUS_WORKBENCH_LLM_API_KEY`` was accepted and never
read, so the settings said an intranet LLM credential existed while every assistant
route stayed off.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

BASE: dict[str, Any] = {
    "database_url": SecretStr("postgresql://workbench@127.0.0.1:5432/speechnexus"),
    "database_schema": "speech_workbench",
    "workspace_id": "workspace-default",
    "deployment_id": "workbench-local",
    "cursor_key": SecretStr("synthetic-workbench-cursor-key-0001"),
}


def _settings(**changes: Any) -> WorkbenchSettings:
    return WorkbenchSettings(**(BASE | changes))


def test_a_partial_llm_configuration_is_refused() -> None:
    with pytest.raises(ValueError) as key_alone:
        _settings(llm_api_key=SecretStr("synthetic-llm-key"))
    message = str(key_alone.value)
    assert "SPEECHNEXUS_WORKBENCH_LLM_API_KEY" in message
    assert "SPEECHNEXUS_WORKBENCH_LLM_ENDPOINT" in message
    assert "SPEECHNEXUS_WORKBENCH_LLM_MODEL" in message
    assert "SPEECHNEXUS_WORKBENCH_LLM_QUALIFICATION_REFERENCE" in message

    with pytest.raises(ValueError, match="whole intranet text LLM"):
        _settings(llm_endpoint="http://127.0.0.1:8000/v1/chat/completions")


def test_a_complete_llm_configuration_builds() -> None:
    settings = _settings(
        llm_endpoint="http://127.0.0.1:8000/v1/chat/completions",
        llm_model="synthetic-intranet-model",
        llm_qualification_reference="qualification:synthetic",
    )

    assert settings.llm_configuration().enabled is True


def test_a_partial_saved_asset_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="whole saved-asset object zone"):
        _settings(speech_base_url="http://127.0.0.1:8000")


def test_blank_optional_values_are_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KEY=`` means "not configured": the deployment must still start (#660 review FP-1).

    Env templates and platform env lists spell an unset optional key as an empty
    value. Before the blank→``None`` normalization the LLM pair validator read
    ``SecretStr('')`` as a configured credential and refused a previously valid
    deployment.
    """
    values = {
        "DATABASE_URL": "postgresql://workbench@127.0.0.1:5432/speechnexus",
        "DATABASE_SCHEMA": "speech_workbench",
        "WORKSPACE_ID": "workspace-default",
        "DEPLOYMENT_ID": "workbench-local",
        "CURSOR_KEY": "synthetic-workbench-cursor-key-0001",
        "LLM_ENDPOINT": "",
        "LLM_MODEL": "",
        "LLM_API_KEY": "",
        "LLM_QUALIFICATION_REFERENCE": "",
        "SPEECH_BASE_URL": "",
        "OBJECT_ENDPOINT": "",
        "OBJECT_BUCKET": "",
        "OBJECT_ACCESS_KEY": "",
        "OBJECT_SECRET_KEY": "",
        "CLEANUP_DATABASE_URL": "",
    }
    for name, value in values.items():
        monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_" + name, value)

    settings = WorkbenchSettings()

    assert settings.llm_api_key is None
    assert settings.llm_configuration().enabled is False


def test_a_blank_llm_key_does_not_hide_a_missing_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-blank value is a real configuration and still names its companions."""
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_DATABASE_URL", "postgresql://w@127.0.0.1/db")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA", "speech_workbench")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_WORKSPACE_ID", "workspace-default")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID", "workbench-local")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_CURSOR_KEY", "synthetic-workbench-cursor-key-0001")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_LLM_API_KEY", "synthetic-llm-key")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_LLM_ENDPOINT", "")

    with pytest.raises(ValueError) as caught:
        WorkbenchSettings()

    message = str(caught.value)
    assert "SPEECHNEXUS_WORKBENCH_LLM_API_KEY" in message
    assert "SPEECHNEXUS_WORKBENCH_LLM_ENDPOINT" in message
