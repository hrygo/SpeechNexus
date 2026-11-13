"""The prefixed-environment refusal and its false-positive boundary.

Issue #674: a variable that carries a service prefix but matches no declared
setting is dropped at load time, so a typo or a key placed in the wrong process
looks like a configured capability. These cases pin both directions of the rule:

* a misspelling is refused at startup and the message names the closest declared
  keys, so the operator can fix it without reading the source;
* everything that is legitimately not a setting keeps starting -- unrelated
  process variables, the deployment plane (Compose interpolation, the role
  runbook, the test harness), and a blank ``KEY=`` which means "unconfigured"
  and never "configured" (#660);
* a key that belongs to another entrypoint is named with its owner instead of
  being refused, because shared environment files are a legal deployment shape.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from speechnexus_backend.environment import (
    PLANES as BACKEND_PLANES,
)
from speechnexus_backend.environment import (
    WORKBENCH_PLANES,
    close_candidates,
    declared_environment_keys,
    environment_prefixes,
    refuse_unrecognised_prefixed_environment,
    unknown_prefixed_variables,
)
from speechnexus_backend.settings import Settings
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings
from speechnexus_gateway.environment import PLANES as GATEWAY_PLANES
from speechnexus_gateway.settings import Settings as GatewaySettings
from speechnexus_node_controller.environment import PLANES as CONTROLLER_PLANES
from speechnexus_node_controller.settings import Settings as ControllerSettings

BACKEND_ENV = {
    "SPEECHNEXUS_DATABASE_URL": "postgresql://api:secret@127.0.0.1:5432/speechnexus",
    "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:9000",
    "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": "http://127.0.0.1:9000/minio/health/live",
    "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
}
WORKBENCH_ENV = {
    "SPEECHNEXUS_WORKBENCH_DATABASE_URL": "postgresql://wb:secret@127.0.0.1:5432/workbench",
    "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": "workbench",
    "SPEECHNEXUS_WORKBENCH_WORKSPACE_ID": "workspace-1",
    "SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID": "deployment-1",
    "SPEECHNEXUS_WORKBENCH_CURSOR_KEY": "c" * 32,
}
GATEWAY_ENV = {
    "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL": "postgresql://gw:secret@127.0.0.1:5432/speechnexus",
}
CONTROLLER_ENV = {
    "SPEECHNEXUS_CONTROLLER_COMPONENT_TOKEN": "c" * 16,
    "SPEECHNEXUS_CONTROLLER_APPROVED_TEMPLATES_FILE": "/etc/speechnexus/templates.json",
    "SPEECHNEXUS_CONTROLLER_INTENT_STORE_PATH": "/var/lib/speechnexus/intents",
}

SERVICES: dict[str, tuple[type, tuple[Any, ...], dict[str, str], str]] = {
    "backend": (Settings, BACKEND_PLANES, BACKEND_ENV, "the speech backend process"),
    "workbench": (WorkbenchSettings, WORKBENCH_PLANES, WORKBENCH_ENV, "the workbench service"),
    "gateway": (GatewaySettings, GATEWAY_PLANES, GATEWAY_ENV, "the execution gateway service"),
    "controller": (
        ControllerSettings,
        CONTROLLER_PLANES,
        CONTROLLER_ENV,
        "the node controller service",
    ),
}

# A misspelling of a key each service really declares, with the declaration the
# message has to suggest.
TYPOS = {
    "backend": ("SPEECHNEXUS_DATABASE_URLL", "SPEECHNEXUS_DATABASE_URL"),
    "workbench": ("SPEECHNEXUS_WORKBENCH_DATABASE_URLL", "SPEECHNEXUS_WORKBENCH_DATABASE_URL"),
    "gateway": (
        "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URLL",
        "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL",
    ),
    "controller": (
        "SPEECHNEXUS_CONTROLLER_COMPONENT_TOKENS",
        "SPEECHNEXUS_CONTROLLER_COMPONENT_TOKEN",
    ),
}


def _environ(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> dict[str, str]:
    """Replace ``os.environ`` with a deterministic deployment-shaped environment."""
    for name in list(__import__("os").environ):
        if name.startswith("SPEECHNEXUS_"):
            monkeypatch.delenv(name)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_a_misspelled_prefixed_key_is_refused_naming_a_candidate(
    service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_cls, _, base, _ = SERVICES[service]
    typo, expected = TYPOS[service]
    _environ(monkeypatch, {**base, typo: "configured-by-mistake"})

    with pytest.raises(ValueError) as error:
        settings_cls()

    message = str(error.value)
    assert typo in message
    assert expected in message
    assert "Fix the spelling or unset the variable" in message


def test_every_misspelling_of_a_declared_key_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    known = declared_environment_keys(Settings)
    _environ(monkeypatch, BACKEND_ENV)
    for name in sorted(known):
        for typo in (f"{name}S", f"{name[:-1]}X"):
            unknown, foreign = unknown_prefixed_variables(
                Settings, {**BACKEND_ENV, typo: "x"}, planes=BACKEND_PLANES
            )
            assert typo in unknown or typo in {raw for raw, _ in foreign}, (name, typo)


def test_an_unmatched_prefixed_key_is_refused_without_a_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environ(monkeypatch, {**BACKEND_ENV, "SPEECHNEXUS_OPERATOR_BACKUP_DIR": "/backup"})

    with pytest.raises(ValueError) as error:
        Settings()

    assert "SPEECHNEXUS_OPERATOR_BACKUP_DIR" in str(error.value)
    assert "no declared setting resembles it" in str(error.value)


@pytest.mark.parametrize(
    "name",
    ["PATH", "HOSTNAME", "HOME", "CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", "SPEECHNEXUS"],
)
def test_a_variable_without_a_service_prefix_is_never_inspected(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environ(monkeypatch, {**BACKEND_ENV, name: "unrelated"})

    assert Settings().process_role == "api"


@pytest.mark.parametrize(
    "name",
    ["SPEECHNEXUS_API_ROLE_PASSWORD", "SPEECHNEXUS_API_HOST_PORT", "SPEECHNEXUS_TEST_DATABASE_URL"],
)
def test_a_deployment_plane_variable_starts_without_a_warning(
    name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _environ(monkeypatch, {**BACKEND_ENV, name: "host-side"})
    caplog.set_level(logging.WARNING)

    assert Settings().process_role == "api"
    assert [record for record in caplog.records if name in record.getMessage()] == []


def test_a_key_owned_by_another_entrypoint_warns_and_names_the_owner(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S4: ``SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`` on the API process."""
    _environ(
        monkeypatch, {**BACKEND_ENV, "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": "postgresql://d"}
    )
    caplog.set_level(logging.WARNING)

    assert Settings().process_role == "api"

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL" in message
        and "the job-dispatch worker entrypoint" in message
        for message in messages
    )


