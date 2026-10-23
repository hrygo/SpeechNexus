"""Submit-ledger SQL mapping, lock order and stored-column drift."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_backend.speech.infrastructure.coordination_grants import (
    GATEWAY_UPDATES,
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.coordination_schema import COORDINATION_DDL
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    InvalidExecutionRequestError,
    StaleFencingTokenError,
    StaleSubmissionVersionError,
    SubmissionIdentity,
    SubmissionIntent,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.submit_sql import (
    SUBMIT_INTENT_INSERT_COLUMNS,
    SUBMIT_INTENT_UPDATE_COLUMNS,
    create_submit_in_transaction,
    from_unix_micros,
    read_submit_intent,
    save_submit_intent,
    submit_intent_update_values,
    submit_intent_values,
    to_unix_micros,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
COMMAND: JsonValue = {"schema_version": "1", "task": "tts.synthesize"}
IDENTITY = SubmissionIdentity(
    submit_intent_id="intent-1",
    lease_id="lease-1",
    operation_id="op-1",
    attempt_id="attempt-1",
    stage_id="stage-1",
    workspace_id="ws-1",
    pool_id="tts-realtime",
    host_node_id="node-1",
    gateway_id="gateway-1",
    instance_id="instance-1",
    instance_generation=1,
    binding_revision_id="binding-rev-1",
    policy_revision_id="policy-rev-1",
    voice_revision_id=None,
    fencing_token=1,
    command_digest=command_digest(COMMAND),
    deadline_at=NOW + timedelta(minutes=5),
    max_output_audio_ms=2000,
    sample_rate_hz=24000,
)
INTENT = SubmissionIntent(
    identity=IDENTITY,
    recorded_at=NOW,
    updated_at=NOW,
    request_digest=DIGEST,
    output_stream_id="tts-main",
)


def authority_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "lease_id": "lease-1",
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "stage_id": "stage-1",
        "host_node_id": "node-1",
        "instance_id": "instance-1",
        "instance_generation": 1,
        "pool_id": "tts-realtime",
        "gateway_id": "gateway-1",
        "fencing_token": 1,
        "lease_state": "reserved",
        "workspace_id": "ws-1",
        "binding_revision_id": "binding-rev-1",
        "policy_revision_id": "policy-rev-1",
    }
    row.update(overrides)
    return row


def stored_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "submit_intent_id": "intent-1",
        "lease_id": "lease-1",
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "command_digest": IDENTITY.command_digest,
        "request_digest": DIGEST,
        "voice_revision_id": None,
        "deadline_at_us": to_unix_micros(IDENTITY.deadline_at),
        "recorded_at_us": to_unix_micros(NOW),
        "updated_at_us": to_unix_micros(NOW),
        "execution_state": "executing",
        "cleanup_state": "pending",
        "engine_request_id": "engine-request-1",
        "dispatched_at_us": to_unix_micros(NOW),
        "error_code": None,
        "output_stream_id": "tts-main",
        "sample_rate_hz": 24000,
        "hard_output_ceiling_samples": 48000,
        "version": 2,
        "evidence_kind": None,
        "evidence_digest": None,
        "stage_id": "stage-1",
        "pool_id": "tts-realtime",
        "host_node_id": "node-1",
        "gateway_id": "gateway-1",
        "instance_id": "instance-1",
        "instance_generation": 1,
        "fencing_token": 1,
        "workspace_id": "ws-1",
        "binding_revision_id": "binding-rev-1",
        "policy_revision_id": "policy-rev-1",
    }
    row.update(overrides)
    return row


class StubCursor:
    """Answers by statement marker and records every call in order."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._answers = answers or {}
        self._rows: list[dict[str, Any]] = []
        self._rowcounts: dict[str, int] = {}
        self.rowcount = 1
        self.calls: list[str] = []

    def fail_rowcount(self, marker: str, rowcount: int) -> None:
        self._rowcounts[marker] = rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        self.calls.append(query)
        for marker, rows in self._answers.items():
            if marker in query:
                self._rows = rows
                self.rowcount = self._rowcounts.get(marker, 1)
                return None
        self._rows = []
        self.rowcount = self._rowcounts.get("default", 1)
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


def test_insert_and_update_columns_stay_inside_the_reviewed_authority() -> None:
    ddl = next(part for part in COORDINATION_DDL if "CREATE TABLE submit_intents" in part)
    granted = set(GATEWAY_UPDATES["submit_intents"].split())
    grants = coordination_runtime_grants(
        "speech", speech_role="speech_runtime", gateway_role="gateway_runtime"
    )

    for column in SUBMIT_INTENT_INSERT_COLUMNS:
        assert column in ddl
    assert set(SUBMIT_INTENT_UPDATE_COLUMNS) <= granted
    assert 'GRANT INSERT ON "speech"."submit_intents" TO "gateway_runtime"' in grants
    assert 'GRANT INSERT ON "speech"."output_stream_heads" TO "gateway_runtime"' in grants


def test_value_mappings_keep_the_statement_column_order() -> None:
    assert tuple(submit_intent_values(INTENT, "{}")) == SUBMIT_INTENT_INSERT_COLUMNS
    assert tuple(submit_intent_update_values(INTENT)) == SUBMIT_INTENT_UPDATE_COLUMNS


def test_micros_round_trip_and_naive_values_are_rejected() -> None:
    stamp = datetime(2026, 10, 24, 3, 4, 5, 678901, tzinfo=UTC)

    assert from_unix_micros(to_unix_micros(stamp)) == stamp
    with pytest.raises(ValueError, match="timezone aware"):
        to_unix_micros(datetime(2026, 10, 24, 3, 4, 5))
    with pytest.raises(ValueError, match="Unix epoch"):
        to_unix_micros(datetime(1969, 1, 1, tzinfo=UTC))


