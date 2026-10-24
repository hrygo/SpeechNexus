"""PostgreSQL submit ledger: one short transaction per call, no engine call inside it.

Only a deadlock or serialization failure the database already rolled back is retried, at
most three times with jitter inside the original budget. A lost connection or commit
acknowledgment is reported as an unknown outcome, because repeating the write could
dispatch a second execution while quoting the same identity.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    CommandRedactionResult,
)
from speechnexus_gateway.domain.fencing import BarrierSnapshot, IngressFence
from speechnexus_gateway.domain.instance_drain import DrainInventory, InstanceIdentity
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import (
    OutputIntervalConflictError,
    OutputIntervalRecord,
    OutputIntervalWrite,
)
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    RecordStop,
    StopEvidenceConflictError,
    SubmissionIntent,
    SubmissionIntentExistsError,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmissionStoreUnavailableError,
)
from speechnexus_gateway.infrastructure.barrier_sql import (
    read_barrier,
    seal_barrier_in_transaction,
)
from speechnexus_gateway.infrastructure.command_redaction_sql import (
    redact_execution_command_in_transaction,
)
from speechnexus_gateway.infrastructure.drain_sql import (
    read_drain_inventory as read_drain_inventory_rows,
)
from speechnexus_gateway.infrastructure.drain_sql import (
    read_instance_identity as read_instance_identity_row,
)
from speechnexus_gateway.infrastructure.observation_sql import (
    record_execution_observation_in_transaction,
)
from speechnexus_gateway.infrastructure.output_interval_sql import (
    append_interval_in_transaction,
    read_output_interval,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.recovery_sql import mark_execution_unknown_in_transaction
from speechnexus_gateway.infrastructure.stop_sql import record_stop_in_transaction
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    create_submit_in_transaction,
    from_unix_micros,
    list_quarantined_intents,
    read_submit_intent,
)

_Result = TypeVar("_Result")


class _BudgetExpiredError(Exception):
    """Raised before commit; the connection context rolls the transaction back."""


class _DeadlineCursor:
    """Bounds every statement by the submit budget that is left."""

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

    def clock(self) -> datetime:
        self.execute(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        )
        return from_unix_micros(self.fetchall()[0]["now_us"])


class PostgresSubmissionLedger:
    """Implements the gateway's ``SubmissionLedger`` port on the frozen tables."""

    def __init__(
        self, pool: GatewayPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Submit transactions need bounded deadlines and retries")
        self._pool = pool
        self._deadline_seconds = timeout_seconds
        self._max_attempts = max_attempts

    def get(self, submit_intent_id: str) -> SubmissionIntent | None:
        return self._run(
            read_only=True,
            work=lambda cursor: read_submit_intent(cursor, submit_intent_id),
        )

    def list_quarantined(
        self, *, gateway_id: str, host_node_id: str, limit: int
    ) -> list[SubmissionIntent]:
        return self._run(
            read_only=True,
            work=lambda cursor: list_quarantined_intents(
                cursor, gateway_id=gateway_id, host_node_id=host_node_id, limit=limit
            ),
        )

    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
        intent = self.get(submit_intent_id)
        return None if intent is None else ExecutionSnapshot(intent)

    def mark_execution_unknown(self, request: MarkExecutionUnknown) -> SubmissionIntent:
        return self._run(
            read_only=False,
            work=lambda cursor: mark_execution_unknown_in_transaction(
                cursor, request, clock=cast(_DeadlineCursor, cursor).clock
            ),
        )

    def add(self, intent: SubmissionIntent, *, request_document: str) -> None:
        self._run(
            read_only=False,
            work=lambda cursor: create_submit_in_transaction(
                cursor,
                intent,
                request_document,
                clock=cast(_DeadlineCursor, cursor).clock,
            ),
        )

    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        return self._run(
            read_only=False,
            work=lambda cursor: record_execution_observation_in_transaction(
                cursor, request, clock=cast(_DeadlineCursor, cursor).clock
            ),
        )

    def append_interval(self, record: OutputIntervalRecord) -> OutputIntervalWrite:
        try:
            return self._run(
                read_only=False,
                work=lambda cursor: append_interval_in_transaction(
                    cursor, record, clock=cast(_DeadlineCursor, cursor).clock
                ),
            )
        except SubmissionIntentExistsError as error:
            raise OutputIntervalConflictError(
                "interval identity already has a durable fact"
            ) from error

    def query_output_interval(self, interval_id: str) -> OutputIntervalRecord | None:
        return self._run(
            read_only=True,
            work=lambda cursor: read_output_interval(cursor, interval_id),
        )

    def record_stop(self, request: RecordStop) -> SubmissionIntent:
        try:
            return self._run(
                read_only=False,
                work=lambda cursor: record_stop_in_transaction(
                    cursor, request, clock=cast(_DeadlineCursor, cursor).clock
                ),
            )
        except SubmissionIntentExistsError as error:
            raise StopEvidenceConflictError("stop evidence identity is already used") from error

    def redact_execution_command(
        self, permit: AuthorizedCommandRedaction
    ) -> CommandRedactionResult:
        return self._run(
            read_only=False,
            work=lambda cursor: redact_execution_command_in_transaction(
                cursor, permit, clock=cast(_DeadlineCursor, cursor).clock
            ),
        )

    def seal_barrier(self, fence: IngressFence) -> BarrierSnapshot:
        return self._run(
            read_only=False,
            work=lambda cursor: seal_barrier_in_transaction(
                cursor, fence, clock=cast(_DeadlineCursor, cursor).clock
            ),
        )

    def get_barrier(self, gateway_id: str, pool_id: str) -> BarrierSnapshot | None:
        return self._run(
            read_only=True,
            work=lambda cursor: read_barrier(
                cursor, gateway_id, pool_id, clock=cast(_DeadlineCursor, cursor).clock
            ),
        )

    def read_instance_identity(
        self, instance_id: str, instance_generation: int
    ) -> InstanceIdentity | None:
        return self._run(
            read_only=True,
            work=lambda cursor: read_instance_identity_row(
                cursor, instance_id, instance_generation
            ),
        )

    def read_drain_inventory(self, identity: InstanceIdentity) -> DrainInventory:
        return self._run(
            read_only=True,
            work=lambda cursor: read_drain_inventory_rows(cursor, identity),
        )

    def _run(self, *, read_only: bool, work: Callable[[SubmitCursor], _Result]) -> _Result:
        deadline = time.monotonic() + self._deadline_seconds
        exhausted: SubmissionStoreError | None = None
        for attempt in range(self._max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
                    if read_only:
                        connection.execute("SET TRANSACTION READ ONLY")
                    with connection.cursor() as raw:
                        cursor = _DeadlineCursor(cast(SubmitCursor, raw), deadline)
                        result = work(cursor)
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                return result  # Commit acknowledgment was received.
            except psycopg.errors.UniqueViolation as error:
                # The submit identity, or its lease, already owns a durable intent. Never
                # return it as if this call had created it: the caller queries instead.
                raise SubmissionIntentExistsError(
                    "the submit identity or its lease already has a durable intent"
                ) from error
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                # Confirmed abort only; no connection or transaction is held here.
                delay = min(random.uniform(0.001, 0.01), max(0.0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)
            except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
                exhausted = SubmissionStoreUnavailableError(
                    "the control database gave no bounded connection before the budget ran out"
                )
                break
            except (psycopg.OperationalError, psycopg.InterfaceError) as error:
                raise SubmissionOutcomeUnknownError(
                    "the submit transaction cannot prove whether it committed"
                ) from error
            except psycopg.Error as error:
                raise SubmissionStoreError(
                    f"the submit transaction failed: {type(error).__name__}"
                ) from error
        raise exhausted or SubmissionStoreUnavailableError(
            "the submit transaction used its retry budget without committing"
        )
