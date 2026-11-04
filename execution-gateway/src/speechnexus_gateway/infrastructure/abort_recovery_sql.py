"""Advance bounded scheduling before processing; scheduling never authorizes a send."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.abort_intent import AbortExecutionRequest
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor, to_unix_micros


def take_due_aborts(
    cursor: SubmitCursor,
    *,
    gateway_id: str,
    host_node_id: str,
    limit: int,
    clock: Callable[[], datetime],
) -> list[AbortExecutionRequest]:
    now_us = to_unix_micros(clock())
    cursor.execute(
        "UPDATE gateway_abort_intents SET next_check_at_us=%s WHERE submit_intent_id IN ("
        "SELECT a.submit_intent_id FROM gateway_abort_intents a "
        "JOIN submit_intents s ON s.submit_intent_id=a.submit_intent_id "
        "JOIN admission_leases l ON l.lease_id=s.lease_id "
        "WHERE l.gateway_id=%s AND l.host_node_id=%s "
        "AND COALESCE(NULLIF(a.next_check_at_us,0),a.received_at_us)<=%s "
        "AND s.execution_state NOT IN ('succeeded','failed','aborted') "
        "ORDER BY COALESCE(NULLIF(a.next_check_at_us,0),a.received_at_us), "
        "a.submit_intent_id LIMIT %s) "
        "AND COALESCE(NULLIF(next_check_at_us,0),received_at_us)<=%s "
        "RETURNING submit_intent_id, operation_id, attempt_id, fencing_token, "
        "reason, wait_timeout_ms",
        (now_us + 30_000_000, gateway_id, host_node_id, now_us, limit, now_us),
    )
    return [AbortExecutionRequest(**row) for row in cursor.fetchall()]
