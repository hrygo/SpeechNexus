"""Real minimum-role PG/InnoDB delivery state; no runtime qualification claims."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from unittest.mock import AsyncMock

import psycopg
import pymysql
import pytest
from psycopg.conninfo import make_conninfo
from tests.fake.runtime_intents import COMMAND, REQUEST
from tests.integration.test_production_runtime_intents import RuntimeDatabase
from tests.integration.test_production_runtime_intents import database as database
from tests.integration.test_production_runtime_intents import foundation as foundation
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_runtime_observations import _wire
from tests.support.job_database import MySQLJobTransactions
from tests.support.runtime_delivery_database import MySQLRuntimeDelivery

from speechnexus_backend.speech.application.runtime_controller import RuntimeController
from speechnexus_backend.speech.application.runtime_delivery import (
    RuntimeDeliveryService,
    RuntimeDeliveryTransactions,
)
from speechnexus_backend.speech.domain.runtime_controller import ControllerObservation
from speechnexus_backend.speech.domain.runtime_delivery import DeliveryClaimRequest, DeliveryTicket
from speechnexus_backend.speech.domain.runtime_intents import CreateRuntimeIntent
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.runtime_codec import prepare_request
from speechnexus_backend.speech.infrastructure.runtime_delivery_postgres import (
    PostgresRuntimeDelivery,
)
from speechnexus_backend.speech.infrastructure.runtime_delivery_schema import (
    ADOPT_RUNTIME_DELIVERIES_SQL,
    runtime_delivery_grants,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import prepare_observation

pytestmark = pytest.mark.integration
CLAIM = DeliveryClaimRequest("delivery-test", ("host-test",), 30000000)


@dataclass
class DeliveryDatabase:
    runtime: RuntimeDatabase = field(repr=False)
    port: RuntimeDeliveryTransactions = field(repr=False)
    role: str
    pool: SpeechPool | None = field(repr=False)

    def ticket(self) -> DeliveryTicket:
        result = self.port.claim(CLAIM)
        assert result.outcome == "claimed" and result.ticket is not None
        return result.ticket

    def physical(self) -> dict[str, object]:
        return {
            k: v
            for k, v in self.runtime.snapshot().items()
            if k not in ("coordination_outbox", "runtime_intent_deliveries")
        }


@contextmanager
def prepared_delivery(
    runtime: RuntimeDatabase, command: CreateRuntimeIntent = COMMAND
) -> Iterator[DeliveryDatabase]:
    db = runtime.foundation.database
    role = db.schema + "_delivery"
    pool: SpeechPool | None = None
    if db.dialect == "postgresql":
        db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_delivery_test'")
    else:
        db.query(f"CREATE USER '{role}'@'%' IDENTIFIED BY 'synthetic_delivery_test'")
    try:
        for grant in runtime_delivery_grants(db.schema, role):
            if db.dialect == "mysql":
                if "USAGE ON SCHEMA" in grant:
                    continue
                grant = re.sub(r'TO "([^"]+)"', r"TO '\1'@'%'", grant).replace('"', "`")
            db.query(grant)
        assert runtime.port.create_runtime_intent(command).outcome == "committed"
        if db.dialect == "postgresql":
            pool = speech_pool(
                make_conninfo(db.url, user=role, password="synthetic_delivery_test"),
                schema=db.schema,
            )
            pool.open()
            port: RuntimeDeliveryTransactions = PostgresRuntimeDelivery(pool)
        else:
            port = MySQLRuntimeDelivery(
                MySQLJobTransactions(db.url, db.schema, role, "synthetic_delivery_test")
            )
        yield DeliveryDatabase(runtime, port, role, pool)
    finally:
        if pool is not None:
            pool.close()
        if db.dialect == "postgresql":
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')
        else:
            db.query(f"DROP USER '{role}'@'%'")


@pytest.fixture
def delivery(runtime_database: RuntimeDatabase) -> Iterator[DeliveryDatabase]:
    with prepared_delivery(runtime_database) as result:
        yield result


def test_before_wire_marker_and_ack_change_only_transport_ledger(
    delivery: DeliveryDatabase,
) -> None:
    before = delivery.physical()
    ticket = delivery.ticket()
    assert ticket.phase == "unsent" and ticket.original == REQUEST
    assert delivery.port.claim(CLAIM).outcome == "empty"
    started = delivery.port.start(ticket)
    assert started.outcome == "committed" and started.submit_allowed and started.ticket is not None
    assert started.ticket.phase == "unknown" and started.ticket.version > ticket.version
    assert delivery.port.start(ticket).outcome == "stale"
    response = json.dumps(_wire(delivery.runtime))  # Synthetic receipt, not qualified readiness.
    assert delivery.port.acknowledge(started.ticket, response).outcome == "committed"
    db = delivery.runtime.foundation.database
    row = db.query("SELECT * FROM runtime_intent_deliveries")[0]
    event = db.query("SELECT * FROM coordination_outbox WHERE event_kind='runtime'")[0]
    assert row["delivery_phase"] == "acknowledged"
    assert event["delivered_at_us"] == row["acknowledged_at_us"]
    assert (
        delivery.physical() == before
    )  # Ready-shaped transport never reduces peak or marks ready.


def test_expired_claim_recovery_is_query_only_and_old_holder_cannot_ack(
    delivery: DeliveryDatabase,
) -> None:
    ticket = delivery.ticket()
    started = delivery.port.start(ticket)
    assert started.ticket is not None
    db = delivery.runtime.foundation.database
    db.query("UPDATE runtime_intent_deliveries SET claim_expires_at_us=1")
    current = delivery.ticket()
    assert current.phase == "unknown" and current.version > started.ticket.version
    reply = json.dumps(_wire(delivery.runtime))
    assert delivery.port.acknowledge(started.ticket, reply).outcome == "stale"
    assert delivery.port.acknowledge(current, reply).outcome == "committed"


def test_mismatched_receipt_and_retry_never_reopen_submission(delivery: DeliveryDatabase) -> None:
    started = delivery.port.start(delivery.ticket())
    assert started.ticket is not None
    reply = _wire(delivery.runtime)
    reply["instance_id"] = "instance-other"
    assert delivery.port.acknowledge(started.ticket, json.dumps(reply)).outcome == "rejected"
    assert delivery.port.defer(started.ticket, "not_found", 100000).outcome == "committed"
    db = delivery.runtime.foundation.database
    row = db.query("SELECT * FROM runtime_intent_deliveries")[0]
    assert row["delivery_phase"] == "unknown" and row["acknowledged_at_us"] is None
    assert delivery.port.claim(CLAIM).outcome == "empty"
    db.query("UPDATE runtime_intent_deliveries SET next_attempt_at_us=0")
    assert delivery.ticket().phase == "unknown"


def test_legacy_adoption_never_grants_first_post(delivery: DeliveryDatabase) -> None:
    db = delivery.runtime.foundation.database
    # Exactly the pre-0008 data shape: committed request/outbox, no delivery row.
    db.query("DELETE FROM runtime_intent_deliveries")
    db.query(ADOPT_RUNTIME_DELIVERIES_SQL, (delivery.runtime.now(),))
    ticket = delivery.ticket()
    assert ticket.phase == "unknown"
    assert delivery.port.start(ticket).outcome == "stale"


def test_head_changed_before_dispatch_never_grants_a_post(delivery: DeliveryDatabase) -> None:
    db = delivery.runtime.foundation.database
    ticket = delivery.ticket()
    db.query("UPDATE runtime_instance_heads SET current_stop_intent_id='stop-committed'")
    result = delivery.port.start(ticket)
    assert result.outcome == "committed" and not result.submit_allowed
    assert result.ticket is not None and result.ticket.phase == "unknown"


def test_two_workers_compete_for_exactly_one_private_claim(delivery: DeliveryDatabase) -> None:
    before = delivery.physical()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: delivery.port.claim(CLAIM), range(2)))
    assert sorted(row.outcome for row in results) == ["claimed", "empty"]
    assert delivery.physical() == before


def test_ack_rechecks_expiration_after_the_actual_outbox_row_lock(
    delivery: DeliveryDatabase,
) -> None:
    claim = replace(CLAIM, lease_us=1000000)
    acquired = delivery.port.claim(claim)
    assert acquired.ticket is not None
    started = delivery.port.start(acquired.ticket)
    assert started.ticket is not None
    ticket = started.ticket
    db = delivery.runtime.foundation.database
    blocker = db.blocker()
    with blocker.cursor() as blocking_cursor:
        blocking_cursor.execute(
            "SELECT event_id FROM coordination_outbox WHERE event_id=%s FOR UPDATE",
            (ticket.event_id,),
        )
    before = delivery.physical()
    response = json.dumps(_wire(delivery.runtime))
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(delivery.port.acknowledge, ticket, response)
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if db.dialect == "postgresql":
                        waiting = db.query(
                            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                            "AND cardinality(pg_blocking_pids(pid))>0",
                            (delivery.role,),
                        )[0]["n"]
                    else:
                        waiting = db.query(
                            "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.data_locks l ON "
                            "w.REQUESTING_ENGINE_LOCK_ID=l.ENGINE_LOCK_ID WHERE l.OBJECT_SCHEMA=%s",
                            (db.schema,),
                        )[0]["n"]
                    if waiting:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("ACK did not encounter the real outbox row lock")
                while delivery.runtime.now() <= ticket.claim_expires_at_us:
                    time.sleep(0.01)
            finally:
                blocker.rollback()
            assert pending.result(timeout=5).outcome == "stale"
    finally:
        blocker.close()
    assert db.query("SELECT delivered_at_us FROM coordination_outbox")[0]["delivered_at_us"] is None
    assert (
        db.query("SELECT delivery_phase FROM runtime_intent_deliveries")[0]["delivery_phase"]
        == "unknown"
    )
    assert delivery.physical() == before


def test_dispatch_role_cannot_rewrite_commands_budgets_or_history(
    delivery: DeliveryDatabase,
) -> None:
    db = delivery.runtime.foundation.database
    statements = (
        "UPDATE host_nodes SET allocatable_unified_physical_memory_bytes=1",
        "UPDATE workspace_quotas SET max_attempts=1000",
        "UPDATE engine_instances SET base_budget_kind='released'",
        "UPDATE runtime_instance_intents SET request_document='{}'",
        "UPDATE coordination_outbox SET event_digest=event_digest",
        "DELETE FROM runtime_intent_deliveries",
        "UPDATE runtime_intent_deliveries SET event_id=event_id",
    )
    before = delivery.runtime.snapshot()
    if delivery.pool is not None:
        with delivery.pool.connection() as connection:
            for statement in statements:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    try:
                        connection.execute(statement)
                    finally:
                        connection.rollback()
    else:
        driver = MySQLJobTransactions(db.url, db.schema, delivery.role, "synthetic_delivery_test")
        mysql_connection = driver.connect()
        try:
            with mysql_connection.cursor() as cursor:
                for statement in statements:
                    with pytest.raises(pymysql.OperationalError) as error:
                        try:
                            cursor.execute(statement)
                        finally:
                            mysql_connection.rollback()
                    assert error.value.args[0] in (1142, 1143)
        finally:
            mysql_connection.close()
    assert delivery.runtime.snapshot() == before


@pytest.mark.parametrize("corruption", ["invalid_json", "changed_original"])
def test_corrupt_original_is_blocked_without_reconstructing_a_request_or_releasing_budget(
    delivery: DeliveryDatabase, corruption: str
) -> None:
    db = delivery.runtime.foundation.database
    before = delivery.physical()
    identity = (REQUEST.binding.target_node_id, REQUEST.intent_id)
    row = db.query(
        "SELECT request_document FROM runtime_instance_intents "
        "WHERE target_node_id=%s AND intent_id=%s",
        identity,
    )[0]
    document = row["request_document"]
    if corruption == "invalid_json":
        document = "{"
    else:
        value = json.loads(document)
        value["request"]["intent_id"] = "changed-original"
        document = json.dumps(value)
    db.query(
        "UPDATE runtime_instance_intents SET request_document=%s "
        "WHERE target_node_id=%s AND intent_id=%s",
        (document, *identity),
    )
    corrupt = delivery.physical()
    assert corrupt != before
    result = delivery.port.claim(CLAIM)
    assert result.outcome == "blocked" and result.ticket is None
    ledger = db.query("SELECT * FROM runtime_intent_deliveries")[0]
    assert ledger["delivery_phase"] == "blocked"
    assert ledger["last_failure_code"] == "invalid_record"
    assert ledger["claim_holder_id"] is None and ledger["submission_started_at_us"] is None
    assert db.query("SELECT delivered_at_us FROM coordination_outbox")[0]["delivered_at_us"] is None
    assert delivery.port.claim(CLAIM).outcome == "empty"
    assert delivery.physical() == corrupt


@pytest.mark.parametrize("phase", ["unsent", "unknown"])
async def test_deeply_nested_original_is_blocked_and_the_next_healthy_event_is_delivered(
    delivery: DeliveryDatabase, phase: str
) -> None:
    db = delivery.runtime.foundation.database
    healthy = replace(REQUEST, intent_id="runtime-healthy", instance_id="instance-healthy")
    assert (
        delivery.runtime.port.create_runtime_intent(replace(COMMAND, request=healthy)).outcome
        == "committed"
    )
    bad_event, good_event = prepare_request(REQUEST).event_id, prepare_request(healthy).event_id
    poison = "[" * 65535 + "0" + "]" * 65535
    assert len(poison.encode()) == 131071  # Within decode_record's existing byte limit.
    db.query(
        "UPDATE runtime_instance_intents SET request_document=%s "
        "WHERE target_node_id=%s AND intent_id=%s",
        (poison, REQUEST.binding.target_node_id, REQUEST.intent_id),
    )
    db.query(
        "UPDATE runtime_intent_deliveries SET delivery_phase=%s,next_attempt_at_us=0 "
        "WHERE event_id=%s",
        (phase, bad_event),
    )
    # Both are due, with the corrupt event deterministically selected first.
    db.query(
        "UPDATE runtime_intent_deliveries SET next_attempt_at_us=1 WHERE event_id=%s",
        (good_event,),
    )
    before = delivery.physical()
    response = prepare_observation(_wire(delivery.runtime, healthy))
    controller = AsyncMock(spec=RuntimeController)
    controller.submit.return_value = ControllerObservation(response.observation, response.document)
    service = RuntimeDeliveryService(
        delivery.port, {healthy.binding.target_node_id: controller}, retry_delay_us=100000
    )

    batch = await service.run_once(CLAIM, limit=3)

    assert batch.blocked == batch.claimed == batch.acknowledged == 1
    assert batch.unknown == batch.stale == 0
    controller.submit.assert_awaited_once_with(healthy)
    controller.query.assert_not_awaited()  # No POST or GET for the corrupt original.
    records = {row["event_id"]: row for row in db.query("SELECT * FROM runtime_intent_deliveries")}
    blocked = records[bad_event]
    assert blocked["delivery_phase"] == "blocked"
    assert blocked["last_failure_code"] == "invalid_record"
    assert blocked["claim_holder_id"] is None and blocked["claim_expires_at_us"] == 0
    assert blocked["submission_started_at_us"] is blocked["acknowledged_at_us"] is None
    assert records[good_event]["delivery_phase"] == "acknowledged"
    events = {
        row["event_id"]: row["delivered_at_us"]
        for row in db.query("SELECT event_id,delivered_at_us FROM coordination_outbox")
    }
    assert events[bad_event] is None and events[good_event] is not None
    assert delivery.physical() == before  # Includes the exact corrupt/healthy commands and budgets.
    again = await service.run_once(CLAIM, limit=3)
    assert again.claimed == again.blocked == 0
    controller.submit.assert_awaited_once_with(healthy)
    controller.query.assert_not_awaited()
