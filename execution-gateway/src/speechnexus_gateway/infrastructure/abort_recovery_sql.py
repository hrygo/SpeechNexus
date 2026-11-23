"""Advance bounded scheduling before processing; scheduling never authorizes a send.

A receipt is returned only while the original intent is unfinished, no decided
attempt outcome exists, and any previous attempt lease has expired. Taking it only
advances its next check; the claim transaction still decides whether a caller may
actually hold the next external attempt.

The due read locks its receipt rows itself and then updates them instead of relying
on a RETURNING clause, so the same statements run on PostgreSQL and on the InnoDB
coordination oracle. Locking stays on the receipt rows: the intent and lease reads
are ordinary reads, in the documented pool->submit->abort order.
"""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.abort_intent import AbortDispatchOutcome, AbortExecutionRequest
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor, to_unix_micros
from speechnexus_portable_sql.contract import ListMarker

_DUE_FILTER = (
    "FROM gateway_abort_intents a WHERE EXISTS ("
    "SELECT 1 FROM submit_intents s JOIN admission_leases l ON l.lease_id=s.lease_id "
    "WHERE s.submit_intent_id=a.submit_intent_id AND l.gateway_id=? "
    "AND l.host_node_id=? "
    "AND s.execution_state NOT IN ('succeeded','failed','aborted')) "
    "AND (a.dispatch_outcome IS NULL OR a.dispatch_outcome=?) "
    "AND (a.dispatch_deadline_at_us IS NULL OR a.dispatch_deadline_at_us<=?) "
    "AND COALESCE(NULLIF(a.next_check_at_us,0),a.received_at_us)<=? "
)

_DUE_ORDER = "COALESCE(NULLIF(a.next_check_at_us,0),a.received_at_us), a.submit_intent_id"
_RECEIPT_COLUMNS = (
    "submit_intent_id, operation_id, attempt_id, fencing_token, reason, wait_timeout_ms, "
    "instance_id, instance_generation"
)


_DUE = (
    "SELECT a.submit_intent_id FROM gateway_abort_intents a WHERE EXISTS ("
    "SELECT 1 FROM submit_intents s JOIN admission_leases l ON l.lease_id=s.lease_id "
    "WHERE s.submit_intent_id=a.submit_intent_id AND l.gateway_id=? "
    "AND l.host_node_id=? "
    "AND s.execution_state NOT IN ('succeeded','failed','aborted')) "
    "AND (a.dispatch_outcome IS NULL OR a.dispatch_outcome=?) "
    "AND (a.dispatch_deadline_at_us IS NULL OR a.dispatch_deadline_at_us<=?) "
    "AND COALESCE(NULLIF(a.next_check_at_us,0),a.received_at_us)<=? "
    "ORDER BY COALESCE(NULLIF(a.next_check_at_us,0),a.received_at_us), "
    "a.submit_intent_id LIMIT ? FOR UPDATE"
)
# 定长列表参数交给 ListMarker;语句文本不再随批次长度变化。
_MARK_DUE = "UPDATE gateway_abort_intents SET next_check_at_us=? WHERE submit_intent_id IN (?)"
_READ_RECEIPTS = (
    "SELECT "
    "submit_intent_id, operation_id, attempt_id, fencing_token, reason, "
    "wait_timeout_ms, instance_id, instance_generation "
    "FROM gateway_abort_intents WHERE submit_intent_id IN (?)"
)


def take_due_aborts(
    cursor: SubmitCursor,
    *,
    gateway_id: str,
    host_node_id: str,
    limit: int,
    clock: Callable[[], datetime],
) -> list[AbortExecutionRequest]:
    now_us = to_unix_micros(clock())
    due = (
        gateway_id,
        host_node_id,
        AbortDispatchOutcome.UNCONFIRMED.value,
        now_us,
        now_us,
    )
    cursor.execute(_DUE, (*due, limit))
    taken = [row["submit_intent_id"] for row in cursor.fetchall()]
    if not taken:
        return []
    identities = ListMarker(taken)
    cursor.execute(_MARK_DUE, (now_us + 30_000_000, identities))
    cursor.execute(_READ_RECEIPTS, (identities,))
    # The update above equalizes every next check, so the due order is the one the
    # locking read produced; the receipt rows are only read back, never re-ordered.
    rows = {row["submit_intent_id"]: row for row in cursor.fetchall()}
    return [AbortExecutionRequest(**rows[name]) for name in taken]
