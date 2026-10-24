"""Short, owner/version-fenced transport transactions shared by SQL adapters."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol

from speechnexus_backend.speech.domain.runtime_delivery import (
    DeliveryClaim,
    DeliveryClaimRequest,
    DeliveryStart,
    DeliveryTicket,
    DeliveryWrite,
)
from speechnexus_backend.speech.domain.runtime_intents import RuntimeIntent, integer
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    decode_record,
    validate_original_observation,
)


class DeliveryCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _rows(cursor: DeliveryCursor, sql: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute(sql, args)
    return cursor.fetchall()


def _original(cursor: DeliveryCursor, event_id: str) -> tuple[RuntimeIntent, str, dict[str, Any]]:
    records = _rows(
        cursor,
        "SELECT o.event_id, o.event_digest, o.target_node_id, o.intent_id, "
        "r.request_document, r.request_digest, r.instance_id, r.instance_generation, r.action, "
        "r.intent_state, r.observation_document, h.current_generation, "
        "h.current_prepare_intent_id, h.current_stop_intent_id, "
        "i.instance_state, i.base_budget_kind "
        "FROM coordination_outbox o JOIN runtime_instance_intents r "
        "ON r.target_node_id=o.target_node_id AND r.intent_id=o.intent_id "
        "LEFT JOIN runtime_instance_heads h ON h.instance_id=r.instance_id "
        "LEFT JOIN engine_instances i ON i.instance_id=r.instance_id "
        "AND i.instance_generation=r.instance_generation "
        "WHERE o.event_id=%s AND o.event_kind='runtime' AND o.delivered_at_us IS NULL",
        (event_id,),
    )
    if len(records) != 1:
        raise ValueError("No authoritative original runtime delivery")
    row = records[0]
    prepared, _, _ = decode_record(row["request_document"])
    original = prepared.request
    if (
        row["event_id"],
        row["event_digest"],
        row["request_digest"],
        row["target_node_id"],
        row["intent_id"],
        row["instance_id"],
        row["instance_generation"],
        row["action"],
    ) != (
        prepared.event_id,
        prepared.request_digest,
        prepared.request_digest,
        original.binding.target_node_id,
        original.intent_id,
        original.instance_id,
        original.instance_generation,
        original.action,
    ):
        raise ValueError("Runtime delivery cannot reinterpret its original identity")
    return original, prepared.request_digest, row


def _eligible(original: RuntimeIntent, row: dict[str, Any], now_us: int) -> bool:
    if (
        original.deadline.at_us <= now_us
        or row["intent_state"] != "pending"
        or row["observation_document"] is not None
        or row["current_generation"] != original.instance_generation
    ):
        return False
    if original.action == "prepare":
        return bool(
            row["current_prepare_intent_id"] == original.intent_id
            and row["current_stop_intent_id"] is None
            and row["instance_state"] == "preparing"
            and row["base_budget_kind"] == "peak"
        )
    return bool(
        row["current_stop_intent_id"] == original.intent_id
        and row["instance_state"] == "draining"
        and row["base_budget_kind"] in ("peak", "steady", "q_peak", "q_steady")
    )


def _ticket(row: dict[str, Any], original: RuntimeIntent, request_digest: str) -> DeliveryTicket:
    return DeliveryTicket(
        row["event_id"],
        request_digest,
        row["claim_holder_id"],
        row["version"],
        row["claim_expires_at_us"],
        row["delivery_phase"],
        original,
    )


def _owned(cursor: DeliveryCursor, ticket: DeliveryTicket, now_us: int) -> dict[str, Any] | None:
    rows = _rows(
        cursor,
        "SELECT * FROM runtime_intent_deliveries WHERE event_id=%s FOR UPDATE",
        (ticket.event_id,),
    )
    if not rows:
        return None
    row = rows[0]
    if (
        row["claim_holder_id"] != ticket.holder_id
        or row["version"] != ticket.version
        or row["claim_expires_at_us"] != ticket.claim_expires_at_us
        or row["claim_expires_at_us"] <= now_us
        or row["delivery_phase"] not in ("unsent", "unknown")
    ):
        return None
    return row


def claim_in_transaction(
    cursor: DeliveryCursor, request: DeliveryClaimRequest, *, clock: Callable[[], int]
) -> DeliveryClaim:
    now_us = clock()
    parameters: tuple[object, ...] = (now_us, now_us, *request.target_node_ids)
    nodes = ",".join("%s" for _ in request.target_node_ids)
    # Only the outer ledger row is locked. The nested read does not grant the
    # delivery worker UPDATE rights on immutable intents, heads or budgets.
    rows = _rows(
        cursor,
        "SELECT d.* FROM runtime_intent_deliveries d "
        "WHERE d.delivery_phase IN ('unsent','unknown') AND d.next_attempt_at_us<=%s "
        "AND d.claim_expires_at_us<=%s AND EXISTS (SELECT 1 FROM coordination_outbox o "
        "WHERE o.event_id=d.event_id AND o.event_kind='runtime' AND o.delivered_at_us IS NULL "
        "AND o.target_node_id IN (" + nodes + ")) "
        "ORDER BY d.next_attempt_at_us,d.event_id LIMIT 1 FOR UPDATE SKIP LOCKED",
        parameters,
    )
    if not rows:
        return DeliveryClaim("empty")
    row = rows[0]
    now_us = clock()  # Any wait precedes the new lease, not its expiration.
    try:
        original, request_digest, _ = _original(cursor, row["event_id"])
    except ValueError, TypeError, KeyError:
        cursor.execute(
            "UPDATE runtime_intent_deliveries SET delivery_phase='blocked', "
            "claim_holder_id=NULL,claim_expires_at_us=0,last_failure_code='invalid_record', "
            "updated_at_us=%s,version=version+1 WHERE event_id=%s AND version=%s",
            (now_us, row["event_id"], row["version"]),
        )
        return DeliveryClaim("blocked")
    now_us = clock()
    expires = integer(now_us + request.lease_us, minimum=1)
    cursor.execute(
        "UPDATE runtime_intent_deliveries SET claim_holder_id=%s,claim_expires_at_us=%s, "
        "updated_at_us=%s,version=version+1 WHERE event_id=%s AND version=%s",
        (request.holder_id, expires, now_us, row["event_id"], row["version"]),
    )
    if cursor.rowcount != 1:
        raise ValueError("Runtime delivery claim lost its row version")
    updated = {
        **row,
        "claim_holder_id": request.holder_id,
        "claim_expires_at_us": expires,
        "version": row["version"] + 1,
    }
    return DeliveryClaim("claimed", _ticket(updated, original, request_digest))


def start_in_transaction(
    cursor: DeliveryCursor, ticket: DeliveryTicket, *, clock: Callable[[], int]
) -> DeliveryStart:
    row = _owned(cursor, ticket, clock())
    if row is None or row["delivery_phase"] != "unsent":
        return DeliveryStart("stale")
    original, request_digest, facts = _original(cursor, ticket.event_id)
    if request_digest != ticket.request_digest:
        return DeliveryStart("rejected")
    now_us = clock()
    if row["claim_expires_at_us"] <= now_us:
        return DeliveryStart("stale")
    submit = _eligible(original, facts, now_us)
    # This marker MUST commit before calling the controller. If this commit's
    # reply is lost, the caller has no permit; a successor may only GET.
    cursor.execute(
        "UPDATE runtime_intent_deliveries SET delivery_phase='unknown', "
        "submission_started_at_us=%s,updated_at_us=%s,version=version+1 "
        "WHERE event_id=%s AND version=%s AND claim_holder_id=%s AND claim_expires_at_us>%s",
        (
            now_us if submit else None,
            now_us,
            ticket.event_id,
            ticket.version,
            ticket.holder_id,
            now_us,
        ),
    )
    if cursor.rowcount != 1:
        return DeliveryStart("stale")
    updated = {**row, "delivery_phase": "unknown", "version": row["version"] + 1}
    return DeliveryStart("committed", _ticket(updated, original, request_digest), submit)


def acknowledge_in_transaction(
    cursor: DeliveryCursor,
    ticket: DeliveryTicket,
    original_response: str,
    *,
    clock: Callable[[], int],
) -> DeliveryWrite:
    row = _owned(cursor, ticket, clock())
    if row is None or row["delivery_phase"] != "unknown":
        return DeliveryWrite("stale")
    original, request_digest, _ = _original(cursor, ticket.event_id)
    if request_digest != ticket.request_digest:
        return DeliveryWrite("rejected")
    # The existing parser owns identities, digest, states and stop-proof shape.
    # This is an ACK candidate, never an authoritative ready/stop projection.
    try:
        validate_original_observation(original_response, original)
    except ValueError, TypeError, KeyError:
        return DeliveryWrite("rejected")
    events = _rows(
        cursor,
        "SELECT event_id FROM coordination_outbox WHERE event_id=%s AND event_kind='runtime' "
        "AND event_digest=%s AND delivered_at_us IS NULL FOR UPDATE",
        (ticket.event_id, request_digest),
    )
    if len(events) != 1:
        return DeliveryWrite("stale")
    # The outbox row may introduce a lock wait. Recheck the lease after every
    # required lock, before either ACK write; an old holder cannot finish late.
    now_us = clock()
    if row["claim_expires_at_us"] <= now_us:
        return DeliveryWrite("stale")
    cursor.execute(
        "UPDATE coordination_outbox SET delivered_at_us=%s WHERE event_id=%s "
        "AND event_kind='runtime' AND event_digest=%s AND delivered_at_us IS NULL",
        (now_us, ticket.event_id, request_digest),
    )
    if cursor.rowcount != 1:
        return DeliveryWrite("stale")
    cursor.execute(
        "UPDATE runtime_intent_deliveries SET delivery_phase='acknowledged', "
        "claim_holder_id=NULL,claim_expires_at_us=0,acknowledged_at_us=%s, "
        "last_failure_code=NULL,updated_at_us=%s,version=version+1 "
        "WHERE event_id=%s AND version=%s AND claim_holder_id=%s",
        (now_us, now_us, ticket.event_id, ticket.version, ticket.holder_id),
    )
    if cursor.rowcount != 1:
        # The outbox ACK must roll back together with this private record.
        raise ValueError("Runtime delivery acknowledgement lost its row version")
    return DeliveryWrite("committed")


def defer_in_transaction(
    cursor: DeliveryCursor,
    ticket: DeliveryTicket,
    failure_code: str,
    delay_us: int,
    *,
    clock: Callable[[], int],
) -> DeliveryWrite:
    if not isinstance(failure_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", failure_code):
        raise ValueError("Invalid runtime delivery failure code")
    integer(delay_us, minimum=100000, maximum=300000000)
    row = _owned(cursor, ticket, clock())
    if row is None:
        return DeliveryWrite("stale")
    now_us = clock()
    if row["claim_expires_at_us"] <= now_us:
        return DeliveryWrite("stale")
    retry_at = integer(now_us + delay_us, minimum=1)
    cursor.execute(
        "UPDATE runtime_intent_deliveries SET claim_holder_id=NULL,claim_expires_at_us=0, "
        "next_attempt_at_us=%s,last_failure_code=%s,updated_at_us=%s,version=version+1 "
        "WHERE event_id=%s AND version=%s AND claim_holder_id=%s",
        (retry_at, failure_code, now_us, ticket.event_id, ticket.version, ticket.holder_id),
    )
    return DeliveryWrite("committed" if cursor.rowcount == 1 else "stale")
