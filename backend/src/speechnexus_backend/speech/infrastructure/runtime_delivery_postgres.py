"""Bounded private transport transactions; an uncertain commit is never replayed."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.runtime_delivery import (
    DeliveryClaim,
    DeliveryClaimRequest,
    DeliveryStart,
    DeliveryTicket,
    DeliveryUnavailableError,
    DeliveryWrite,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import (
    _BudgetExpiredError,
    _DeadlineCursor,
)
from speechnexus_backend.speech.infrastructure.leases_sql import LeaseCursor
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.runtime_delivery_sql import (
    DeliveryCursor,
    acknowledge_in_transaction,
    claim_in_transaction,
    defer_in_transaction,
    start_in_transaction,
)


class PostgresRuntimeDelivery:
    def __init__(
        self, pool: SpeechPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 30
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("Delivery storage requires bounded deadlines and rollback retries")
        self._pool, self._timeout, self._attempts = pool, timeout_seconds, max_attempts

    def _run[T](self, operation: Callable[[DeliveryCursor, Callable[[], int]], T]) -> T:
        deadline = time.monotonic() + self._timeout
        for attempt in range(self._attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
                    with connection.cursor() as raw:
                        cursor = _DeadlineCursor(cast(LeaseCursor, raw), deadline)
                        result = operation(cursor, cursor.clock)
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                return result  # Commit acknowledgement precedes any network permission.
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                # PostgreSQL guarantees rollback for these failures. An unknown
                # connection/commit result never enters this retry branch.
                if attempt + 1 < self._attempts:
                    delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                    if delay:
                        time.sleep(delay)
            except (
                psycopg.Error,
                PoolClosed,
                PoolTimeout,
                TooManyRequests,
                _BudgetExpiredError,
                ValueError,
                TypeError,
                KeyError,
            ):
                raise DeliveryUnavailableError from None
        raise DeliveryUnavailableError

    def claim(self, request: DeliveryClaimRequest) -> DeliveryClaim:
        return self._run(lambda cursor, clock: claim_in_transaction(cursor, request, clock=clock))

    def start(self, ticket: DeliveryTicket) -> DeliveryStart:
        return self._run(lambda cursor, clock: start_in_transaction(cursor, ticket, clock=clock))

    def acknowledge(self, ticket: DeliveryTicket, original_response: str) -> DeliveryWrite:
        return self._run(
            lambda cursor, clock: acknowledge_in_transaction(
                cursor, ticket, original_response, clock=clock
            )
        )

    def defer(self, ticket: DeliveryTicket, failure_code: str, delay_us: int) -> DeliveryWrite:
        return self._run(
            lambda cursor, clock: defer_in_transaction(
                cursor, ticket, failure_code, delay_us, clock=clock
            )
        )
