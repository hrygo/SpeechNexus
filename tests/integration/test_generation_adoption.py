"""The audited adoption repairs a generation divergence in one transaction, or not at all.

#1282: the coordination ledger said generation 1 while the node controller had prepared
generation 11, so every readiness receipt attested a generation no pod served. The repair
is an operator-attested adoption: copy the approved plan to the node's generation, retire
the ledger's generation with the node's own testimony, and leave an audit row saying why.
These cases drive the same ``adopt_in_transaction`` on PostgreSQL and InnoDB, because the
transaction is written in the restricted SQL subset for exactly that reason.

What each case is evidence of:

* a successful adoption writes every row admission and the readiness report read, moves no
  host capacity (the same physical engine holds the same steady), and the written receipt
  is one admission would accept;
* every refusal writes **nothing** -- the proof is a full snapshot of the touched tables
  compared before and after, not the absence of an error message;
* two concurrent adoptions produce exactly one audit row: the second waits on the head row
  lock and then answers ``aligned``. The wait is observed in the engine's own lock views,
  not inferred from call ordering.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any, cast

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import (
    foundation as _foundation_fixture,
)
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as _database_fixture
from tests.support.job_database import MySQLJobTransactions
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.engine_registration import EngineProbe, RegistrationError
from speechnexus_backend.generation_adoption import (
    AdoptionRequest,
    AdoptionResult,
    adopt_in_transaction,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeBinding,
    RuntimeDeadline,
    RuntimeIntent,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_VALID,
    VerifiedRuntimeReadiness,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import encode_record, prepare_request
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_from_document,
    stored_readiness_verdict,
)
from speechnexus_portable_sql.clock import now_us as clock_now_us
from speechnexus_portable_sql.vendor import dict_row

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64
INSTANCE = "instance-test"
NODE = "host-test"
OPERATOR = "operator-test"
ZERO = PhysicalResources(0, 0, 0, 0, 0, 0)
BINDING = RuntimeBinding(
    NODE,
    "tts-realtime",
    "gateway-test",
    "template-test",
    "binding-test",
    "manifest-test",
    DIGEST,
)
#: Matches the ``engine_instances`` row the foundation fixture seeds (two slots, no
#: capacity), so the adoption copies this plan rather than inventing one.
PLAN = ApprovedRuntimePlan(
    BINDING,
    DIGEST,
    DIGEST,
    "unified_memory",
    "realtime",
    "on_demand",
    2,
    0,
    ZERO,
    ZERO,
    ZERO,
)
GENERATION_ONE_INTENT = "register-instance-test-g1"
ADOPTED_INTENT = "adopt-instance-test-g11"

#: Every relation the adoption can write, in a deterministic read order.
LIFECYCLE = {
    "engine_instances": "instance_id, instance_generation",
    "runtime_instance_heads": "instance_id",
    "runtime_instance_intents": "target_node_id, intent_id",
    "resource_pools": "pool_id",
    "coordination_outbox": "event_id",
    "runtime_intent_deliveries": "event_id",
    "runtime_generation_adoptions": "instance_id, adopted_generation",
    "host_nodes": "host_node_id",
}


def _snapshot(db: JobDatabase) -> dict[str, list[dict[str, Any]]]:
    return {
        table: db.query(f"SELECT * FROM {table} ORDER BY {order}")
        for table, order in LIFECYCLE.items()
    }


def _probe(observed_at_us: int) -> EngineProbe:
    return EngineProbe(
        url="http://127.0.0.1:9/health",
        status=200,
        content_type="application/json",
        body_sha256="sha256:" + "d" * 64,
        body_bytes=2,
        observed_at_us=observed_at_us,
    )


def _install_generation_one(db: JobDatabase) -> int:
    """Make the seeded ledger a decodable, plan-matched generation 1.

    The foundation fixture seeds a synthetic intent document because it only exercises
    constraints. The adoption copies the current generation's *plan*, so this replaces it
    with a real encoded prepare record for a plan that matches the seeded instance row.
    """
    db.query("DELETE FROM runtime_instance_intents")
    request = RuntimeIntent(
        GENERATION_ONE_INTENT,
        PLAN.binding,
        INSTANCE,
        1,
        "prepare",
        1,
        PLAN.memory,
        RuntimeDeadline("2250-01-01T00:00:00.123456789Z"),
    )
    prepared = prepare_request(request)
    db.query(
        "INSERT INTO runtime_instance_intents (target_node_id, intent_id, instance_id, "
        "instance_generation, action, request_digest, request_document, intent_state, "
        "recorded_at_us, updated_at_us) VALUES (%s, %s, %s, 1, 'prepare', %s, %s, "
        "'succeeded', 1, 1)",
        (
            NODE,
            GENERATION_ONE_INTENT,
            INSTANCE,
            prepared.request_digest,
            encode_record(prepared, PLAN, "register-engine"),
        ),
    )
    db.query(
        "UPDATE runtime_instance_heads SET current_prepare_intent_id = %s WHERE instance_id = %s",
        (GENERATION_ONE_INTENT, INSTANCE),
    )
    # A non-zero epoch, so "the node's token is behind the pool" is expressible: a
    # token of 0 is refused as an incomplete head, not as a spent epoch.
    db.query("UPDATE resource_pools SET fencing_token = 4, version = version + 1")
    return 4


def _mysql_now_us(cursor: Any) -> int:
    cursor.execute(
        "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS now_us"
    )
    return int(cursor.fetchall()[0]["now_us"])


def _run(
    db: JobDatabase,
    request: AdoptionRequest,
    *,
    probe: Callable[[], EngineProbe],
) -> AdoptionResult:
    if db.dialect == "postgresql":
        connection = portable_pg_connect(db.url, row_factory=dict_row)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{db.schema}", pg_catalog')
                result = adopt_in_transaction(
                    cast(Any, cursor), request, probe=probe, now_us=lambda: clock_now_us(cursor)
                )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    transaction = MySQLJobTransactions(db.url, db.schema)
    mysql_connection = transaction.connect()
    try:
        with mysql_connection.cursor() as cursor:
            result = adopt_in_transaction(
                cast(Any, cursor),
                request,
                probe=probe,
                now_us=lambda: _mysql_now_us(cast(Any, cursor)),
            )
        mysql_connection.commit()
        return result
    except BaseException:
        mysql_connection.rollback()
        raise
    finally:
        mysql_connection.close()


def _blocked_sessions(db: JobDatabase) -> int:
    if db.dialect == "postgresql":
        rows = db.query(
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE datname = current_database() AND cardinality(pg_blocking_pids(pid)) > 0"
        )
    else:
        rows = db.query(
            "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
            "JOIN performance_schema.data_locks l ON w.REQUESTING_ENGINE_LOCK_ID = "
            "l.ENGINE_LOCK_ID WHERE l.OBJECT_SCHEMA = %s",
            (db.schema,),
        )
    return int(rows[0]["n"])


def _await_a_real_lock_wait(db: JobDatabase) -> None:
    for _ in range(300):
        if _blocked_sessions(db) > 0:
            return
        time.sleep(0.01)
    pytest.fail("no blocked session was observed; the second adoption is not serialized")


def db_now(db: JobDatabase) -> int:
    """The database's clock: the same one the transaction reads."""
    if db.dialect == "postgresql":
        return int(
            db.query("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000000) AS n")[0]["n"]
        )
    return int(
        db.query("SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS n")[0][
            "n"
        ]
    )


