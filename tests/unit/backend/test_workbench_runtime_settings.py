"""Explicit, bounded, secret-safe configuration of the independent workbench."""

from typing import Any

import pytest
from pydantic import ValidationError

from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings


def configuration(**changes: Any) -> WorkbenchSettings:
    return WorkbenchSettings(
        **{
            "database_url": "host=/synthetic-private user=synthetic password=DO_NOT_ECHO_PASSWORD",
            "database_schema": "workbench",
            "workspace_id": "workspace-synthetic",
            "deployment_id": "synthetic-deployment",
            "cursor_key": "DO_NOT_ECHO_CURSOR_KEY_1234567890123456789",
            **changes,
        }
    )


def test_export_validation_has_explicit_execution_limits() -> None:
    settings = configuration()
    assert settings.export_max_output_bytes == 256 * 1024**2
    assert settings.export_max_unpacked_bytes == 512 * 1024**2
    assert settings.export_max_archive_entries == 1024
    assert settings.export_max_audio_seconds == 36000
    assert settings.export_validation_timeout_seconds == 30


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("export_max_output_bytes", 0),
        ("export_max_unpacked_bytes", -1),
        ("export_max_archive_entries", 0),
        ("export_max_audio_seconds", float("inf")),
        ("export_validation_timeout_seconds", float("nan")),
    ],
)
def test_export_validation_limits_refuse_unbounded_configuration(field: str, value: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        configuration(**{field: value})
    assert all(error["type"] != "extra_forbidden" for error in caught.value.errors())


def test_runtime_requires_configuration_and_never_reads_an_implicit_dotenv(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("DATABASE_URL", "DATABASE_SCHEMA", "WORKSPACE_ID", "DEPLOYMENT_ID", "CURSOR_KEY"):
        monkeypatch.delenv("SPEECHNEXUS_WORKBENCH_" + name, raising=False)
    (tmp_path / ".env").write_text("SPEECHNEXUS_WORKBENCH_DATABASE_URL=implicit-secret")
    with pytest.raises(ValidationError) as caught:
        WorkbenchSettings()
    assert "database_url" in str(caught.value) and "implicit-secret" not in str(caught.value)


def test_settings_keep_credentials_out_of_repr_and_errors() -> None:
    value = configuration()
    assert "DO_NOT_ECHO" not in repr(value)
    with pytest.raises(ValidationError) as caught:
        configuration(max_connections=0)
    assert "DO_NOT_ECHO" not in str(caught.value)
    assert value.max_connections == 8 and value.max_json_bytes == 1048576


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("database_url", ""),
        ("database_schema", "speech"),
        ("database_schema", "public"),
        ("database_schema", "pg_temp"),
        ("database_schema", "invalid-schema"),
        ("workspace_id", ""),
        ("workspace_id", "contains space"),
        ("deployment_id", ""),
        ("cursor_key", "too-short"),
        ("max_connections", True),
        ("max_connections", 65),
        ("max_json_bytes", 0),
        ("body_timeout_seconds", 0),
        ("allowed_hosts", ()),
        ("allowed_hosts", ("*",)),
        ("allowed_hosts", ("https://localhost",)),
        ("allowed_hosts", ("localhost:80",)),
        ("allowed_origins", ("*",)),
        ("allowed_origins", ("http://user:secret@localhost",)),
        ("allowed_origins", ("http://localhost/path",)),
        ("allowed_origins", ("http://localhost:invalid",)),
    ],
)
def test_invalid_runtime_configuration_is_rejected(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        configuration(**{key: value})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("allowed_origins", ("http://localhost:DO_NOT_ECHO_SYNTHETIC_INPUT",)),
        ("allowed_origins", ("http://[DO_NOT_ECHO_SYNTHETIC_INPUT",)),
        ("allowed_hosts", ("localhost:DO_NOT_ECHO_SYNTHETIC_INPUT",)),
    ],
)
def test_url_configuration_parse_failure_never_echoes_its_input(key: str, value: object) -> None:
    # urlsplit's port/host accessors raise messages that quote the raw input. A
    # configuration parse failure must not turn a private deployment value into a
    # validation message, log line or handler response.
    with pytest.raises(ValidationError) as caught:
        configuration(**{key: value})
    assert "DO_NOT_ECHO_SYNTHETIC_INPUT" not in str(caught.value)


def test_explicit_environment_uses_a_separate_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_DATABASE_URL", "synthetic-private-connection")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA", "wb_explicit")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_WORKSPACE_ID", "workspace-explicit")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID", "deployment-explicit")
    monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_CURSOR_KEY", "synthetic-32-character-cursor-key-only")
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_URL", "unrelated-shared-connection")
    settings = WorkbenchSettings()
    assert settings.database_url.get_secret_value() == "synthetic-private-connection"
    assert settings.workspace_id == "workspace-explicit"


def test_owner_baseline_also_refuses_speech_namespace() -> None:
    from speechnexus_backend.workbench.infrastructure.runtime_schema import (
        bootstrap_sql,
        runtime_grants_sql,
    )

    with pytest.raises(ValueError):
        bootstrap_sql("speech")
    with pytest.raises(ValueError):
        runtime_grants_sql("speech", "synthetic_runtime")


LOCAL_LLM = {
    "llm_endpoint": "http://127.0.0.1:8000/v1/chat/completions",
    "llm_model": "synthetic-local-model",
    "llm_qualification_reference": "adr-0003-approved-local-intranet-text-llm",
}


def test_the_text_llm_stays_off_until_the_deployment_configures_the_whole_binding() -> None:
    assert configuration().llm_configuration().profile is None
    for partial in (
        {"llm_endpoint": LOCAL_LLM["llm_endpoint"]},
        {"llm_model": LOCAL_LLM["llm_model"]},
        {"llm_qualification_reference": LOCAL_LLM["llm_qualification_reference"]},
    ):
        with pytest.raises(ValidationError):
            configuration(**partial)


def test_only_a_loopback_or_private_endpoint_can_carry_the_text_llm() -> None:
    for refused in (
        "https://api.openai.com/v1/chat/completions",
        "https://example.internal/v1/chat/completions",
        "http://127.0.0.1:8000/v1/chat",
        "http://10.20.30.40/v1/chat/completions",
        "http://127.0.0.1:8000/v1/chat/completions?key=synthetic",
    ):
        with pytest.raises(ValidationError) as caught:
            configuration(
                llm_model="synthetic-local-model",
                llm_qualification_reference="q",
                llm_endpoint=refused,
            )
        assert refused not in str(caught.value)
    private = configuration(
        **{**LOCAL_LLM, "llm_endpoint": "https://10.20.30.40/v1/chat/completions"}
    )
    assert private.llm_configuration().enabled is True


def test_a_configured_binding_pins_the_profile_and_keeps_the_credential_private() -> None:
    value = configuration(**LOCAL_LLM, llm_api_key="DO_NOT_ECHO_LLM_KEY", llm_template_revision=7)
    adapter = value.llm_configuration()
    assert adapter.enabled is True
    assert adapter.profile is not None
    assert (adapter.profile.model, adapter.profile.template_revision) == (
        "synthetic-local-model",
        7,
    )
    assert adapter.endpoint == LOCAL_LLM["llm_endpoint"]
    assert "DO_NOT_ECHO_LLM_KEY" not in repr(value)
    assert "DO_NOT_ECHO_LLM_KEY" not in repr(adapter)
    with pytest.raises(ValidationError):
        configuration(**LOCAL_LLM, llm_template_revision=0)