def test_create_submit_locks_the_pool_attempt_and_lease_in_global_order() -> None:
    cursor = StubCursor(
        {
            "FROM admission_leases": [authority_row()],
            "FROM output_stream_heads": [],
        }
    )

    create_submit_in_transaction(cursor, INTENT, "{}")

    locks = [statement.split(" WHERE")[0] for statement in cursor.calls[:3]]
    assert locks[0].startswith("UPDATE resource_pools")
    assert locks[1].startswith("UPDATE admission_attempts")
    assert locks[2].startswith("UPDATE admission_leases")
    assert cursor.calls[3].startswith("\nSELECT") or "FROM admission_leases AS l" in cursor.calls[3]
    assert any(statement.startswith("INSERT INTO submit_intents") for statement in cursor.calls)
    assert any(
        statement.startswith("INSERT INTO output_stream_heads") for statement in cursor.calls
    )


def test_create_submit_rejects_a_claim_the_lease_contradicts() -> None:
    cursor = StubCursor({"FROM admission_leases": [authority_row(fencing_token=9)]})

    with pytest.raises(StaleFencingTokenError):
        create_submit_in_transaction(cursor, INTENT, "{}")

    assert not any(statement.startswith("INSERT INTO submit_intents") for statement in cursor.calls)


def test_create_submit_rejects_an_unknown_lease_without_inserting() -> None:
    cursor = StubCursor()
    cursor.fail_rowcount("UPDATE admission_leases", 0)

    with pytest.raises(InvalidExecutionRequestError, match="unknown lease"):
        create_submit_in_transaction(cursor, INTENT, "{}")

    assert not any(statement.startswith("INSERT INTO submit_intents") for statement in cursor.calls)


def test_create_submit_reuses_a_frozen_stream_head_but_refuses_another_rate() -> None:
    same_rate = StubCursor(
        {
            "FROM admission_leases": [authority_row()],
            "FROM output_stream_heads": [{"sample_rate_hz": 24000}],
        }
    )

    create_submit_in_transaction(same_rate, INTENT, "{}")

    assert not any(
        statement.startswith("INSERT INTO output_stream_heads") for statement in same_rate.calls
    )

    conflicting = StubCursor(
        {
            "FROM admission_leases": [authority_row()],
            "FROM output_stream_heads": [{"sample_rate_hz": 48000}],
        }
    )
    with pytest.raises(InvalidExecutionRequestError, match="sample rate"):
        create_submit_in_transaction(conflicting, INTENT, "{}")


def test_a_stage_without_audio_never_creates_a_stream_head() -> None:
    silent = SubmissionIntent(
        identity=replace(IDENTITY, max_output_audio_ms=None, sample_rate_hz=None),
        recorded_at=NOW,
        updated_at=NOW,
        request_digest=DIGEST,
    )
    cursor = StubCursor({"FROM admission_leases": [authority_row()]})

    create_submit_in_transaction(cursor, silent, "{}")

    assert not any("output_stream_heads" in statement for statement in cursor.calls)


def test_read_submit_intent_rebuilds_the_durable_record() -> None:
    cursor = StubCursor({"FROM submit_intents AS s": [stored_row()]})

    intent = read_submit_intent(cursor, "intent-1")

    assert intent is not None
    assert intent.identity == IDENTITY
    assert intent.request_digest == DIGEST
    assert intent.output_stream_id == "tts-main"
    assert intent.execution_state is ExecutionState.EXECUTING
    assert intent.engine_request_id == "engine-request-1"
    assert intent.version == 2


def test_read_submit_intent_returns_nothing_for_an_unknown_identity() -> None:
    assert read_submit_intent(StubCursor(), "intent-missing") is None


@pytest.mark.parametrize(
    "broken",
    [
        {"execution_state": "reconciling"},
        {"sample_rate_hz": 8000},
        {"hard_output_ceiling_samples": 1},
    ],
)
def test_read_submit_intent_reports_rows_it_cannot_reinterpret(broken: dict[str, Any]) -> None:
    cursor = StubCursor({"FROM submit_intents AS s": [stored_row(**broken)]})

    with pytest.raises(SubmissionStoreError):
        read_submit_intent(cursor, "intent-1")


def test_save_requires_exactly_one_newer_version_and_the_read_version() -> None:
    cursor = StubCursor()
    updated = read_submit_intent(
        StubCursor({"FROM submit_intents AS s": [stored_row()]}), "intent-1"
    )
    assert updated is not None
    updated.mark_stopping(at=NOW + timedelta(seconds=1))

    save_submit_intent(cursor, updated, expected_version=2)

    assert any(statement.startswith("UPDATE submit_intents SET") for statement in cursor.calls)
    with pytest.raises(ValueError, match="one version"):
        save_submit_intent(cursor, updated, expected_version=1)


def test_save_reports_a_stale_version_instead_of_succeeding() -> None:
    cursor = StubCursor()
    cursor.fail_rowcount("default", 0)
    intent = SubmissionIntent(
        identity=IDENTITY,
        recorded_at=NOW,
        updated_at=NOW,
        request_digest=DIGEST,
        output_stream_id="tts-main",
    )
    intent.mark_dispatched("engine-request-1", at=NOW)
    intent.mark_stopping(at=NOW)

    with pytest.raises(StaleSubmissionVersionError):
        save_submit_intent(cursor, intent, expected_version=2)


def test_an_unreadable_lease_state_is_reported_instead_of_guessed() -> None:
    cursor = StubCursor({"FROM admission_leases": [authority_row(lease_state="reconciling")]})

    with pytest.raises(SubmissionStoreError):
        create_submit_in_transaction(cursor, INTENT, "{}")
