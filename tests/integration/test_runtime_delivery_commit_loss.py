"""Actual committed PG/InnoDB writes lose their replies; never retry unknowns."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pymysql
import pytest
from tests.integration.test_runtime_delivery import CLAIM, DeliveryDatabase
from tests.integration.test_runtime_delivery import database as database
from tests.integration.test_runtime_delivery import delivery as delivery
from tests.integration.test_runtime_delivery import foundation as foundation
from tests.integration.test_runtime_delivery import runtime_database as runtime_database
from tests.integration.test_runtime_observations import _wire
from tests.support.job_database import MySQLJobTransactions
from tests.support.runtime_delivery_database import MySQLRuntimeDelivery

from speechnexus_backend.speech.application.runtime_delivery import RuntimeDeliveryTransactions
from speechnexus_backend.speech.domain.runtime_delivery import DeliveryUnavailableError
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.runtime_delivery_postgres import (
    PostgresRuntimeDelivery,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stage", ["claim", "start", "ack"])
def test_actual_commit_reply_loss_preserves_the_phase_without_automatic_reexecution(
    delivery: DeliveryDatabase,
    stage: str,
) -> None:
    calls = []
    if delivery.pool is not None:
        base = delivery.pool

        class FaultPool:
            @contextmanager
            def connection(self, **kwargs: Any) -> Iterator[Any]:
                with base.connection(**kwargs) as connection:
                    yield connection
                calls.append("committed")
                raise psycopg.OperationalError("Synthetic lost commit reply")

        faulty: RuntimeDeliveryTransactions = PostgresRuntimeDelivery(cast(SpeechPool, FaultPool()))
    else:
        assert isinstance(delivery.port, MySQLRuntimeDelivery)
        driver = delivery.port.driver

        class FaultDriver:
            @contextmanager
            def transaction(self) -> Iterator[Any]:
                with driver.transaction() as cursor:
                    yield cursor
                calls.append("committed")
                raise pymysql.OperationalError(2013, "Synthetic lost commit reply")

        faulty = MySQLRuntimeDelivery(cast(MySQLJobTransactions, FaultDriver()))
    before = delivery.physical()
    if stage == "claim":
        with pytest.raises(DeliveryUnavailableError):
            faulty.claim(CLAIM)
    elif stage == "start":
        with pytest.raises(DeliveryUnavailableError):
            faulty.start(delivery.ticket())
    else:
        started = delivery.port.start(delivery.ticket())
        assert started.ticket is not None
        import json

        with pytest.raises(DeliveryUnavailableError):
            faulty.acknowledge(started.ticket, json.dumps(_wire(delivery.runtime)))
    assert calls == ["committed"]  # Neither adapter retries an ambiguous commit.
    db = delivery.runtime.foundation.database
    record = db.query("SELECT * FROM runtime_intent_deliveries")[0]
    outbox = db.query("SELECT * FROM coordination_outbox WHERE event_kind='runtime'")[0]
    if stage == "ack":
        assert record["delivery_phase"] == "acknowledged"
        assert record["acknowledged_at_us"] == outbox["delivered_at_us"]
        assert delivery.port.claim(CLAIM).outcome == "empty"
    else:
        assert record["delivery_phase"] == ("unsent" if stage == "claim" else "unknown")
        assert outbox["delivered_at_us"] is None
        db.query("UPDATE runtime_intent_deliveries SET claim_expires_at_us=1")
        recovered = delivery.ticket()
        assert recovered.phase == ("unsent" if stage == "claim" else "unknown")
        if stage == "start":
            assert delivery.port.start(recovered).outcome == "stale"
    assert delivery.physical() == before


def test_failure_between_the_two_ack_writes_rolls_back_the_outbox_and_ledger(
    delivery: DeliveryDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from collections.abc import Callable

    from tests.support import runtime_delivery_database as mysql_adapter

    from speechnexus_backend.speech.domain.runtime_delivery import DeliveryTicket, DeliveryWrite
    from speechnexus_backend.speech.infrastructure import runtime_delivery_postgres as pg_adapter
    from speechnexus_backend.speech.infrastructure.runtime_delivery_sql import (
        DeliveryCursor,
        acknowledge_in_transaction,
    )

    started = delivery.port.start(delivery.ticket())
    assert started.ticket is not None
    owned_ticket = started.ticket
    physical = delivery.physical()
    attempts: list[str] = []

    class FaultCursor:
        def __init__(self, raw: DeliveryCursor) -> None:
            self.raw = raw

        @property
        def rowcount(self) -> int:
            return self.raw.rowcount

        def fetchall(self) -> list[dict[str, Any]]:
            return self.raw.fetchall()

        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
            if query.startswith(
                "UPDATE runtime_intent_deliveries SET delivery_phase='acknowledged'"
            ):
                self.raw.execute(
                    "SELECT delivered_at_us FROM coordination_outbox WHERE event_id=%s",
                    (owned_ticket.event_id,),
                )
                assert self.raw.fetchall()[0]["delivered_at_us"] is not None
                attempts.append("outbox_written_before_failure")
                raise ValueError("Synthetic failure before private acknowledgement")
            return self.raw.execute(query, parameters)

    def fail_ack(
        cursor: DeliveryCursor,
        ticket: DeliveryTicket,
        original_response: str,
        *,
        clock: Callable[[], int],
    ) -> DeliveryWrite:
        return acknowledge_in_transaction(
            FaultCursor(cursor), ticket, original_response, clock=clock
        )

    monkeypatch.setattr(pg_adapter, "acknowledge_in_transaction", fail_ack)
    monkeypatch.setattr(mysql_adapter, "acknowledge_in_transaction", fail_ack)
    with pytest.raises(DeliveryUnavailableError):
        delivery.port.acknowledge(started.ticket, json.dumps(_wire(delivery.runtime)))
    assert attempts == ["outbox_written_before_failure"]
    db = delivery.runtime.foundation.database
    row = db.query("SELECT * FROM runtime_intent_deliveries")[0]
    event = db.query("SELECT * FROM coordination_outbox WHERE event_kind='runtime'")[0]
    assert event["delivered_at_us"] is None
    assert row["delivery_phase"] == "unknown" and row["acknowledged_at_us"] is None
    assert delivery.physical() == physical
