"""Durable intent facts survive a controller restart without becoming a second authority."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    SAMPLE_INSTANCE_ID,
    build_request,
)

from speechnexus_node_controller.domain.intents import (
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeInstanceIntent,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
    RuntimeStopEvidence,
)
from speechnexus_node_controller.domain.payloads import (
    intent_record_payload,
    request_payload,
)
from speechnexus_node_controller.infrastructure.sqlite_intent_store import (
    SCHEMA_VERSION,
    IntentStoreError,
    SqliteIntentStore,
)


def _store(tmp_path: Path, name: str = "runtime-intents.sqlite3") -> SqliteIntentStore:
    return SqliteIntentStore(tmp_path / name)


def _intent(**overrides: object) -> RuntimeInstanceIntent:
    request_fields: dict[str, object] = {}
    intent_fields: dict[str, object] = {}
    for key, value in overrides.items():
        if key in {
            "intent_state",
            "observed_instance_state",
            "memory_hold",
            "stop_evidence",
            "observed_peak_memory",
            "error_code",
            "version",
        }:
            intent_fields[key] = value
        else:
            request_fields[key] = value
    return RuntimeInstanceIntent(
        request=build_request(**request_fields),
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        **intent_fields,  # type: ignore[arg-type]
    )


def test_a_recorded_intent_round_trips_through_the_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    store.add(intent)

    loaded = store.get("intent-1")

    assert loaded is not None
    assert loaded.request == intent.request
    assert loaded.intent_state is RuntimeIntentState.PENDING
    assert loaded.memory_hold is MemoryHold.PEAK
    assert loaded.version == 1
    store.close()


def test_intent_facts_survive_a_controller_restart(tmp_path: Path) -> None:
    first = _store(tmp_path)
    first.add(_intent())
    first.close()

    restarted = _store(tmp_path)
    loaded = restarted.get("intent-1")

    assert loaded is not None
    assert loaded.request == _intent().request
    restarted.close()


def test_a_succeeded_stop_keeps_its_verified_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(
        intent_id="intent-stop-1",
        action=RuntimeActionKind.STOP,
        intent_state=RuntimeIntentState.SUCCEEDED,
        observed_instance_state=ObservedInstanceState.STOPPED,
        stop_evidence=RuntimeStopEvidence(
            verified_at=FIXED_NOW, exit_code=143, runtime_container_id="container-1"
        ),
    )
    store.add(intent)

    loaded = store.get("intent-stop-1")

    assert loaded is not None
    assert loaded.stop_evidence is not None
    assert loaded.stop_evidence.exit_code == 143
    assert loaded.memory_hold is MemoryHold.NONE
    store.close()


def test_a_losing_updater_cannot_overwrite_a_recorded_intent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    store.add(intent)
    intent.quarantine(RuntimeIntentErrorCode.UNTRACKED_INSTANCE_OBSERVED, at=FIXED_NOW)
    store.save(intent, expected_version=1)

    with pytest.raises(ValueError, match="version"):
        store.save(intent, expected_version=1)

    stored = store.get("intent-1")
    assert stored is not None
    assert stored.version == 2
    assert stored.intent_state is RuntimeIntentState.QUARANTINED
    store.close()


def test_the_same_intent_identity_is_never_stored_twice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_intent())

    with pytest.raises(ValueError, match="already stored"):
        store.add(_intent())

    store.close()


def test_intents_are_listed_in_recording_order_per_instance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_intent(intent_id="intent-1"))
    store.add(_intent(intent_id="intent-2", instance_generation=2, fencing_token=2))
    store.add(_intent(intent_id="intent-other", instance_id="instance-other"))

    listed = store.list_for_instance(SAMPLE_INSTANCE_ID)

    assert [intent.intent_id for intent in listed] == ["intent-1", "intent-2"]
    assert store.list_for_instance("instance-unknown") == []
    store.close()


def test_an_unusable_store_file_fails_instead_of_serving_partial_state(tmp_path: Path) -> None:
    path = tmp_path / "runtime-intents.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE runtime_intent_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO runtime_intent_store_meta (key, value) VALUES ('schema_version', '999')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(IntentStoreError, match="schema version"):
        SqliteIntentStore(path)


def test_a_corrupted_record_is_reported_instead_of_guessed(tmp_path: Path) -> None:
    path = tmp_path / "runtime-intents.sqlite3"
    store = SqliteIntentStore(path)
    store.add(_intent())
    store.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE runtime_instance_intents SET record = ? WHERE intent_id = ?",
        (json.dumps({"request": {"intent_id": "intent-1"}}), "intent-1"),
    )
    connection.commit()
    connection.close()

    reopened = SqliteIntentStore(path)
    with pytest.raises(IntentStoreError, match="record shape"):
        reopened.get("intent-1")
    reopened.close()


def test_the_record_carries_the_full_request_and_the_store_schema_is_versioned(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.add(_intent())
    store.probe()

    connection = sqlite3.connect(store.path)
    version = connection.execute(
        "SELECT value FROM runtime_intent_store_meta WHERE key = 'schema_version'"
    ).fetchone()
    record = connection.execute(
        "SELECT record FROM runtime_instance_intents WHERE intent_id = 'intent-1'"
    ).fetchone()
    connection.close()

    assert version == (SCHEMA_VERSION,)
    assert record is not None
    stored = json.loads(record[0])
    assert stored["request"] == request_payload(build_request())
    assert stored == intent_record_payload(_intent())
    store.close()
