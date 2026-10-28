"""Persist and recover fixed disable targets on a caller-owned transaction.

The caller owns commit and authority. Capturing targets is not a cancellation,
does not prove enumeration complete, and does not release any lease.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol

from speechnexus_backend.speech.domain.binding_disable_intent import DisableExecutionTarget
from speechnexus_backend.speech.domain.runtime_intents import identifier, integer
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    ABORT_INTENT_TABLE,
    TARGET_TABLE,
)

_FIELDS = (
    "submit_intent_id",
    "operation_id",
    "attempt_id",
    "lease_id",
    "stage_id",
    "workspace_id",
    "pool_id",
    "gateway_id",
    "host_node_id",
    "instance_id",
    "instance_generation",
    "binding_revision_id",
    "policy_revision_id",
    "voice_revision_id",
    "fencing_token",
    "command_digest",
    "wait_timeout_ms",
)


class TargetCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _target(row: dict[str, Any]) -> DisableExecutionTarget:
    return DisableExecutionTarget(**{name: row[name] for name in _FIELDS})


def list_disable_event_ids(cursor: TargetCursor, *, after: str | None, limit: int) -> list[str]:
    """Page original intents; a polling pass must restart at the beginning."""
    integer(limit, minimum=1, maximum=128)
    if after is not None:
        identifier(after)
    cursor.execute(
        "SELECT event_id FROM speech_binding_disable_intents "
        "WHERE (%s::text IS NULL OR event_id > %s) ORDER BY event_id LIMIT %s",
        (after, after, limit),
    )
    event_ids = [row["event_id"] for row in cursor.fetchall()]
    for event_id in event_ids:
        identifier(event_id)
    return event_ids


def list_disable_targets(
    cursor: TargetCursor,
    event_id: str,
    *,
    after: str | None,
    limit: int,
    gateway_id: str | None = None,
    node_id: str | None = None,
) -> list[DisableExecutionTarget]:
    """Recover the original targets, including targets whose outcome is unknown."""
    identifier(event_id)
    integer(limit, minimum=1, maximum=128)
    if after is not None:
        identifier(after)
    if (gateway_id is None) != (node_id is None):
        raise ValueError("gateway and node scope must be specified together")
    if gateway_id is not None and node_id is not None:
        identifier(gateway_id)
        identifier(node_id)
    cursor.execute(
        f"SELECT {', '.join(_FIELDS)} FROM {TARGET_TABLE} "
        "WHERE event_id=%s AND (%s::text IS NULL OR submit_intent_id > %s) "
        "AND (%s::text IS NULL OR gateway_id=%s) "
        "AND (%s::text IS NULL OR host_node_id=%s) "
        "ORDER BY submit_intent_id LIMIT %s",
        (event_id, after, after, gateway_id, gateway_id, node_id, node_id, limit),
    )
    return [_target(row) for row in cursor.fetchall()]


def read_disable_target(
    cursor: TargetCursor, event_id: str, submit_intent_id: str
) -> DisableExecutionTarget | None:
    """Recover an already-persisted target by both original identities."""
    identifier(event_id)
    identifier(submit_intent_id)
    cursor.execute(
        f"SELECT {', '.join(_FIELDS)} FROM {TARGET_TABLE} "
        "WHERE event_id=%s AND submit_intent_id=%s",
        (event_id, submit_intent_id),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Duplicate binding-disable target")
    return None if not rows else _target(rows[0])


def record_abort_intent(
    cursor: TargetCursor, event_id: str, submit_intent_id: str, *, recorded_at_us: int
) -> bool:
    """Return true only for the transaction that inserted the original send intent.

    The caller may send only after this transaction commits. A preexisting intent
    is an ambiguous prior send and requires original-identity query, not resend.
    """
    identifier(event_id)
    identifier(submit_intent_id)
    integer(recorded_at_us, minimum=1)
    cursor.execute(
        f"INSERT INTO {ABORT_INTENT_TABLE} (event_id, submit_intent_id, recorded_at_us) "
        "VALUES (%s,%s,%s) ON CONFLICT (event_id, submit_intent_id) DO NOTHING "
        "RETURNING event_id",
        (event_id, submit_intent_id, recorded_at_us),
    )
    return bool(cursor.fetchall())


def capture_disable_targets(
    cursor: TargetCursor,
    event_id: str,
    *,
    wait_timeout_ms: int,
    limit: int,
    gateway_id: str | None = None,
    node_id: str | None = None,
) -> list[DisableExecutionTarget]:
    """Append one bounded batch from recorded active submissions for this event.

    Repeat to discover later visible submissions. No returned batch establishes
    that concurrent admission or dispatch has been fenced or that work stopped.
    """
    identifier(event_id)
    integer(wait_timeout_ms, minimum=100, maximum=60000)
    integer(limit, minimum=1, maximum=128)
    if (gateway_id is None) != (node_id is None):
        raise ValueError("gateway and node scope must be specified together")
    if gateway_id is not None and node_id is not None:
        identifier(gateway_id)
        identifier(node_id)
    cursor.execute(
        "SELECT s.submit_intent_id, s.operation_id, s.attempt_id, l.lease_id, l.stage_id, "
        "a.workspace_id, l.pool_id, l.gateway_id, l.host_node_id, l.instance_id, "
        "l.instance_generation, a.binding_revision_id, a.policy_revision_id, "
        "s.voice_revision_id, l.fencing_token, s.command_digest, %s AS wait_timeout_ms "
        "FROM speech_binding_disable_intents i "
        "JOIN speech_publication_lifecycle_events e ON e.event_id=i.event_id "
        "JOIN admission_attempts a ON a.binding_revision_id=e.revision_id "
        "JOIN admission_leases l ON l.operation_id=a.operation_id AND l.attempt_id=a.attempt_id "
        "JOIN submit_intents s ON s.lease_id=l.lease_id AND s.operation_id=l.operation_id "
        "AND s.attempt_id=l.attempt_id "
        "WHERE i.event_id=%s AND e.action='model_binding.disable' "
        "AND s.execution_state IN ('submitting','executing','stopping','unknown_quarantined') "
        "AND (%s::text IS NULL OR l.gateway_id=%s) "
        "AND (%s::text IS NULL OR l.host_node_id=%s) "
        f"AND NOT EXISTS (SELECT 1 FROM {TARGET_TABLE} t "
        "WHERE t.event_id=i.event_id AND t.submit_intent_id=s.submit_intent_id) "
        "ORDER BY s.submit_intent_id LIMIT %s",
        (wait_timeout_ms, event_id, gateway_id, gateway_id, node_id, node_id, limit),
    )
    candidates = [_target(row) for row in cursor.fetchall()]
    captured = []
    for target in candidates:
        values = tuple(getattr(target, name) for name in _FIELDS)
        cursor.execute(
            f"INSERT INTO {TARGET_TABLE} (event_id, {', '.join(_FIELDS)}) "
            f"VALUES ({', '.join(['%s'] * (len(_FIELDS) + 1))}) "
            "ON CONFLICT (event_id, submit_intent_id) DO NOTHING",
            (event_id, *values),
        )
        stored = read_disable_target(cursor, event_id, target.submit_intent_id)
        if stored is None or stored != replace(target, wait_timeout_ms=stored.wait_timeout_ms):
            raise ValueError("Binding-disable target identity changed")
        captured.append(stored)
    return captured