def _head(pool_token: int, **overrides: Any) -> Any:
    from speechnexus_backend.controller_head import NodeInstanceHead

    values: dict[str, Any] = {
        "current_generation": 11,
        "generation_high_water": 11,
        "stopped_generations": (1,),
        "voided_generations": (),
        "last_fencing_token": pool_token + 1,
    }
    values.update(overrides)
    return NodeInstanceHead(INSTANCE, **values)


def _request(head: Any, *, accept: bool = False) -> AdoptionRequest:
    return AdoptionRequest(
        instance_id=INSTANCE,
        node_id=NODE,
        operator_id=OPERATOR,
        node_head=head,
        accept_missing_stop_evidence=accept,
    )


#: Re-exported under their own names so pytest registers them in this module: the
#: dual-engine database fixture and the seeded coordination ledger built on it.
database = _database_fixture
foundation = _foundation_fixture


def test_an_adoption_writes_a_usable_generation_without_moving_host_capacity(
    foundation: Foundation,
) -> None:
    db = foundation.database
    pool_before = _install_generation_one(db)
    host_before = db.query("SELECT * FROM host_nodes ORDER BY host_node_id")

    result = _run(db, _request(_head(pool_before)), probe=lambda: _probe(db_now(db)))

    assert result.outcome == "adopted"
    assert (result.ledger_generation, result.adopted_generation) == (1, 11)
    assert result.previous_stop_evidence == "node_reported_stopped"

    head = db.query("SELECT * FROM runtime_instance_heads")[0]
    assert head["current_generation"] == 11
    assert head["generation_high_water"] == 11
    assert head["current_prepare_intent_id"] == ADOPTED_INTENT
    assert head["current_stop_intent_id"] is None

    retired = db.query(
        "SELECT * FROM engine_instances WHERE instance_id = %s AND instance_generation = 1",
        (INSTANCE,),
    )[0]
    adopted = db.query(
        "SELECT * FROM engine_instances WHERE instance_id = %s AND instance_generation = 11",
        (INSTANCE,),
    )[0]
    assert (retired["instance_state"], retired["base_budget_kind"]) == ("stopped", "released")
    assert retired["stopped_at_us"] == result.adopted_at_us
    assert (adopted["instance_state"], adopted["base_budget_kind"]) == ("ready", "steady")
    assert adopted["ready_at_us"] == result.adopted_at_us
    assert adopted["last_fencing_token"] == pool_before + 1
    # The plan is copied, not re-derived: same slots and same capacity vectors.
    for column in ("max_slots", "realtime_reserved_slots", "config_digest", "loading_policy"):
        assert adopted[column] == retired[column], column

    intent = db.query(
        "SELECT * FROM runtime_instance_intents WHERE intent_id = %s", (ADOPTED_INTENT,)
    )[0]
    assert intent["intent_state"] == "succeeded"
    assert intent["observation_document"] is not None
    assert intent["readiness_document"] is not None
    # The outbox event exists and is not left for a node delivery that cannot happen.
    delivery = db.query("SELECT delivery_phase, last_failure_code FROM runtime_intent_deliveries")[
        0
    ]
    assert delivery == {"delivery_phase": "blocked", "last_failure_code": "operator_attested"}

    assert (
        db.query("SELECT fencing_token FROM resource_pools")[0]["fencing_token"] == pool_before + 1
    )
    assert db.query("SELECT * FROM host_nodes ORDER BY host_node_id") == host_before, (
        "the adoption moved host capacity; the running engine's hold must not change"
    )
    assert (
        db.query("SELECT count(*) AS n FROM engine_instances WHERE instance_state = 'ready'")[0][
            "n"
        ]
        == 1
    )

    audit = db.query("SELECT * FROM runtime_generation_adoptions")[0]
    assert audit["adopted_generation"] == 11
    assert audit["ledger_generation"] == 1
    assert audit["node_high_water"] == 11
    assert audit["node_last_fencing_token"] == pool_before + 1
    assert audit["previous_stop_evidence"] == "node_reported_stopped"
    assert audit["operator_id"] == OPERATOR
    assert audit["adopted_at_us"] == result.adopted_at_us
    document = json.loads(audit["document"])
    assert document["branch"] == "node_reported_stopped"
    assert document["node"]["stopped_generations"] == [1]
    assert document["ledger_before"]["current_generation"] == 1
    assert document["pool_epoch"] == {"before": pool_before, "after": pool_before + 1}
    assert document["health_probe"]["http_status"] == 200


