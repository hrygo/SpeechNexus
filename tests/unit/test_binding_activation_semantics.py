"""Production activation decisions over synthetic SQL state; no PG/commit evidence."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingActivationCommand,
    BindingActivationConflictError,
    BindingLifecycleConflictError,
    BindingLifecycleReceipt,
    BindingRetirementCommand,
)
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    ACTIVATE_OPERATION,
    RETIRE_OPERATION,
)
from speechnexus_backend.speech.interfaces.http.binding_lifecycle import _command
from tests.unit.test_binding_lifecycle import receipt


class Cursor:
    """Only models SQL state used here, without transactions or database locking."""

    def __init__(self) -> None:
        self.head: str | None = "2"
        self.retirements: dict[str, dict[str, Any]] = {}
        self.disable_intents: list[object] = []
        self.catalogue = 7
        self.events: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.rows, self.rowcount = [], 0
        if "pg_advisory_xact_lock" in query:
            return
        if query.startswith("SELECT * FROM speech_publication_lifecycle_events"):
            self.rows = [e for e in self.events if e["key"] == parameters[0]]
        elif "FROM speech_publications AS p" in query:
            revision = str(parameters[1])
            original = receipt(status="published", retired_at_us=None)
            self.rows = [
                {
                    "binding_id": "asr-quality",
                    "revision": revision,
                    "revision_id": "binding-rev-" + revision,
                    "definition_json": original.definition.decode(),
                    "created_at_us": original.created_at_us,
                    "published_at_us": original.published_at_us,
                    "capacity_manifest_id": original.capacity_manifest_id,
                    "qualification_report_id": original.qualification_report_id,
                    "retired_at_us": None,
                    **self.retirements.get(revision, {}),
                }
            ]
        elif query.startswith("SELECT revision, revision_id FROM speech_publication_heads"):
            self.rows = (
                []
                if self.head is None
                else [{"revision": self.head, "revision_id": "binding-rev-" + self.head}]
            )
        elif query.startswith("SELECT CAST(EXTRACT(EPOCH"):
            self.rows = [{"t": 1_700_000_000_000_003}]
        elif query.startswith("INSERT INTO speech_publication_heads"):
            self.head, self.rowcount = str(parameters[1]), 1
        elif query.startswith("INSERT INTO speech_publication_retirements"):
            self.retirements[str(parameters[1])] = {
                "retired_at_us": parameters[3],
                "mode": parameters[4],
                "reason": parameters[5],
            }
            self.catalogue += 1  # Model the retirement trigger; not PG evidence.
            self.rowcount = 1
        elif query.startswith("DELETE FROM speech_publication_heads"):
            self.head, self.rowcount = None, 1
        elif query.startswith("INSERT INTO speech_binding_disable_intents"):
            self.disable_intents.append(parameters[0])
            self.rowcount = 1
        elif query.startswith("UPDATE speech_catalog_state"):
            self.catalogue += 1
            self.rowcount = 1
        elif query.startswith("SELECT revision FROM speech_catalog_state"):
            self.rows = [{"revision": self.catalogue}]
        elif query.startswith("INSERT INTO speech_publication_lifecycle_events"):
            columns = [
                "event_id",
                "action",
                "binding_id",
                "revision",
                "revision_id",
                "result_status",
                "retired_at_us",
                "disabled_reason",
                "request_id",
                "actor_type",
                "credential_label",
                "occurred_at_us",
                "policy_revision",
                "intent_digest",
                "key",
                "change_summary_json",
            ]
            self.events.append(dict(zip(columns, parameters, strict=True)))
            self.rowcount = 1
        else:
            raise AssertionError("Unmodelled SQL: " + query)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def command(target: str, expected: str, key: str | None) -> BindingActivationCommand:
    def digest(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

    return BindingActivationCommand(
        binding_id="asr-quality",
        revision=target,
        request_id="synthetic-activation",
        actor_type="anonymous",
        credential_label=None,
        key_digest=None if key is None else digest(key),
        intent_digest=digest("activate/asr-quality/" + target + "/" + expected),
        expected_current_revision=expected,
    )


def activate(cursor: Cursor, request: BindingActivationCommand) -> BindingLifecycleReceipt:
    return PostgresBindingLifecycle(cast(Any, None))._activate(cast(Any, cursor), request)


def test_rejection_does_not_bind_key_and_committed_replay_precedes_new_cas() -> None:
    cursor = Cursor()
    rejected = command("1", "3", "original-key")
    with pytest.raises(BindingActivationConflictError):
        activate(cursor, rejected)
    assert (cursor.head, cursor.catalogue, cursor.events) == ("2", 7, [])

    accepted = command("1", "2", "original-key")
    result = activate(cursor, accepted)
    assert not result.replayed
    assert (cursor.head, cursor.catalogue, len(cursor.events)) == ("1", 8, 1)
    with pytest.raises(BindingLifecycleConflictError, match="idempotency_conflict"):
        activate(cursor, rejected)
    activate(cursor, command("2", "1", "other-key"))
    before = (cursor.head, cursor.catalogue, list(cursor.events))
    replay = activate(cursor, accepted)
    assert replay.replayed and replay.revision == result.revision == "1"
    assert (cursor.head, cursor.catalogue, cursor.events) == before


def test_original_rejected_request_is_reevaluated_without_a_stored_result() -> None:
    cursor = Cursor()
    original = command("2", "1", "rejected-key")
    with pytest.raises(BindingActivationConflictError):
        activate(cursor, original)
    activate(cursor, command("1", "2", "other-key"))
    result = activate(cursor, original)
    assert result.revision == "2" and not result.replayed
    assert (cursor.head, cursor.catalogue, len(cursor.events)) == ("2", 9, 2)


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize(
    "first,second,rejected,head,delta,events",
    [
        ("1", "1", True, "1", 1, 1),
        ("2", "2", False, "2", 0, 2),
        ("2", "1", False, "1", 1, 2),
        ("1", "2", True, "1", 1, 1),
    ],
    ids=["change-change", "noop-noop", "noop-change", "change-noop"],
)
def test_serial_orders_for_new_commands(
    keyed: bool, first: str, second: str, rejected: bool, head: str, delta: int, events: int
) -> None:
    cursor = Cursor()
    activate(cursor, command(first, "2", "first-key" if keyed else None))
    next_command = command(second, "2", "second-key" if keyed else None)
    if rejected:
        with pytest.raises(BindingActivationConflictError):
            activate(cursor, next_command)
    else:
        assert not activate(cursor, next_command).replayed
    assert (cursor.head, cursor.catalogue, len(cursor.events)) == (head, 7 + delta, events)


def test_successful_noop_also_binds_key() -> None:
    cursor = Cursor()
    original = command("2", "2", "noop-key")
    activate(cursor, original)
    assert (cursor.head, cursor.catalogue, len(cursor.events)) == ("2", 7, 1)
    with pytest.raises(BindingLifecycleConflictError, match="idempotency_conflict"):
        activate(cursor, command("1", "2", "noop-key"))
    activate(cursor, command("1", "2", "other-key"))
    replay = activate(cursor, original)
    assert replay.replayed and replay.revision == "2"
    assert (cursor.head, cursor.catalogue, len(cursor.events)) == ("1", 8, 2)


def lifecycle_command(
    action: str,
    *,
    target: str = "2",
    key: str = "shared-lifecycle-key",
    expected: str | None = "2",
    binding: str = "asr-quality",
) -> BindingActivationCommand | BindingRetirementCommand:
    body = (
        {"expected_current_revision": expected}
        if action == "activate"
        else {"mode": action, "reason": "synthetic lifecycle reason"}
    )
    result = _command(
        ACTIVATE_OPERATION if action == "activate" else RETIRE_OPERATION,
        {"path": {"binding_id": binding, "revision": target}, "body": body},
        "synthetic-cross-operation",
        actor_type="anonymous",
        credential_label=None,
        key=key,
    )
    assert isinstance(result, (BindingActivationCommand, BindingRetirementCommand))
    return result


def run_lifecycle(
    cursor: Cursor, request: BindingActivationCommand | BindingRetirementCommand
) -> BindingLifecycleReceipt:
    adapter = PostgresBindingLifecycle(cast(Any, None))
    if isinstance(request, BindingActivationCommand):
        return adapter._activate(cast(Any, cursor), request)
    return adapter._retire(cast(Any, cursor), request)


def state(cursor: Cursor) -> tuple[object, ...]:
    return (
        cursor.head,
        cursor.catalogue,
        list(cursor.events),
        dict(cursor.retirements),
        list(cursor.disable_intents),
    )


@pytest.mark.parametrize(
    "first,second",
    [
        ("activate", "retire"),
        ("activate", "disable"),
        ("retire", "activate"),
        ("disable", "activate"),
        ("retire", "disable"),
        ("disable", "retire"),
    ],
)
def test_committed_key_is_shared_across_lifecycle_operations(first: str, second: str) -> None:
    cursor = Cursor()
    original = lifecycle_command(first)
    result = run_lifecycle(cursor, original)
    before = state(cursor)
    other = lifecycle_command(second, target="1" if second == "activate" else "2")
    assert original.key_digest == other.key_digest
    with pytest.raises(BindingLifecycleConflictError, match="idempotency_conflict"):
        run_lifecycle(cursor, other)
    assert state(cursor) == before
    assert run_lifecycle(cursor, original) == replace(result, replayed=True)
    assert state(cursor) == before


@pytest.mark.parametrize("change", ["binding", "revision", "caller"])
def test_key_scope_spans_bindings_revisions_and_callers(change: str) -> None:
    cursor = Cursor()
    original = lifecycle_command("activate")
    result = run_lifecycle(cursor, original)
    before = state(cursor)
    if change == "caller":
        # Credential/request metadata is not part of canonical operation intent.
        other = replace(
            original,
            actor_type="shared_credential",
            credential_label="synthetic-other",
            request_id="synthetic-another-request",
        )
        assert run_lifecycle(cursor, other) == replace(result, replayed=True)
    else:
        other = (
            lifecycle_command("activate", binding="tts-other")
            if change == "binding"
            else lifecycle_command("activate", target="1")
        )
        with pytest.raises(BindingLifecycleConflictError, match="idempotency_conflict"):
            run_lifecycle(cursor, other)
    assert state(cursor) == before


@pytest.mark.parametrize("action", ["retire", "disable"])
def test_rejected_activation_leaves_key_available_to_other_operations(action: str) -> None:
    cursor = Cursor()
    original = lifecycle_command("activate")
    assert isinstance(original, BindingActivationCommand)
    rejected = lifecycle_command("activate", expected="3")
    with pytest.raises(BindingActivationConflictError):
        run_lifecycle(cursor, rejected)
    assert cursor.events == []
    result = run_lifecycle(cursor, lifecycle_command(action))
    assert result.status == ("retired" if action == "retire" else "disabled")
    assert len(cursor.events) == 1
