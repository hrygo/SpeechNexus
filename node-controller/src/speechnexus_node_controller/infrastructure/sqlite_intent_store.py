"""Durable node-local intent facts for one restricted controller.

This store is deliberately not a second coordination authority. The scheduler
owns the authoritative runtime instance intents and receives the controller's
observations over ``internal/runtime-control``; what the controller must survive
is its own restart on its own node, so the record is a local file rather than a
shared database dependency. Terminal intents are never revived: a recorded
identity is immutable and a version-guarded write refuses a stale updater.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Final

from speechnexus_node_controller.domain.intents import (
    InvalidRuntimeRequestError,
    RuntimeInstanceIntent,
)
from speechnexus_node_controller.domain.payloads import (
    intent_record_payload,
    parse_intent_record,
)

SCHEMA_VERSION: Final = "1"

_SCHEMA: Final = (
    """
    CREATE TABLE IF NOT EXISTS runtime_intent_store_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_instance_intents (
        intent_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        instance_generation INTEGER NOT NULL,
        intent_state TEXT NOT NULL,
        version INTEGER NOT NULL,
        record TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_instance_intents_instance
        ON runtime_instance_intents (instance_id, instance_generation)
    """,
)


class IntentStoreError(Exception):
    """The durable intent record could not be read or written."""


class SqliteIntentStore:
    """Durable ``IntentStore`` adapter backed by one local SQLite database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def probe(self) -> None:
        """Fail when the store cannot answer, for readiness and startup checks."""
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM runtime_intent_store_meta WHERE key = 'schema_version'"
            ).fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            raise IntentStoreError("the runtime intent store schema version is not usable")

    def get(self, intent_id: str) -> RuntimeInstanceIntent | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT record FROM runtime_instance_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:
            return None
        return _decode(str(row[0]), intent_id)

    def add(self, intent: RuntimeInstanceIntent) -> None:
        record = _encode(intent)
        with self._lock, self._transaction():
            try:
                self._connection.execute(
                    "INSERT INTO runtime_instance_intents ("
                    "intent_id, instance_id, instance_generation, intent_state, version, record"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        intent.intent_id,
                        intent.instance_id,
                        intent.instance_generation,
                        intent.intent_state.value,
                        intent.version,
                        record,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"runtime intent {intent.intent_id} is already stored") from error

    def save(self, intent: RuntimeInstanceIntent, *, expected_version: int) -> None:
        record = _encode(intent)
        with self._lock, self._transaction():
            cursor = self._connection.execute(
                "UPDATE runtime_instance_intents SET instance_generation = ?, intent_state = ?, "
                "version = ?, record = ? WHERE intent_id = ? AND version = ?",
                (
                    intent.instance_generation,
                    intent.intent_state.value,
                    intent.version,
                    record,
                    intent.intent_id,
                    expected_version,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError(
                    f"runtime intent {intent.intent_id} was not stored at version "
                    f"{expected_version}"
                )

    def list_for_instance(self, instance_id: str) -> list[RuntimeInstanceIntent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT intent_id, record FROM runtime_instance_intents "
                "WHERE instance_id = ? ORDER BY rowid",
                (instance_id,),
            ).fetchall()
        return [_decode(str(record), str(intent_id)) for intent_id, record in rows]

    def _create_schema(self) -> None:
        with self._lock, self._transaction():
            for statement in _SCHEMA:
                self._connection.execute(statement)
            self._connection.execute(
                "INSERT INTO runtime_intent_store_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO NOTHING",
                (SCHEMA_VERSION,),
            )
        self.probe()

    def _transaction(self) -> _Transaction:
        return _Transaction(self._connection)


class _Transaction:
    """Serialize one durable write and roll it back on any failure."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self._connection.execute("COMMIT")
        else:
            self._connection.execute("ROLLBACK")


def _encode(intent: RuntimeInstanceIntent) -> str:
    return json.dumps(intent_record_payload(intent), separators=(",", ":"), sort_keys=True)


def _decode(record: str, intent_id: str) -> RuntimeInstanceIntent:
    try:
        decoded = json.loads(record)
    except json.JSONDecodeError as error:
        raise IntentStoreError(f"stored intent {intent_id} is not readable") from error
    if not isinstance(decoded, dict):
        raise IntentStoreError(f"stored intent {intent_id} is not an object")
    try:
        return parse_intent_record(decoded)
    except InvalidRuntimeRequestError as error:
        raise IntentStoreError(
            f"stored intent {intent_id} does not satisfy the runtime intent record shape"
        ) from error
