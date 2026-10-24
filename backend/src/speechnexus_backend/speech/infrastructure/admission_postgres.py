"""Bounded PostgreSQL admission; ambiguous commit recovery is strictly read-only."""

from __future__ import annotations

import random
import time
from typing import cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRejectedError,
    AdmissionResult,
    AdmitRecipe,
    rejected,
    unknown,
)
from speechnexus_backend.speech.domain.runtime_intents import digest, integer
from speechnexus_backend.speech.infrastructure.admission_codec import (
    PreparedAdmission,
    prepare_admission,
)
from speechnexus_backend.speech.infrastructure.admission_recovery_sql import (
    AdmissionCursor,
    read_original,
)
from speechnexus_backend.speech.infrastructure.admission_sql import admit_in_transaction
from speechnexus_backend.speech.infrastructure.leases_postgres import (
    _BudgetExpiredError,
    _DeadlineCursor,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class PostgresAdmissionTransactions:
    def __init__(
        self,
        pool: SpeechPool,
        *,
        max_command_bytes: int,
        timeout_seconds: float = 3,
        max_attempts: int = 3,
    ) -> None:
        integer(max_command_bytes, minimum=1)
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Admission requires bounded transaction deadlines and retries")
        self._pool = pool
        self._max_bytes = max_command_bytes
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def admit_recipe(self, command: AdmitRecipe) -> AdmissionResult:
        identity = command.request.identity
        try:
            prepared = prepare_admission(command, max_command_bytes=self._max_bytes)
        except ValueError, TypeError, KeyError:
            return rejected(identity, "invalid_execution_request")
        return self._run(identity, prepared=prepared, deadline=time.monotonic() + self._timeout)

    def query_admission(
        self, identity: AdmissionIdentity, *, request_digest: str | None = None
    ) -> AdmissionResult:
        if request_digest is not None:
            try:
                digest(request_digest)
            except ValueError:
                return rejected(identity, "invalid_execution_request")
        return self._run(
            identity, request_digest=request_digest, deadline=time.monotonic() + self._timeout
        )

    def _run(
        self,
        identity: AdmissionIdentity,
        *,
        deadline: float,
        prepared: PreparedAdmission | None = None,
        request_digest: str | None = None,
        recover_prepared: PreparedAdmission | None = None,
        conflict_if_absent: bool = False,
    ) -> AdmissionResult:
        for attempt in range(self._max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready_to_commit = False
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
                    if prepared is None:
                        connection.execute("SET TRANSACTION READ ONLY")
                    with connection.cursor() as raw:
                        cursor = _DeadlineCursor(cast(AdmissionCursor, raw), deadline)
                        if prepared is not None:
                            result = admit_in_transaction(
                                cursor,
                                prepared,
                                clock=cursor.clock,
                                max_command_bytes=self._max_bytes,
                            )
                        else:
                            found = read_original(
                                cursor,
                                identity,
                                max_command_bytes=self._max_bytes,
                                request_digest=request_digest,
                                prepared=recover_prepared,
                            )
                            if found is None:
                                result = (
                                    rejected(identity, "idempotency_conflict")
                                    if conflict_if_absent
                                    else unknown(identity)
                                )
                            else:
                                result = found
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                    ready_to_commit = True
                return result  # Return success only after commit acknowledgement.
            except AdmissionRejectedError as error:
                return rejected(identity, error.code)
            except psycopg.errors.UniqueViolation:
                # The context confirmed rollback. The collision may be an
                # alternate global lease/Job/event key; never issue a new write.
                if prepared is None:
                    return rejected(identity, "reconciliation_required")
                return self._run(
                    identity,
                    deadline=deadline,
                    recover_prepared=prepared,
                    conflict_if_absent=True,
                )
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)  # Confirmed rollback, after returning the connection.
            except (
                PoolClosed,
                PoolTimeout,
                TooManyRequests,
                _BudgetExpiredError,
                psycopg.errors.QueryCanceled,
            ):
                if ready_to_commit:
                    return unknown(identity)
                break
            except psycopg.OperationalError, psycopg.InterfaceError:
                return unknown(identity)
            except psycopg.Error, ValueError, TypeError, KeyError:
                if ready_to_commit:
                    return unknown(identity)
                return rejected(identity, "reconciliation_required")
        # An exhausted read cannot prove that the original admission write rolled back.
        if prepared is None:
            return unknown(identity)
        return AdmissionResult(
            "rejected", identity, error_code="reconciliation_required", recovery="retry_transaction"
        )
