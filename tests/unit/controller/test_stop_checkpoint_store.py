"""Stop facts survive reopen, cannot be retargeted, and upgrade v1 without changing receipts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.fake.controller import FIXED_NOW, build_request

from speechnexus_node_controller.domain.intents import (
    IntentConflictError,
    InvalidRuntimeRequestError,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
)
from speechnexus_node_controller.domain.payloads import intent_record_payload
from speechnexus_node_controller.domain.stop_checkpoint import (
    StopCheckpoint,
    stop_checkpoint_payload,
)
from speechnexus_node_controller.infrastructure.sqlite_intent_store import (
    SCHEMA_VERSION,
    IntentStoreError,
    SqliteIntentStore,
)


def pending(store: SqliteIntentStore) -> StopCheckpoint:
    request = build_request(action=RuntimeActionKind.STOP)
    store.add(RuntimeInstanceIntent(request, FIXED_NOW, FIXED_NOW))
    return StopCheckpoint(request=request, container_id="container-original")


def test_checkpoints_are_monotonic_across_connections_and_keep_the_first_evidence_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intents.sqlite3"
    first, second = SqliteIntentStore(path), SqliteIntentStore(path)
    try:
        target = pending(first)
        assert first.save_stop_checkpoint(target) == target
        exit_fact = replace(target, exit_code=143, exited_at=FIXED_NOW)
        assert second.save_stop_checkpoint(exit_fact) == exit_fact
        assert first.save_stop_checkpoint(target) == exit_fact
        later_exit = replace(exit_fact, exited_at=FIXED_NOW + timedelta(seconds=1))
        assert first.save_stop_checkpoint(later_exit) == exit_fact
        released = replace(exit_fact, released_at=FIXED_NOW + timedelta(seconds=2))
        assert second.save_stop_checkpoint(released) == released
        assert (
            first.save_stop_checkpoint(
                replace(released, released_at=FIXED_NOW + timedelta(seconds=3))
            )
            == released
        )
        assert first.save_stop_checkpoint(target) == released
    finally:
        first.close()
        second.close()
    reopened = SqliteIntentStore(path)
    try:
        restored = reopened.get_stop_checkpoint(target.request)
        assert restored == released
        assert restored is not None and restored.evidence() == released.evidence()
    finally:
        reopened.close()


def test_no_checkpoint_can_skip_the_original_target_or_committed_exit(tmp_path: Path) -> None:
    store = SqliteIntentStore(tmp_path / "intents.sqlite3")
    try:
        target = pending(store)
        exited = replace(target, exit_code=0, exited_at=FIXED_NOW)
        released = replace(exited, released_at=FIXED_NOW)
        with pytest.raises(InvalidRuntimeRequestError, match="target"):
            store.save_stop_checkpoint(exited)
        store.save_stop_checkpoint(target)
        with pytest.raises(InvalidRuntimeRequestError, match="exit checkpoint"):
            store.save_stop_checkpoint(released)
        assert store.get_stop_checkpoint(target.request) == target
    finally:
        store.close()


def test_a_failed_commit_rolls_back_and_allows_a_later_checkpoint_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect

    class FailingCommit(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if self.fail_commit and sql == "COMMIT":
                raise sqlite3.OperationalError("synthetic commit failure")
            return super().execute(sql, parameters)

    connection = real_connect(
        tmp_path / "intents.sqlite3",
        check_same_thread=False,
        isolation_level=None,
        factory=FailingCommit,
    )
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)
    store = SqliteIntentStore(tmp_path / "intents.sqlite3")
    try:
        target = pending(store)
        connection.fail_commit = True
        with pytest.raises(RuntimeControllerUnavailableError):
            store.save_stop_checkpoint(target)
        assert not connection.in_transaction
        assert store.get_stop_checkpoint(target.request) is None
        connection.fail_commit = False
        assert store.save_stop_checkpoint(target) == target
    finally:
        store.close()


def test_only_the_persisted_original_stop_can_pin_a_target(tmp_path: Path) -> None:
    store = SqliteIntentStore(tmp_path / "intents.sqlite3")
    try:
        request = build_request(action=RuntimeActionKind.STOP)
        with pytest.raises(IntentConflictError, match="persisted original"):
            store.save_stop_checkpoint(StopCheckpoint(request, "container-original"))
        target = pending(store)
        store.save_stop_checkpoint(target)
        for altered in (
            replace(target, container_id="container-replacement"),
            replace(target, request=replace(request, instance_generation=2)),
            replace(target, request=replace(request, fencing_token=2)),
            replace(target, request=replace(request, deadline_at=FIXED_NOW)),
        ):
            with pytest.raises(IntentConflictError):
                store.save_stop_checkpoint(altered)
        exited = replace(target, exit_code=143, exited_at=FIXED_NOW)
        store.save_stop_checkpoint(exited)
        with pytest.raises(IntentConflictError, match="exit fact"):
            store.save_stop_checkpoint(replace(exited, exit_code=0))
        with pytest.raises(IntentConflictError, match="original request"):
            store.get_stop_checkpoint(replace(request, gateway_id="other-gateway"))
        assert store.get_stop_checkpoint(request) == exited
    finally:
        store.close()


@pytest.mark.parametrize(
    "corruption", ("duplicate", "wrong-id", "unknown", "partial", "invalid-json")
)
def test_corrupt_checkpoint_never_yields_a_proof_or_private_exception_detail(
    tmp_path: Path, corruption: str
) -> None:
    store = SqliteIntentStore(tmp_path / "intents.sqlite3")
    try:
        target = pending(store)
        store.save_stop_checkpoint(target)
        payload = stop_checkpoint_payload(target)
        if corruption == "duplicate":
            raw = json.dumps(payload)[:-1] + ', "format_version": 1}'
        elif corruption == "wrong-id":
            payload["request"]["intent_id"] = "intent-other"
            raw = json.dumps(payload)
        elif corruption == "unknown":
            raw = json.dumps(payload | {"SYNTHETIC_PRIVATE_DETAIL": True})
        elif corruption == "partial":
            raw = json.dumps(payload | {"released_at": FIXED_NOW.isoformat()})
        else:
            raw = "SYNTHETIC_PRIVATE_DETAIL"
        with sqlite3.connect(store.path) as connection:
            connection.execute("UPDATE runtime_stop_checkpoints SET record = ?", (raw,))
        with pytest.raises(RuntimeControllerUnavailableError) as error:
            store.get_stop_checkpoint(target.request)
        assert str(error.value) == "the local stop checkpoint is unavailable"
    finally:
        store.close()


def test_v1_upgrade_retains_every_original_receipt_and_unknown_version_is_not_modified(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intents.sqlite3"
    request = build_request()
    original = RuntimeInstanceIntent(request, FIXED_NOW, FIXED_NOW)
    encoded = json.dumps(intent_record_payload(original))
    # The genuine v1 table layout from main 381e685; the record format stays unchanged.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE runtime_intent_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO runtime_intent_store_meta VALUES ('schema_version', '1');"
            "CREATE TABLE runtime_instance_intents (intent_id TEXT PRIMARY KEY,"
            "instance_id TEXT NOT NULL, instance_generation INTEGER NOT NULL,"
            "intent_state TEXT NOT NULL, version INTEGER NOT NULL, record TEXT NOT NULL);"
        )
        connection.execute(
            "INSERT INTO runtime_instance_intents VALUES (?, ?, 1, 'pending', 1, ?)",
            (request.intent_id, request.instance_id, encoded),
        )
    store = SqliteIntentStore(path)
    try:
        assert store.get(request.intent_id) == original
        store.probe()
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT record FROM runtime_instance_intents").fetchone() == (
            encoded,
        )
        assert connection.execute("SELECT value FROM runtime_intent_store_meta").fetchone() == (
            SCHEMA_VERSION,
        )
        assert connection.execute("SELECT count(*) FROM runtime_stop_checkpoints").fetchone() == (
            0,
        )
        connection.execute("UPDATE runtime_intent_store_meta SET value = '999'")
        before = connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    with pytest.raises(IntentStoreError, match="schema version"):
        SqliteIntentStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall() == (before)
        assert connection.execute("SELECT value FROM runtime_intent_store_meta").fetchone() == (
            "999",
        )
