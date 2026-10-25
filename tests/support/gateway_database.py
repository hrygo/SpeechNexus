"""InnoDB behavior oracle for the gateway's ordinary SQL; no product MySQL adapter."""

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

import pymysql

from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    CommandRedactionResult,
)
from speechnexus_gateway.domain.fencing import BarrierSnapshot, IngressFence
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
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.barrier_sql import (
    read_barrier,
    seal_barrier_in_transaction,
)
from speechnexus_gateway.infrastructure.command_redaction_sql import (
    redact_execution_command_in_transaction,
)
from speechnexus_gateway.infrastructure.observation_sql import (
    record_execution_observation_in_transaction,
)
from speechnexus_gateway.infrastructure.original_command_sql import read_original_request
from speechnexus_gateway.infrastructure.output_interval_sql import (
    append_interval_in_transaction,
    read_output_interval,
)
from speechnexus_gateway.infrastructure.recovery_sql import mark_execution_unknown_in_transaction
from speechnexus_gateway.infrastructure.stop_sql import record_stop_in_transaction
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    check_file_start_in_transaction,
    create_submit_in_transaction,
    from_unix_micros,
    list_quarantined_intents,
    list_unfinished_intents,
    read_submit_intent,
)
from tests.integration.test_coordination_foundation import Foundation

_Result = TypeVar("_Result")


class MySQLSubmissionLedger:
    """Run the same gateway statements and pure rules as PG, with test-only plumbing."""

    def __init__(self, foundation: Foundation) -> None:
        self._foundation = foundation

    def _run(self, work: Callable[[SubmitCursor], _Result], *, read_only: bool = False) -> _Result:
        with self._foundation.connection("gateway") as connection:
            connection.autocommit(False)
            try:
                with connection.cursor() as raw:
                    if read_only:
                        raw.execute("SET TRANSACTION READ ONLY")
                    result = work(cast(SubmitCursor, raw))
                connection.commit()
                return result
            except pymysql.IntegrityError as error:
                connection.rollback()
                if error.args[0] == 1062:
                    raise StopEvidenceConflictError("evidence identity already used") from error
                raise SubmissionStoreError("test InnoDB transaction failed") from error
            except pymysql.OperationalError as error:
                connection.rollback()
                if error.args[0] == 3819:  # A CHECK rejected the statement; rollback is known.
                    raise SubmissionStoreError("test InnoDB constraint rejected stop") from error
                raise
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _clock(cursor: SubmitCursor) -> datetime:
        cursor.execute(
            "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS now_us"
        )
        return from_unix_micros(cursor.fetchall()[0]["now_us"])

    def get(self, submit_intent_id: str) -> SubmissionIntent | None:
        return self._run(lambda cursor: read_submit_intent(cursor, submit_intent_id))

    def read_request(self, intent: SubmissionIntent, *, max_document_bytes: int) -> SubmitRequest:
        return self._run(
            lambda cursor: read_original_request(
                cursor, intent, max_document_bytes=max_document_bytes
            ),
            read_only=True,
        )

    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
        intent = self.get(submit_intent_id)
        return None if intent is None else ExecutionSnapshot(intent)

    def list_unfinished(
        self, *, gateway_id: str, host_node_id: str, limit: int
    ) -> list[SubmissionIntent]:
        return self._run(
            lambda cursor: list_unfinished_intents(
                cursor, gateway_id=gateway_id, host_node_id=host_node_id, limit=limit
            ),
            read_only=True,
        )

    def check_file_start(self, intent: SubmissionIntent) -> SubmissionIntent:
        return self._run(
            lambda cursor: check_file_start_in_transaction(
                cursor, intent, clock=lambda: self._clock(cursor)
            )
        )

    def list_quarantined(
        self,
        *,
        gateway_id: str,
        host_node_id: str,
        limit: int,
        after_submit_intent_id: str | None = None,
    ) -> list[SubmissionIntent]:
        return self._run(
            lambda cursor: list_quarantined_intents(
                cursor,
                gateway_id=gateway_id,
                host_node_id=host_node_id,
                limit=limit,
                after_submit_intent_id=after_submit_intent_id,
            ),
            read_only=True,
        )

    def mark_execution_unknown(self, request: MarkExecutionUnknown) -> SubmissionIntent:
        return self._run(
            lambda cursor: mark_execution_unknown_in_transaction(
                cursor, request, clock=lambda: self._clock(cursor)
            )
        )

    def add(self, intent: SubmissionIntent, *, request_document: str) -> None:
        self._run(
            lambda cursor: create_submit_in_transaction(
                cursor, intent, request_document, clock=lambda: self._clock(cursor)
            )
        )

    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        return self._run(
            lambda cursor: record_execution_observation_in_transaction(
                cursor, request, clock=lambda: self._clock(cursor)
            )
        )

    def append_interval(self, record: OutputIntervalRecord) -> OutputIntervalWrite:
        try:
            return self._run(
                lambda cursor: append_interval_in_transaction(
                    cursor, record, clock=lambda: self._clock(cursor)
                )
            )
        except StopEvidenceConflictError as error:
            raise OutputIntervalConflictError(
                "interval identity already has a durable fact"
            ) from error

    def query_output_interval(self, interval_id: str) -> OutputIntervalRecord | None:
        return self._run(lambda cursor: read_output_interval(cursor, interval_id), read_only=True)

    def record_stop(self, request: RecordStop) -> SubmissionIntent:
        return self._run(
            lambda cursor: record_stop_in_transaction(
                cursor, request, clock=lambda: self._clock(cursor)
            )
        )

    def seal_barrier(self, fence: IngressFence) -> BarrierSnapshot:
        return self._run(
            lambda cursor: seal_barrier_in_transaction(
                cursor, fence, clock=lambda: self._clock(cursor)
            )
        )

    def get_barrier(self, gateway_id: str, pool_id: str) -> BarrierSnapshot | None:
        return self._run(
            lambda cursor: read_barrier(
                cursor, gateway_id, pool_id, clock=lambda: self._clock(cursor)
            )
        )

    def redact_execution_command(
        self, permit: AuthorizedCommandRedaction
    ) -> CommandRedactionResult:
        return self._run(
            lambda cursor: redact_execution_command_in_transaction(
                cursor, permit, clock=lambda: self._clock(cursor)
            )
        )
