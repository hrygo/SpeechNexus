"""Read-only abort recovery returns original, bounded facts without command contents."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any, cast

import pytest
import rfc8785

from speechnexus_backend.speech.infrastructure.gateway_query_grants import gateway_query_grants
from speechnexus_gateway.domain.recovery import AbortReceipt, AbortRecovery, ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
    SubmissionStoreError,
    SubmissionStoreUnavailableError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.execution_reader import (
    PostgresExecutionReader,
    verify_execution_reader_authority,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.reader_authority import verify_execution_reader_target
from tests.fake.execution_command import command_example
from tests.unit.gateway.test_submit_execution import NOW, identity_for, intent_for


def _request() -> SubmitRequest:
    command = command_example(1)
    return SubmitRequest(identity_for(command), command)


def _row() -> dict[str, Any]:
    identity = _request().identity
    micros = int(NOW.timestamp() * 1_000_000)
    return {
        "submit_intent_id": identity.submit_intent_id,
        "lease_id": identity.lease_id,
        "operation_id": identity.operation_id,
        "attempt_id": identity.attempt_id,
        "command_digest": identity.command_digest,
        "request_digest": _request().request_digest,
        "voice_revision_id": identity.voice_revision_id,
        "deadline_at_us": micros + 300_000_000,
        "recorded_at_us": micros,
        "updated_at_us": micros,
        "execution_state": "submitting",
        "cleanup_state": "pending",
        "engine_request_id": None,
        "dispatched_at_us": None,
        "error_code": None,
        "output_stream_id": None,
        "sample_rate_hz": None,
        "hard_output_ceiling_samples": None,
        "generated_pcm_samples": 0,
        "staging_output_object_key": None,
        "staging_result_document": None,
        "version": 1,
        "terminated_at_us": None,
        "has_output_intervals": False,
        "evidence_kind": None,
        "evidence_digest": None,
        "stage_id": identity.stage_id,
        "pool_id": identity.pool_id,
        "host_node_id": identity.host_node_id,
        "gateway_id": identity.gateway_id,
        "instance_id": identity.instance_id,
        "instance_generation": identity.instance_generation,
        "fencing_token": identity.fencing_token,
        "workspace_id": identity.workspace_id,
        "binding_revision_id": identity.binding_revision_id,
        "policy_revision_id": identity.policy_revision_id,
    }


def _receipt() -> dict[str, Any]:
    return {
        "submit_intent_id": "intent-1",
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "fencing_token": 7,
        "reason": "client_cancelled",
        "wait_timeout_ms": 3000,
        "received_at_us": int(NOW.timestamp() * 1_000_000),
        "dispatched_at_us": None,
    }


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.connection.statements.append((query, parameters))
        if "pg_control_system" in query:
            self.rows = [self.connection.target]
        elif "AS document_present" in query and "JOIN admission_leases" in query:
            self.rows = [self.connection.deletion]
        elif "AS bounded_document" in query:
            assert self.connection.intent is not None
            document = self.connection.document
            self.rows = [
                {
                    **self.connection.deletion,
                    "version": self.connection.intent["version"],
                    "request_digest": self.connection.intent["request_digest"],
                    "command_digest": self.connection.intent["command_digest"],
                    "document_bytes": None if document is None else len(document.encode()),
                    "bounded_document": document,
                }
            ]
        elif "FROM submit_intents AS s" in query:
            self.rows = [] if self.connection.intent is None else [self.connection.intent]
        elif "FROM gateway_abort_intents" in query:
            self.rows = [] if self.connection.receipt is None else [self.connection.receipt]
        elif "set_config" not in query:
            raise AssertionError("unexpected query")

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _Connection:
    def __init__(self, intent: dict[str, Any] | None, receipt: dict[str, Any] | None) -> None:
        self.intent, self.receipt = intent, receipt
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.document: str | None = _request().request_document
        self.target = _target_row()
        self.deletion: dict[str, Any] = {
            "document_present": True,
            "lease_state": "reserved",
            "command_redacted_at_us": None,
            "command_deletion_job_id": None,
            "command_deletion_evidence_digest": None,
        }

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.statements.append((query, parameters))

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_value = connection

    def connection(self, *, timeout: float) -> _Connection:
        assert 0 < timeout <= 3
        return self.connection_value


def _reader(
    intent: dict[str, Any] | None, receipt: dict[str, Any] | None
) -> tuple[PostgresExecutionReader, _Connection]:
    connection = _Connection(intent, receipt)
    return PostgresExecutionReader(cast(GatewayPool, _Pool(connection))), connection


def test_legacy_snapshot_omits_recovery_and_reader_absence_is_observed() -> None:
    assert "abort_recovery" not in ExecutionSnapshot(intent_for()).status()
    reader, connection = _reader(_row(), None)
    snapshot = reader.query_outcome("intent-1")
    assert snapshot is not None
    assert snapshot.status()["abort_recovery"] == {
        "execution_request_digest": _row()["request_digest"],
        "document_state": "verified",
        "receipt": None,
    }
    sql = " ".join(statement for statement, _ in connection.statements).upper()
    assert "REPEATABLE READ" in sql and "READ ONLY" in sql
    assert "FOR UPDATE" not in sql
    assert all(word not in sql for word in ("INSERT INTO", "UPDATE ", "DELETE FROM"))


@pytest.mark.parametrize("claimed", [False, True])
def test_receipt_hashes_six_field_original_and_keeps_private_values_out(claimed: bool) -> None:
    stored = _receipt()
    if claimed:
        stored["dispatched_at_us"] = stored["received_at_us"] + 1_000_000
    reader, _ = _reader(_row(), stored)
    snapshot = reader.query_outcome("intent-1")
    assert snapshot is not None
    wire = snapshot.status()["abort_recovery"]
    digest_input = {
        name: stored[name]
        for name in (
            "submit_intent_id",
            "operation_id",
            "attempt_id",
            "fencing_token",
            "reason",
            "wait_timeout_ms",
        )
    }
    expected = "sha256:" + hashlib.sha256(rfc8785.dumps(digest_input)).hexdigest()
    assert wire == {
        "execution_request_digest": _row()["request_digest"],
        "document_state": "verified",
        "receipt": {
            "abort_request_digest": expected,
            "received_at": "2026-10-24T00:00:00.000000Z",
            "claim_state": "claimed" if claimed else "received",
            "claimed_at": "2026-10-24T00:00:01.000000Z" if claimed else None,
        },
    }
    assert "client_cancelled" not in repr(wire)


def test_missing_original_is_not_misreported_as_a_receipt() -> None:
    reader, connection = _reader(None, _receipt())
    assert reader.query_outcome("intent-1") is None
    assert not any("FROM gateway_abort_intents" in sql for sql, _ in connection.statements)


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": "op-other"},
        {"attempt_id": "attempt-other"},
        {"fencing_token": 8},
        {"reason": "private-invalid"},
        {"dispatched_at_us": 1},
        {"received_at_us": -1},
    ],
)
def test_inconsistent_receipt_never_becomes_an_observation(changes: dict[str, object]) -> None:
    reader, _ = _reader(_row(), _receipt() | changes)
    with pytest.raises(SubmissionStoreError) as error:
        reader.query_outcome("intent-1")
    assert "private-invalid" not in str(error.value)


def test_typed_receipt_rejects_state_time_contradictions() -> None:
    digest = "sha256:" + "a" * 64
    with pytest.raises(ValueError):
        AbortReceipt(digest, NOW, "received", NOW)
    with pytest.raises(ValueError):
        AbortReceipt(digest, NOW, "claimed", None)
    with pytest.raises(ValueError):
        AbortReceipt(digest, NOW, "claimed", NOW - timedelta(seconds=1))
    with pytest.raises(ValueError):
        AbortRecovery("invalid", "verified", None)


def test_reader_uses_only_column_select_and_no_writes() -> None:
    grants = gateway_query_grants("speech", "reader")
    combined = " ".join(grants).lower()
    assert "request_document" in combined
    assert "grant select (" in combined and "grant usage on schema" in combined
    assert all(
        word not in combined
        for word in (
            "grant insert",
            "grant update",
            "grant delete",
            "grant create",
            "all privileges",
        )
    )


def test_legitimately_redacted_terminal_execution_preserves_real_receipt() -> None:
    row = _row() | {
        "execution_state": "failed",
        "cleanup_state": "not_required",
        "error_code": "engine_transport_unknown",
        "version": 2,
    }
    reader, connection = _reader(row, _receipt())
    connection.document = None
    connection.deletion = {
        "document_present": False,
        "lease_state": "released",
        "command_redacted_at_us": int(NOW.timestamp() * 1_000_000),
        "command_deletion_job_id": "deletion-test",
        "command_deletion_evidence_digest": "sha256:" + "b" * 64,
    }
    snapshot = reader.query_outcome("intent-1")
    assert snapshot is not None
    recovery = cast(dict[str, Any], snapshot.status()["abort_recovery"])
    assert recovery["document_state"] == "redacted"
    assert recovery["execution_request_digest"] == row["request_digest"]
    assert recovery["receipt"] is not None
    assert "deletion-test" not in repr(recovery)
    assert not any("AS bounded_document" in sql for sql, _ in connection.statements)


@pytest.mark.parametrize(
    "changes",
    [
        {"command_deletion_job_id": None},
        {"command_deletion_evidence_digest": "wrong"},
        {"command_redacted_at_us": 1},
        {"lease_state": "reserved"},
    ],
)
def test_missing_original_with_illegal_tombstone_fails_closed(changes: dict[str, object]) -> None:
    row = _row() | {
        "execution_state": "failed",
        "cleanup_state": "not_required",
        "error_code": "engine_transport_unknown",
        "version": 2,
    }
    reader, connection = _reader(row, None)
    connection.document = None
    connection.deletion = {
        "document_present": False,
        "lease_state": "released",
        "command_redacted_at_us": int(NOW.timestamp() * 1_000_000),
        "command_deletion_job_id": "deletion-test",
        "command_deletion_evidence_digest": "sha256:" + "b" * 64,
    } | changes
    with pytest.raises(SubmissionStoreError):
        reader.query_outcome("intent-1")


def test_corrupted_original_fails_without_leaking_command() -> None:
    reader, connection = _reader(_row(), None)
    connection.document = '{"private_invalid":true}'
    with pytest.raises(SubmissionStoreError) as error:
        reader.query_outcome("intent-1")
    assert "private_invalid" not in str(error.value)


def test_pool_failure_is_unavailable_and_does_not_leak_connection_details() -> None:
    from psycopg_pool import PoolTimeout

    class Unavailable:
        def connection(self, *, timeout: float) -> Any:
            raise PoolTimeout("secret connection details")

    with pytest.raises(SubmissionStoreUnavailableError) as error:
        PostgresExecutionReader(cast(GatewayPool, Unavailable())).query_outcome("intent-1")
    assert "secret" not in str(error.value)


class _AuthorityResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _AuthorityConnection(_Connection):
    def __init__(self) -> None:
        super().__init__(None, None)
        self.roles = ["reader"]
        self.unsafe = False
        self.extra_read = False
        self.writable = False
        self.row_security = False
        self.missing = False
        self.version = 160000
        self.effective = "reader"
        self.search_path = "speech,pg_catalog,pg_temp"

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Any:
        self.statements.append((query, parameters))
        if "SET TRANSACTION" in query or "set_config" in query:
            return _AuthorityResult([])
        if "server_version_num" in query:
            return _AuthorityResult(
                [
                    {
                        "version": self.version,
                        "login": "reader",
                        "effective": self.effective,
                        "search_path": self.search_path,
                    }
                ]
            )
        if "WITH RECURSIVE reachable" in query:
            return _AuthorityResult([{"rolname": role} for role in self.roles])
        if "AS forbidden" in query:
            return _AuthorityResult([{"forbidden": self.unsafe}])
        if "AS can_read" in query:
            rows = []
            for grant in gateway_query_grants("speech", "reader")[1:]:
                match = re.search(r'SELECT \((.*?)\) ON "speech"\."(.*?)"', grant)
                assert match is not None
                for column in match.group(1).split(", "):
                    rows.append(
                        {
                            "schema_name": "speech",
                            "relname": match.group(2),
                            "attname": column,
                            "can_read": True,
                            "can_insert": False,
                            "can_change": self.writable,
                            "can_delegate": False,
                            "destructive": False,
                            "explicit_write": False,
                            "trusted_metadata": False,
                            "native_settings": False,
                            "row_security": self.row_security,
                        }
                    )
            if self.extra_read:
                rows.append(
                    {
                        "schema_name": "speech",
                        "relname": "submit_intents",
                        "attname": "other",
                        "can_read": True,
                        "can_insert": False,
                        "can_change": False,
                        "can_delegate": False,
                        "destructive": False,
                        "explicit_write": False,
                        "trusted_metadata": False,
                        "native_settings": False,
                        "row_security": False,
                    }
                )
            if self.missing:
                rows.pop()
            return _AuthorityResult(rows)
        if "AS other_capability" in query:
            return _AuthorityResult([{"other_capability": False}])
        if "AS usable" in query:
            return _AuthorityResult([{"usable": True}])
        raise AssertionError("unexpected authority query")


def test_reader_authority_requires_exact_columns_and_trusted_session() -> None:
    connection = _AuthorityConnection()
    pool = cast(GatewayPool, _Pool(connection))
    assert verify_execution_reader_authority(pool, "speech")
    for attr, value in (
        ("unsafe", True),
        ("extra_read", True),
        ("writable", True),
        ("row_security", True),
        ("missing", True),
        ("version", 150000),
        ("effective", "other"),
        ("search_path", "pg_temp,speech"),
        ("roles", ["reader", "pg_write_all_data"]),
    ):
        changed = _AuthorityConnection()
        setattr(changed, attr, value)
        assert not verify_execution_reader_authority(cast(GatewayPool, _Pool(changed)), "speech")


def _target_row() -> dict[str, Any]:
    return {
        "database_name": "synthetic_database",
        "database_oid": 1234,
        "schema_name": "speech",
        "schema_oid": 5678,
        "search_path": "speech,pg_catalog,pg_temp",
        "system_identifier": "01234567890123456789",
        "postmaster_started_at": NOW,
        "server_address": "127.0.0.1/32",
        "server_port": 5432,
        "in_recovery": False,
        "singleton": True,
        "installation_id": "760c5f7e-bbb7-4322-86c2-91c96d4e4da6",
    }


class _TargetConnection(_Connection):
    def __init__(self, **changes: object) -> None:
        super().__init__(None, None)
        self.target.update(changes)

    def execute(self, query: Any, parameters: tuple[object, ...] = ()) -> Any:
        self.statements.append((str(query), parameters))
        return _AuthorityResult([self.target])


def test_reader_and_writer_must_target_same_primary_installation() -> None:
    writer = _TargetConnection()

    def pool(connection: _TargetConnection) -> GatewayPool:
        return cast(GatewayPool, _Pool(connection))

    expected = verify_execution_reader_target(pool(writer), pool(_TargetConnection()), "speech")
    assert expected is not None
    for changes in (
        {"database_name": "other"},
        {"database_oid": 5555},
        {"schema_name": "other"},
        {"search_path": "pg_temp,speech"},
        {"schema_oid": 8888},
        {"server_address": "127.0.0.2/32"},
        {"server_port": 5433},
        {"system_identifier": "999"},
        {"postmaster_started_at": NOW + timedelta(seconds=1)},
        {"in_recovery": True},
        {"singleton": False},
        {"installation_id": None},
        {"installation_id": "a842c243-3c14-4edc-9cf3-9eb051345169"},
    ):
        assert not verify_execution_reader_target(
            pool(writer), pool(_TargetConnection(**changes)), "speech"
        )
    assert not verify_execution_reader_target(
        pool(_TargetConnection(in_recovery=True)), pool(_TargetConnection()), "speech"
    )


def test_runtime_reader_checks_pinned_target_in_its_own_consistent_snapshot() -> None:
    def pool(connection: _Connection) -> GatewayPool:
        return cast(GatewayPool, _Pool(connection))

    target = verify_execution_reader_target(
        pool(_TargetConnection()), pool(_TargetConnection()), "speech"
    )
    assert target is not None
    connection = _Connection(_row(), None)
    reader = PostgresExecutionReader(pool(connection), expected_target=target)
    assert reader.query_outcome("intent-1") is not None
    connection.target["postmaster_started_at"] = NOW + timedelta(seconds=1)
    connection.target["server_address"] = "127.0.0.2/32"
    assert reader.query_outcome("intent-1") is not None
    for key, value in (
        ("in_recovery", True),
        ("database_oid", 999),
        ("installation_id", "a842c243-3c14-4edc-9cf3-9eb051345169"),
    ):
        connection.target = _target_row() | {key: value}
        with pytest.raises(SubmissionStoreError):
            reader.query_outcome("intent-1")