def test_the_adopted_receipt_is_one_admission_would_accept(foundation: Foundation) -> None:
    """The repair is only a repair if the written receipt passes admission's own check."""
    db = foundation.database
    pool_before = _install_generation_one(db)
    _run(db, _request(_head(pool_before)), probe=lambda: _probe(db_now(db)))

    row = db.query(
        "SELECT i.readiness_document, i.request_document, e.instance_generation "
        "FROM runtime_instance_intents AS i JOIN engine_instances AS e "
        "ON e.instance_id = i.instance_id AND e.instance_generation = i.instance_generation "
        "WHERE i.intent_id = %s",
        (ADOPTED_INTENT,),
    )[0]
    from speechnexus_backend.speech.infrastructure.runtime_codec import decode_record

    prepared, plan, _creator = decode_record(row["request_document"])
    now = db_now(db)
    verdict = stored_readiness_verdict(
        row["readiness_document"],
        request=prepared.request,
        plan=plan,
        now_us=now,
        warning_us=0,
        freshness_us=86_400_000_000,
    )
    assert verdict.state == READINESS_VALID
    receipt = readiness_from_document(row["readiness_document"])
    assert isinstance(receipt, VerifiedRuntimeReadiness)
    assert receipt.instance_generation == 11
    assert receipt.fencing_token == pool_before + 1


