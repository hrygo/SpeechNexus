"""Every configured key must either take effect, name its prerequisite, or warn.

This module is the executable half of the configuration-prerequisite scan
(``tests/ci/configuration_prerequisites.py`` and
``docs/operations/configuration-prerequisites.md``). Issue #660: a key that is
accepted at startup but never used is worse than an absent one -- the operator
cannot tell a typo, a missing companion key or an intentionally disabled
capability apart from the documented "not configured" 503.

The refusals asserted here are construction-time only; no PostgreSQL connection is
opened unless a test says so (the cross-database cases live in
``tests/integration/test_job_database_pair_pg.py`` because they need two real
databases).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import AnyHttpUrl, SecretStr

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech_runtime import build_speech
from speechnexus_backend.transcription_runtime import build_transcription
from tests.ci.configuration_prerequisites import API_ROLE_IDENTITIES
from tests.unit.backend.test_health import _settings

JOB_WRITER = "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
JOB_ADMISSION = "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"
VOICE_PUBLISHER = "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL"
DSN = "postgresql://identity@127.0.0.1:1/speechnexus"


def _field(variable: str) -> str:
    return variable.removeprefix("SPEECHNEXUS_").lower()


def _base(**changes: Any) -> Settings:
    """Settings that compose a whole API process without any optional identity.

    Object-storage credentials are supplied because the asset runtime is the base
    of several capability chains; no pool is opened by the constructors.
    """
    return Settings(
        **_settings().model_dump()
        | {
            "object_storage_access_key": SecretStr("test-key"),
            "object_storage_secret_key": SecretStr("test-secret"),
        }
        | changes
    )


@pytest.mark.parametrize("companion", [JOB_ADMISSION, VOICE_PUBLISHER])
def test_a_job_companion_dsn_without_the_writer_names_both_keys(companion: str) -> None:
    """The publisher/admission pools are created by the Job writer runtime (#660).

    Configuring either DSN alone used to start clean and keep the capability at
    its documented 503, indistinguishable from an intentional absence.
    """
    settings = _base(**{_field(companion): SecretStr(DSN)})

    with pytest.raises(ValueError) as caught:
        create_app(settings=settings)

    message = str(caught.value)
    assert companion in message
    assert JOB_WRITER in message
    assert "unset" in message and "set SPEECHNEXUS_JOB_WRITER_DATABASE_URL" in message


def test_a_job_companion_dsn_is_accepted_when_the_writer_is_configured() -> None:
    """The refusal is about the missing writer, not about the companion DSN."""
    settings = _base(
        **{
            _field(JOB_WRITER): SecretStr(DSN),
            _field(JOB_ADMISSION): SecretStr(DSN),
            _field(VOICE_PUBLISHER): SecretStr(DSN),
        }
    )

    app = create_app(settings=settings)

    runtime = app.state.job_runtime
    assert runtime is not None
    assert runtime._settings.job_admission_database_url is not None
    assert runtime._settings.voice_publisher_database_url is not None


@pytest.mark.parametrize("variable", API_ROLE_IDENTITIES)
def test_a_non_api_role_refuses_every_identity_dsn(variable: str) -> None:
    """An identity DSN is only composed for the API role; other roles refuse.

    The message names the identity rather than the variable -- that is the
    pre-existing subject style and this card does not rewrite it.
    """
    settings = _base(
        process_role="scheduler",
        **{_field(variable): SecretStr(DSN)},
    )

    with pytest.raises(ValueError, match="Configure an explicit API"):
        create_app(settings=settings)


def test_the_profile_writer_and_evidence_document_name_each_other() -> None:
    """Either half alone stops startup and names the other key."""
    evidence = "tests/unit/backend/test_startup_key_pairs.py"

    with pytest.raises(ValueError) as without_writer:
        create_app(settings=_base(profile_publication_evidence_file=evidence))
    assert "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE" in str(without_writer.value)
    assert "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL" in str(without_writer.value)

    with pytest.raises(ValueError) as without_evidence:
        create_app(
            settings=_base(profile_writer_database_url=SecretStr(DSN)),
        )
    assert "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE" in str(without_evidence.value)


@pytest.mark.parametrize("missing", ["secret", "access"])
def test_a_partial_object_storage_pair_is_refused(missing: str) -> None:
    """A lone object-storage key is a configuration error, not a disabled store."""
    pair = {
        "object_storage_access_key": SecretStr("test-key"),
        "object_storage_secret_key": SecretStr("test-secret"),
    }
    pair.pop(f"object_storage_{missing}_key")

    with pytest.raises(ValueError, match="object-storage"):
        Settings(**_settings().model_dump() | pair)


@pytest.mark.parametrize("missing", ["component_credential", "base_url"])
def test_a_partial_execution_gateway_pair_is_refused(missing: str) -> None:
    """URL and component credential are one configuration, never one of two."""
    pair: dict[str, Any] = {
        "execution_gateway_base_url": AnyHttpUrl("http://127.0.0.1:8010"),
        "execution_gateway_component_credential": SecretStr("component-credential-0001"),
    }
    pair.pop(f"execution_gateway_{missing}")

    with pytest.raises(ValueError, match="execution-gateway"):
        Settings(**_settings().model_dump() | pair)


def test_both_binding_writer_modes_are_refused() -> None:
    """Two explicit binding writer modes cannot be resolved by preference."""
    with pytest.raises(ValueError, match="one explicit binding writer mode"):
        Settings(
            **_settings().model_dump()
            | {
                "binding_writer_database_url": SecretStr(DSN),
                "binding_defaults_writer_database_url": SecretStr(DSN),
            }
        )


def _bare(**changes: Any) -> Settings:
    """Settings without object-storage credentials, for the full-set warnings."""
    return Settings(**_settings().model_dump() | changes)


def test_partial_transcription_configuration_warns_with_every_missing_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A partial synchronous-transcription configuration is reported, not silent."""
    caplog.set_level(logging.WARNING, logger="speechnexus_backend.transcription_runtime")

    assert build_transcription(_bare(transcription_input_bucket="speechnexus-inputs")) is None

    message = caplog.records[-1].getMessage()
    for variable in (
        "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE",
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
        "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY",
        "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY",
    ):
        assert variable in message


def test_partial_speech_configuration_warns_with_every_missing_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A partial synchronous-TTS configuration is reported, not silent."""
    caplog.set_level(logging.WARNING, logger="speechnexus_backend.speech_runtime")

    assert build_speech(_bare(speech_default_language="zh")) is None

    message = caplog.records[-1].getMessage()
    for variable in (
        "SPEECHNEXUS_SPEECH_RECIPE_FILE",
        "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL",
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
        "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY",
        "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY",
    ):
        assert variable in message
