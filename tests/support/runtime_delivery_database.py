"""InnoDB test plumbing around the unchanged production delivery SQL."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import pymysql

from speechnexus_backend.speech.domain.runtime_delivery import (
    DeliveryClaim,
    DeliveryClaimRequest,
    DeliveryStart,
    DeliveryTicket,
    DeliveryUnavailableError,
    DeliveryWrite,
)
from speechnexus_backend.speech.infrastructure.runtime_delivery_sql import (
    DeliveryCursor,
    acknowledge_in_transaction,
    claim_in_transaction,
    defer_in_transaction,
    start_in_transaction,
)
from tests.support.job_database import MySQLJobTransactions


@dataclass(frozen=True)
class MySQLRuntimeDelivery:
    driver: MySQLJobTransactions = field(repr=False)

    def _run[T](self, operation: Callable[[DeliveryCursor, Callable[[], int]], T]) -> T:
        try:
            with self.driver.transaction() as raw:
                cursor = cast(DeliveryCursor, raw)

                def clock() -> int:
                    cursor.execute(
                        "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) "
                        "* 1000000 AS SIGNED) AS now_us"
                    )
                    return cast(int, cursor.fetchall()[0]["now_us"])

                return operation(cursor, clock)
        except pymysql.Error, ValueError, TypeError, KeyError:
            raise DeliveryUnavailableError from None

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