def test_a_key_owned_by_another_service_warns_and_names_the_owner(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _environ(
        monkeypatch, {**BACKEND_ENV, "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL": "postgresql://g"}
    )
    caplog.set_level(logging.WARNING)

    assert Settings().process_role == "api"

    assert any("the execution gateway service" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_a_blank_value_is_a_known_key_and_never_the_prefix_refusal(
    service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KEY=`` is "not configured" (#660): the prefix rule must not refuse it.

    A blank value never reaches the refusal branch, because the name is declared.
    Whatever the assembled-state validators then decide is their own contract; the
    assertion here is that this card added no new "blank counts as configured"
    judgement. The backend object-storage credential keys still refuse a blank
    placeholder through ``min_length``; that predates this card and is recorded in
    the operations document.
    """
    settings_cls, planes, base, entrypoint = SERVICES[service]
    blanked = next(iter(base))
    _environ(monkeypatch, {**base, blanked: ""})
    try:
        settings_cls()
    except ValueError as error:
        assert "match no declared setting" not in str(error)
        assert entrypoint not in str(error)
    else:
        unknown, foreign = unknown_prefixed_variables(
            settings_cls, {**base, blanked: ""}, planes=planes
        )
        assert unknown == {} and foreign == ()


def test_the_scan_is_limited_to_the_declared_prefix() -> None:
    assert environment_prefixes(Settings) == ("SPEECHNEXUS_",)
    assert environment_prefixes(WorkbenchSettings) == ("SPEECHNEXUS_WORKBENCH_",)
    assert environment_prefixes(GatewaySettings) == ("SPEECHNEXUS_GATEWAY_",)
    assert environment_prefixes(ControllerSettings) == ("SPEECHNEXUS_CONTROLLER_",)


def test_candidates_are_ranked_and_bounded() -> None:
    known = declared_environment_keys(Settings)

    assert close_candidates("SPEECHNEXUS_DATABASE_URLL", known)[0] == "SPEECHNEXUS_DATABASE_URL"
    assert len(close_candidates("SPEECHNEXUS_DATABASE_URLL", known)) <= 3
    assert close_candidates("SPEECHNEXUS_DATABASE_URLL", known, limit=2) == (
        "SPEECHNEXUS_DATABASE_URL",
        "SPEECHNEXUS_DATABASE_SCHEMA",
    )


def test_names_are_matched_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    _environ(monkeypatch, {**BACKEND_ENV, "speechnexus_database_urll": "typo"})

    with pytest.raises(ValueError, match="speechnexus_database_urll"):
        Settings()


def test_the_refusal_never_renders_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _environ(monkeypatch, {**BACKEND_ENV, "SPEECHNEXUS_DATABASE_URLL": "postgresql://leaked"})

    with pytest.raises(ValueError) as error:
        Settings()

    assert "leaked" not in str(error.value)


def test_the_engine_accepts_an_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit mapping keeps the check testable without touching the process."""
    _environ(monkeypatch, {**BACKEND_ENV})

    refuse_unrecognised_prefixed_environment(
        Settings, "the speech backend", BACKEND_ENV, planes=BACKEND_PLANES
    )
    with pytest.raises(ValueError, match="SPEECHNEXUS_DATABASE_URLL"):
        refuse_unrecognised_prefixed_environment(
            Settings,
            "the speech backend",
            {**BACKEND_ENV, "SPEECHNEXUS_DATABASE_URLL": "typo"},
            planes=BACKEND_PLANES,
        )


def test_a_blank_value_of_a_normalised_key_stays_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #660 rework's "blank means unset" list still decides the value."""
    _environ(
        monkeypatch,
        {
            **BACKEND_ENV,
            "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": "",
            "SPEECHNEXUS_ASSET_INSPECTION_TOKEN": "",
        },
    )

    settings = Settings()

    assert settings.job_writer_database_url is None
    assert settings.asset_inspection_token is None