@pytest.mark.parametrize(
    "case",
    ["no_testimony", "stale_fencing_token", "probe_failure"],
)
def test_a_refusal_leaves_every_relation_unchanged(foundation: Foundation, case: str) -> None:
    db = foundation.database
    pool_before = _install_generation_one(db)
    before = _snapshot(db)
    if case == "no_testimony":
        head = _head(pool_before, stopped_generations=(), voided_generations=())
        expected = "previous_generation_stop_unconfirmed"

        def probe() -> EngineProbe:
            return _probe(db_now(db))

    elif case == "stale_fencing_token":
        # The node's generation was prepared in an epoch the pool already left: two
        # writers in one epoch if adopted.
        head = _head(pool_before, last_fencing_token=pool_before - 1)
        expected = "stale_fencing_token"

        def probe() -> EngineProbe:
            return _probe(db_now(db))

    else:
        head = _head(pool_before)
        expected = "engine_unreachable"

        def probe() -> EngineProbe:
            raise RegistrationError("engine_unreachable", "the engine is unreachable")

    with pytest.raises(RegistrationError) as raised:
        _run(db, _request(head), probe=probe)

    assert raised.value.code == expected
    assert _snapshot(db) == before, "a refused adoption must not write anything"


def test_missing_testimony_can_be_accepted_and_is_recorded_as_the_weaker_claim(
    foundation: Foundation,
) -> None:
    db = foundation.database
    pool_before = _install_generation_one(db)
    head = _head(pool_before, stopped_generations=(), voided_generations=())

    result = _run(db, _request(head, accept=True), probe=lambda: _probe(db_now(db)))

    assert result.previous_stop_evidence == "missing"
    audit = db.query("SELECT previous_stop_evidence, document FROM runtime_generation_adoptions")[0]
    assert audit["previous_stop_evidence"] == "missing"
    assert json.loads(audit["document"])["accept_missing_stop_evidence"] is True


def test_an_aligned_pair_writes_nothing(foundation: Foundation) -> None:
    # The idempotent answer: a second adoption (or an instance the ledger already
    # follows) must not re-sign a receipt or write a second audit row.
    db = foundation.database
    _install_generation_one(db)
    before = _snapshot(db)
    head = _head(1, current_generation=1, generation_high_water=1)

    result = _run(db, _request(head), probe=lambda: _probe(db_now(db)))

    assert result.outcome == "aligned"
    assert _snapshot(db) == before


def test_two_concurrent_adoptions_commit_exactly_one(foundation: Foundation) -> None:
    db = foundation.database
    pool_before = _install_generation_one(db)
    released = threading.Event()
    entered = threading.Event()
    results: list[object] = []

    def first() -> None:
        def gated() -> EngineProbe:
            entered.set()
            assert released.wait(10), "the test never released the first adoption"
            return _probe(db_now(db))

        results.append(_run(db, _request(_head(pool_before)), probe=gated))

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(10), "the first adoption never reached its probe"

    second: list[object] = []

    def rival() -> None:
        # The loser must not adopt a plan it computed before the winner committed: the
        # head moved, so it refuses with a version conflict and writes nothing.
        try:
            second.append(_run(db, _request(_head(pool_before)), probe=lambda: _probe(db_now(db))))
        except RegistrationError as error:
            second.append(error)

    rival_worker = threading.Thread(target=rival)
    rival_worker.start()
    _await_a_real_lock_wait(db)
    released.set()
    worker.join(20)
    rival_worker.join(20)

    assert len(results) == 1 and len(second) == 1
    assert cast(AdoptionResult, results[0]).outcome == "adopted"
    loser = cast(RegistrationError, second[0])
    assert loser.code == "version_conflict"
    assert "re-run" in loser.message
    assert db.query("SELECT count(*) AS n FROM runtime_generation_adoptions")[0]["n"] == 1
    assert (
        db.query("SELECT count(*) AS n FROM engine_instances WHERE instance_generation = 11")[0][
            "n"
        ]
        == 1
    )
