"""The workbench startup gate must name what refused and never echo the DSN.

``RuntimeStartupError`` used to be raised bare, so an authentication refusal, an
unreachable server and a wrong grant face all produced one sentence. The gate now
names its stage and appends the driver's own diagnosis through
``_database_error_detail``; these cases pin the two halves of that contract that a
unit test can reach without a database: the server's words survive, and the
address, the DSN and every conninfo entry do not.
"""

from __future__ import annotations

from typing import cast

import psycopg
from psycopg import errors

from speechnexus_backend.workbench.runtime import (
    RuntimeStartupError,
    _database_error_detail,
)


class _Diagnostic:
    """The ``psycopg`` diagnostic shape a server error carries (primary only)."""

    message_primary = "permission denied for table keyed_locks"


class _Refused(psycopg.Error):
    """A server error: the diagnostic shape, without a live connection."""

    # ``Diagnostic`` exposes read-only fields and takes no constructor arguments,
    # so the shape the helper reads is cast in place of a live server error.
    diag = cast(errors.Diagnostic, _Diagnostic())


def test_a_connection_refusal_keeps_the_diagnosis_and_drops_the_server_target() -> None:
    error = psycopg.OperationalError(
        'connection failed: connection to server at "10.1.2.3", port 6543 failed: '
        'FATAL:  password authentication failed for user "sn_workbench_runtime"'
    )

    detail = _database_error_detail(error)

    # The words an operator acts on survive, including the identity that tried.
    assert "password authentication failed for user" in detail
    assert "OperationalError" in detail
    # Where we dialled does not.
    assert "10.1.2.3" not in detail
    assert "6543" not in detail


def test_a_socket_refusal_drops_the_socket_path_too() -> None:
    error = psycopg.OperationalError(
        'connection failed: connection to server on socket "/tmp/.s.PGSQL.55441" failed: '
        'FATAL:  password authentication failed for user "sn_workbench_runtime"'
    )

    detail = _database_error_detail(error)

    assert "password authentication failed for user" in detail
    assert "/tmp/.s.PGSQL.55441" not in detail


def test_a_dsn_quoted_in_the_driver_message_is_replaced() -> None:
    error = psycopg.OperationalError(
        "could not parse connection string: tried postgresql://sn_rt:secret@db:5432/x"
    )

    detail = _database_error_detail(error)

    assert "<dsn>" in detail
    assert "postgresql://" not in detail
    assert "secret" not in detail


def test_conninfo_entries_are_redacted_by_keyword() -> None:
    error = psycopg.OperationalError(
        'connection failed: host=10.1.2.3 port=6543 user=sn_rt password=secret dbname="x"'
    )

    detail = _database_error_detail(error)

    assert "10.1.2.3" not in detail
    assert "6543" not in detail
    assert "secret" not in detail
    assert detail.count("<redacted>") == 5


def test_a_server_error_reports_its_primary_message_not_the_statement_text() -> None:
    # ``str()`` of a psycopg error can carry statement text and context, so the
    # primary diagnostic is preferred whenever the driver has one.
    assert _database_error_detail(_Refused("statement text must not escape")) == (
        "_Refused: permission denied for table keyed_locks"
    )


def test_the_fixed_sentence_stays_a_prefix_and_the_named_check_follows() -> None:
    message = str(
        RuntimeStartupError(
            "startup check column UPDATE surface refused: missing=['keyed_locks.version']"
        )
    )

    assert message.startswith("Workbench runtime database is unavailable or unsafe.")
    assert "column UPDATE surface" in message
    assert "keyed_locks.version" in message
