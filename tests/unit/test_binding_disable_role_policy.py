"""Unavailable authority evidence must never authorize worker startup."""

from typing import cast
from unittest.mock import Mock

import psycopg
import pytest

from speechnexus_backend.binding_disable_worker import _verify_no_role_switch
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection


@pytest.mark.parametrize("version", [None, {"version": 150000}])
def test_unknown_or_unsupported_server_version_refuses_authority(
    version: dict[str, int] | None,
) -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = version
    with pytest.raises(ValueError, match="PostgreSQL 16"):
        _verify_no_role_switch(cast(SpeechConnection, connection), "Settlement worker")


@pytest.mark.parametrize(
    "authority",
    [
        None,
        {"can_switch": None, "privileged": False, "can_admin": False, "predefined": False},
        {"can_switch": False, "privileged": False, "can_admin": False},
        {"can_switch": False, "privileged": False, "can_admin": False, "predefined": None},
        {"can_switch": False, "privileged": False, "can_admin": False, "predefined": True},
        {"can_switch": False, "privileged": False, "can_admin": False, "predefined": 0},
    ],
)
def test_missing_authority_is_not_accepted(authority: dict[str, object] | None) -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.side_effect = [{"version": 160015}, authority]
    with pytest.raises(ValueError, match="restricted identity"):
        _verify_no_role_switch(cast(SpeechConnection, connection), "Settlement worker")


def test_only_explicitly_restricted_authority_is_accepted() -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.side_effect = [
        {"version": 160015},
        {"can_switch": False, "privileged": False, "can_admin": False, "predefined": False},
    ]
    _verify_no_role_switch(cast(SpeechConnection, connection), "Settlement worker")


@pytest.mark.parametrize(
    "failure",
    [psycopg.OperationalError, psycopg.errors.QueryCanceled, psycopg.errors.InsufficientPrivilege],
)
@pytest.mark.parametrize("fail_at", [0, 1], ids=["version", "roles"])
def test_database_failure_propagates_without_authorizing(
    failure: type[psycopg.Error], fail_at: int
) -> None:
    connection = Mock()
    version_cursor = Mock()
    version_cursor.fetchone.return_value = {"version": 160015}
    connection.execute.side_effect = ([version_cursor] if fail_at else []) + [failure()]
    with pytest.raises(failure):
        _verify_no_role_switch(cast(SpeechConnection, connection), "Settlement worker")
