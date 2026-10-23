"""InnoDB behavior oracle for the gateway's ordinary SQL; no product MySQL adapter."""

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

import pymysql

from speechnexus_gateway.domain.fencing import BarrierSnapshot, IngressFence
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    RecordStop,
    StopEvidenceConflictError,
    SubmissionIntent,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.barrier_sql import seal_barrier_in_transaction
from speechnexus_gateway.infrastructure.observation_sql import (
    record_execution_observation_in_transaction,
)
from speechnexus_gateway.infrastructure.recovery_sql import mark_execution_unknown_in_transaction
from speechnexus_gateway.infrastructure.stop_sql import record_stop_in_transaction
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    create_submit_in_transaction,
    from_unix_micros,
    read_submit_intent,
)
from tests.integration.test_coordination_foundation import Foundation

_Result = TypeVar("_Result")


class MySQLSubmissionLedger:
    """Run the same gateway statements and pure rules as PG, with test-only plumbing."""

    def __init__(self, foundation: Foundation) -> None:
        self._foundation = foundation

    def _run(self, work: Callable[[SubmitCursor], _Result]) -> _Result:
        with self._foundation.connection("gateway") as connection:
            connection.autocommit(False)
            try:
                with connection.cursor() as raw:
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

    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
        intent = self.get(submit_intent_id)
        return None if intent is None else ExecutionSnapshot(intent)

    def mark_execution_unknown(self, request: MarkExecutionUnknown) -> SubmissionIntent:
        return self._run(
            lambda cursor: mark_execution_unknown_in_transaction(
                cursor, request, clock=lambda: self._clock(cursor)
            )
        )

    def add(self, intent: SubmissionIntent, *, request_document: str) -> None:
        self._run(lambda cursor: create_submit_in_transaction(cursor, intent, request_document))

    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        return self._run(
            lambda cursor: record_execution_observation_in_transaction(
                cursor, request, clock=lambda: self._clock(cursor)
            )
        )

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
