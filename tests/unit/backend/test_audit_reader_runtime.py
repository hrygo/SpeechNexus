"""Startup authority gate for the read-only audit identity.

The audit read path must run on a login that can SELECT the lifecycle event table
and nothing else. These cases drive the verification against a recording
connection so every rejection branch is proven without a database, plus the
runtime lifecycle (open twice, failure cleanup, diagnostic redaction).
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from pydantic import SecretStr

from speechnexus_backend import audit_reader_runtime
from speechnexus_backend.audit_reader_runtime import AuditReaderRuntime
from speechnexus_backend.settings import Settings
from tests.unit.backend.test_health import _settings

URL = "postgresql://audit_reader@127.0.0.1/unused"


class Row:
    def __init__(self, **values: Any) -> None:
        self._values = values

    def __getitem__(self, key: str) -> Any:
        return self._values[key]


class Privileges:
    """A connection that answers each startup probe with a declared grant set."""

    def __init__(
        self,
        *,
        usage: bool = True,
        create: bool = False,
        foreign: str | None = None,
        select: bool = True,
        table: dict[str, bool] | None = None,
        column: dict[str, bool] | None = None,
    ) -> None:
        self._usage, self._create, self._foreign = usage, create, foreign
        self._select, self._table, self._column = select, table or {}, column or {}
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> Any:
        self.calls.append((sql, params))
        if "has_schema_privilege" in sql:
            return Result(Row(held=self._usage if "USAGE" in sql else self._create))
        if "FROM pg_catalog.pg_class" in sql:
            return Result(Row(name=self._foreign) if self._foreign is not None else None)
        if not params:
            return Result(None)
        privilege = str(params[-1])
        if "has_any_column_privilege" in sql:
            return Result(Row(held=self._column.get(privilege, False)))
        if "'SELECT'" in sql:
            return Result(Row(held=self._select))
        return Result(Row(held=self._table.get(privilege, False)))


class Result:
    def __init__(self, row: Row | None) -> None:
        self._row = row

    def fetchone(self) -> Row | None:
        return self._row


class Pool:
    """A pool stand-in: opening either succeeds or raises the given error."""

    def __init__(self, connection: Any = None, *, error: BaseException | None = None) -> None:
        self._connection, self._error = connection, error
        self.opened = self.closed = 0

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def connection(self, *, timeout: float | None = None) -> Any:
        if self._error is not None:
            raise self._error
        return Context(self._connection)


class Context:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __enter__(self) -> Any:
        return self._connection

    def __exit__(self, *exc: object) -> None:
        return None


def settings(**overrides: object) -> Settings:
    values: dict[str, Any] = _settings().model_dump()
    values.update(
        {
            "process_role": "api",
            "database_schema": "speech",
            "audit_reader_database_url": SecretStr(URL),
        }
    )
    values.update(overrides)
    return Settings(**values)


def runtime(**overrides: object) -> AuditReaderRuntime:
    return AuditReaderRuntime(settings(**overrides))


def test_configuration_is_required_and_only_for_the_api_process() -> None:
    with pytest.raises(ValueError, match="explicit API audit reader identity"):
        AuditReaderRuntime(settings(audit_reader_database_url=None))
    with pytest.raises(ValueError, match="explicit API audit reader identity"):
        AuditReaderRuntime(settings(process_role="job-worker"))


def test_a_select_only_login_passes_verification() -> None:
    connection = Privileges()
    runtime()._verify_read_only(cast(Any, connection))
    # One probe per declared authority, in the documented order.
    assert len(connection.calls) == 13
    assert all("pg_catalog.pg_class" in sql or "has_" in sql for sql, _ in connection.calls)


@pytest.mark.parametrize(
    ("grants", "message"),
    [
        ({"usage": False}, "lacks USAGE on schema speech"),
        ({"create": True}, "must not hold CREATE on schema speech"),
        ({"foreign": "speech_publications"}, "must not hold privileges on undeclared table"),
        (
            {"table": {"INSERT": True}},
            "must not hold INSERT on speech_publication_lifecycle_events",
        ),
        ({"table": {"TRUNCATE": True}}, "must not hold TRUNCATE"),
        ({"column": {"UPDATE": True}}, "must not hold column UPDATE"),
        ({"select": False}, "lacks SELECT on speech_publication_lifecycle_events"),
    ],
)
def test_each_unexpected_authority_is_rejected(grants: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        runtime()._verify_read_only(cast(Any, Privileges(**grants)))


def test_open_wires_the_service_only_after_verification(monkeypatch: Any) -> None:
    pool = Pool(Privileges())
    monkeypatch.setattr(audit_reader_runtime, "speech_pool", lambda *args, **kwargs: pool)
    subject = runtime()
    subject._verify_read_only = lambda connection: None  # type: ignore[method-assign]
    subject.open()
    assert pool.opened == 1
    assert subject.service is not None
    with pytest.raises(ValueError, match="already open"):
        subject.open()
    subject.close()
    assert cast(Any, subject).service is None
    assert pool.closed == 1
    # Closing is idempotent; a closed runtime holds no service.
    subject.close()
    assert pool.closed == 1


@pytest.mark.parametrize("error", [TimeoutError("pool timeout"), RuntimeError("closed")])
def test_open_failure_closes_the_pool_and_names_the_remedy(
    monkeypatch: Any, error: Exception
) -> None:
    pool = Pool(error=error)
    monkeypatch.setattr(audit_reader_runtime, "speech_pool", lambda *args, **kwargs: pool)
    subject = runtime()
    with pytest.raises(ValueError, match="Audit reader startup rejected"):
        subject.open()
    assert subject.service is None
    assert pool.closed == 1


def test_a_non_exception_base_exception_is_re_raised(monkeypatch: Any) -> None:
    pool = Pool(error=KeyboardInterrupt())
    monkeypatch.setattr(audit_reader_runtime, "speech_pool", lambda *args, **kwargs: pool)
    with pytest.raises(KeyboardInterrupt):
        runtime().open()


def test_pool_diagnostics_redact_the_configured_name() -> None:
    diagnostic = audit_reader_runtime._PoolDiagnostics("speech-audit-read-abc")
    relevant = logging.LogRecord(
        "psycopg.pool", logging.ERROR, __file__, 1, "error connecting to %s", ("db",), None
    )
    relevant.msg, relevant.args = "pool speech-audit-read-abc failed: secret detail", ()
    relevant.exc_info = (ValueError, ValueError("dsn"), None)
    redacted: Any = relevant
    assert diagnostic.filter(redacted) is True
    assert redacted.getMessage() == "Audit database connection unavailable"
    # The filter drops the recorded traceback so the DSN cannot leak through it.
    assert redacted.exc_info is None
    unrelated = logging.LogRecord(
        "psycopg.pool", logging.ERROR, __file__, 1, "other pool failed", (), None
    )
    assert diagnostic.filter(unrelated) is True
    assert unrelated.getMessage() == "other pool failed"


def test_a_runtime_without_a_pool_closes_cleanly() -> None:
    subject = runtime()
    subject.close()
    assert subject.service is None


def test_a_pool_that_cannot_be_created_is_reported_without_closing_anything(
    monkeypatch: Any,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cannot build pool")

    monkeypatch.setattr(audit_reader_runtime, "speech_pool", fail)
    subject = runtime()
    with pytest.raises(ValueError, match="Audit reader startup rejected"):
        subject.open()
    assert cast(Any, subject)._pool is None
    assert subject.service is None
