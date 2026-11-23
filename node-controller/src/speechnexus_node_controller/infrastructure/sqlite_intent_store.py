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
from collections.abc import Callable
from contextlib import suppress
from functools import wraps
from pathlib import Path
from typing import Final

from speechnexus_node_controller.domain.intents import (
    IntentConflictError,
    InvalidRuntimeRequestError,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.payloads import (
    intent_record_payload,
    parse_intent_record,
)
from speechnexus_node_controller.domain.stop_checkpoint import (
    StopCheckpoint,
    parse_stop_checkpoint,
    stop_checkpoint_payload,
)
from speechnexus_portable_sql.transaction import savepoint

SCHEMA_VERSION: Final = "2"

#: meta 行的写入是**真 upsert**(改已有值),不是「插入即读」。三步放在同一事务里:
#: 先 `UPDATE`,只有 `rowcount == 0`(行还不存在)才 `INSERT`;插入这一步包在保存点里,
#: 这样撞唯一键时只回滚这一步,事务回到可用状态后可以再写一次。
_META_UPDATE: Final = "UPDATE runtime_intent_store_meta SET value = ? WHERE key = ?"
_META_INSERT: Final = "INSERT INTO runtime_intent_store_meta (key, value) VALUES (?, ?)"
_META_SAVEPOINT: Final = "runtime_intent_store_meta_upsert"

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
    """
    CREATE INDEX IF NOT EXISTS runtime_instance_intents_unresolved
        ON runtime_instance_intents (intent_state, intent_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_stop_checkpoints (
        intent_id TEXT PRIMARY KEY REFERENCES runtime_instance_intents(intent_id),
        record TEXT NOT NULL
    )
    """,
)


class IntentStoreError(Exception):
    """The durable intent record could not be read or written."""


def _upsert_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    """把 meta 行写成 `value`:先改,行不存在才插.

    为什么不是「插入即读」:`schema_version` 这一行必须跟随本进程的 ``SCHEMA_VERSION``;
    `INSERT ... DO NOTHING` 在行已存在时会留下旧值,把升级静默吞掉。

    为什么这里没有 `SELECT ... FOR UPDATE`:SQLite 没有行锁,写者由 ``BEGIN IMMEDIATE``
    的库级写锁串行化(见 ``_Transaction``),所以顺序是「先改后插」而不是「先锁后改」。
    插入仍包在保存点里:两步之间若另一个写者先插入同一个键,撞唯一键只会回滚这一步,
    事务回到可用状态后重写一次就收敛到同一个值 —— 这正是 ``savepoint`` 文档描述的形状。
    """

    cursor = connection.cursor()
    if cursor.execute(_META_UPDATE, (value, key)).rowcount:
        return
    try:
        with savepoint(cursor, _META_SAVEPOINT):
            cursor.execute(_META_INSERT, (key, value))
    except sqlite3.IntegrityError:
        if cursor.execute(_META_UPDATE, (value, key)).rowcount != 1:
            raise


