"""Independent, bounded, read-only recovery of an execution and its abort receipt."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_gateway.domain.abort_intent import AbortExecutionRequest
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.recovery import AbortReceipt, AbortRecovery, ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIntent,
    SubmissionStoreError,
    SubmissionStoreUnavailableError,
)
from speechnexus_gateway.infrastructure.original_command_state import read_original_document_state
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.reader_authority import (
    ExecutionReaderTarget,
    read_execution_reader_target,
)
from speechnexus_gateway.infrastructure.reader_authority import (
    verify_execution_reader_authority as verify_execution_reader_authority,
)
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    from_unix_micros,
    read_submit_intent,
)


class _BudgetExpiredError(Exception):
    pass


class _DeadlineCursor:
    """Apply the remaining total time budget to each read statement."""

    def __init__(self, cursor: SubmitCursor, deadline: float) -> None:
        self._cursor = cursor
        self._deadline = deadline

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetExpiredError
        self._cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._cursor.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cursor.fetchall()


def _instant(value: object) -> datetime:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ValueError("invalid persisted instant")
    return from_unix_micros(value)


def _read_receipt(cursor: SubmitCursor, intent: SubmissionIntent) -> AbortReceipt | None:
    cursor.execute(
        "SELECT submit_intent_id, operation_id, attempt_id, fencing_token, reason, "
        "wait_timeout_ms, received_at_us, dispatched_at_us "
        "FROM gateway_abort_intents WHERE submit_intent_id=%s",
        (intent.identity.submit_intent_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("duplicate abort receipt")
    row = rows[0]
    identity = intent.identity
    if (row["submit_intent_id"], row["operation_id"], row["attempt_id"], row["fencing_token"]) != (
        identity.submit_intent_id,
        identity.operation_id,
        identity.attempt_id,
        identity.fencing_token,
    ):
        raise ValueError("abort receipt identity mismatch")
    request = AbortExecutionRequest(
        submit_intent_id=row["submit_intent_id"],
        operation_id=row["operation_id"],
        attempt_id=row["attempt_id"],
        fencing_token=row["fencing_token"],
        reason=row["reason"],
        wait_timeout_ms=row["wait_timeout_ms"],
    )
    digest = command_digest(
        {
            "submit_intent_id": request.submit_intent_id,
            "operation_id": request.operation_id,
            "attempt_id": request.attempt_id,
            "fencing_token": request.fencing_token,
            "reason": request.reason,
            "wait_timeout_ms": request.wait_timeout_ms,
        }
    )
    received = _instant(row["received_at_us"])
    claimed = None if row["dispatched_at_us"] is None else _instant(row["dispatched_at_us"])
    return AbortReceipt(digest, received, "received" if claimed is None else "claimed", claimed)


class PostgresExecutionReader:
    """Owns no pool lifecycle and cannot dispatch, change, or lock an execution."""

    def __init__(
        self,
        pool: GatewayPool,
        *,
        timeout_seconds: float = 3,
        expected_target: ExecutionReaderTarget | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("execution reader requires a bounded timeout")
        if expected_target is not None and not isinstance(expected_target, ExecutionReaderTarget):
            raise ValueError("execution reader requires a verified target")
        self._pool = pool
        self._timeout_seconds = timeout_seconds
        self._expected_target = expected_target

    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
        deadline = time.monotonic() + self._timeout_seconds
        try:
            with self._pool.connection(timeout=self._timeout_seconds) as connection:
                if time.monotonic() >= deadline:
                    raise _BudgetExpiredError
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                with connection.cursor() as raw:
                    cursor = _DeadlineCursor(cast(SubmitCursor, raw), deadline)
                    if self._expected_target is not None and (
                        read_execution_reader_target(
                            cursor, self._expected_target.schema_name
                        ).target
                        != self._expected_target
                    ):
                        raise ValueError("execution reader target changed")
                    intent = read_submit_intent(cursor, submit_intent_id)
                    if intent is None:
                        result = None
                    else:
                        if intent.identity.submit_intent_id != submit_intent_id:
                            raise ValueError("unexpected submit identity")
                        document_state = read_original_document_state(cursor, intent)
                        receipt = _read_receipt(cursor, intent)
                        result = ExecutionSnapshot(
                            intent, AbortRecovery(intent.request_digest, document_state, receipt)
                        )
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
            return result
        except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError, psycopg.Error:
            # No write was attempted. A failed read says nothing about a prior abort.
            raise SubmissionStoreUnavailableError("execution recovery is unavailable") from None
        except (
            SubmissionStoreError,
            InvalidExecutionRequestError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
        ):
            raise SubmissionStoreError("execution recovery cannot be verified") from None