def _checkpoint_errors[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except sqlite3.Error, IntentStoreError:
            raise RuntimeControllerUnavailableError(
                "the local stop checkpoint is unavailable"
            ) from None

    return wrapped


class SqliteIntentStore:
    """Durable ``IntentStore`` adapter backed by one local SQLite database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
        except BaseException:
            self._connection.close()
            raise

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

    @_checkpoint_errors
    def list_instances(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT instance_id FROM runtime_instance_intents ORDER BY instance_id"
            ).fetchall()
        return [str(instance_id) for (instance_id,) in rows]

    @_checkpoint_errors
    def list_for_instance(self, instance_id: str) -> list[RuntimeInstanceIntent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT intent_id, record FROM runtime_instance_intents "
                "WHERE instance_id = ? ORDER BY rowid",
                (instance_id,),
            ).fetchall()
        return [_decode(str(record), str(intent_id)) for intent_id, record in rows]

    def list_unresolved(
        self, *, after_intent_id: str | None, limit: int
    ) -> list[RuntimeInstanceIntent]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("an observation page requires a limit within 1..500")
        states = tuple(state.value for state in RuntimeIntentState if state.is_unresolved)
        placeholders = ", ".join("?" for _ in states)
        with self._lock:
            rows = self._connection.execute(
                "SELECT intent_id, record FROM runtime_instance_intents "
                f"WHERE intent_state IN ({placeholders}) "
                "AND intent_id > ? ORDER BY intent_id LIMIT ?",
                (*states, after_intent_id or "", limit),
            ).fetchall()
        return [_decode(str(record), str(intent_id)) for intent_id, record in rows]

    @_checkpoint_errors
    def get_stop_checkpoint(self, request: RuntimeInstanceIntentRequest) -> StopCheckpoint | None:
        with self._lock:
            checkpoint = self._read_stop_checkpoint(request.intent_id)
        if checkpoint is not None and checkpoint.request != request:
            raise IntentConflictError("the stop checkpoint belongs to another original request")
        return checkpoint

    @_checkpoint_errors
    def save_stop_checkpoint(self, checkpoint: StopCheckpoint) -> StopCheckpoint:
        with self._lock, self._transaction():
            row = self._connection.execute(
                "SELECT record FROM runtime_instance_intents WHERE intent_id = ?",
                (checkpoint.request.intent_id,),
            ).fetchone()
            if row is None or _decode(str(row[0]), checkpoint.request.intent_id).request != (
                checkpoint.request
            ):
                raise IntentConflictError("a checkpoint requires the persisted original stop")
            current = self._read_stop_checkpoint(checkpoint.request.intent_id)
            if current is None:
                if checkpoint.stage != "targeted":
                    raise InvalidRuntimeRequestError("the original target must be committed first")
                self._connection.execute(
                    "INSERT INTO runtime_stop_checkpoints (intent_id, record) VALUES (?, ?)",
                    (checkpoint.request.intent_id, _encode_checkpoint(checkpoint)),
                )
                return checkpoint
            updated = current.advance(checkpoint)
            if updated != current:
                self._connection.execute(
                    "UPDATE runtime_stop_checkpoints SET record = ? WHERE intent_id = ?",
                    (_encode_checkpoint(updated), checkpoint.request.intent_id),
                )
            return updated

    def _read_stop_checkpoint(self, intent_id: str) -> StopCheckpoint | None:
        row = self._connection.execute(
            "SELECT record FROM runtime_stop_checkpoints WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        checkpoint = _decode_checkpoint(str(row[0]))
        if checkpoint.request.intent_id != intent_id:
            raise IntentStoreError("the stop checkpoint identity is not usable")
        return checkpoint

    def _create_schema(self) -> None:
        with self._lock, self._transaction():
            self._connection.execute(_SCHEMA[0])
            version = self._connection.execute(
                "SELECT value FROM runtime_intent_store_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and version[0] not in {"1", SCHEMA_VERSION}:
                raise IntentStoreError("the runtime intent store schema version is not usable")
            for statement in _SCHEMA[1:]:
                self._connection.execute(statement)
            _upsert_meta(self._connection, "schema_version", SCHEMA_VERSION)
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
            try:
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    with suppress(sqlite3.Error):
                        self._connection.execute("ROLLBACK")
                raise
        else:
            self._connection.execute("ROLLBACK")


def _encode(intent: RuntimeInstanceIntent) -> str:
    return json.dumps(intent_record_payload(intent), separators=(",", ":"), sort_keys=True)


def _encode_checkpoint(checkpoint: StopCheckpoint) -> str:
    return json.dumps(stop_checkpoint_payload(checkpoint), separators=(",", ":"), sort_keys=True)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate stop checkpoint field")
        result[key] = value
    return result


def _decode_checkpoint(record: str) -> StopCheckpoint:
    try:
        payload = json.loads(record, object_pairs_hook=_unique_fields)
        if not isinstance(payload, dict):
            raise ValueError("the checkpoint must be an object")
        return parse_stop_checkpoint(payload)
    except ValueError, InvalidRuntimeRequestError:
        raise IntentStoreError("the stop checkpoint record is not usable") from None


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
