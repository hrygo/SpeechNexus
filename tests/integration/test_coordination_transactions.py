"""Same controlled-transaction behavior oracle on real isolated PG and InnoDB."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import psycopg
import pymysql
import pytest
from tests.support.coordination import (
    DIMENSIONS,
    Database,
    Recipe,
    RejectedError,
    Row,
    Transaction,
    counter,
    digest,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64
RECIPE = Recipe("binding-test", "manifest-test", {"stage-main": "tts-realtime"})


@pytest.fixture(params=["postgresql", "mysql"])
def database(request: pytest.FixtureRequest) -> Iterator[Database]:
    dialect: Literal["postgresql", "mysql"] = request.param
    variable = (
        "SPEECHNEXUS_TEST_DATABASE_URL" if dialect == "postgresql" else "SPEECHNEXUS_TEST_MYSQL_URL"
    )
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"set {variable} to a project-isolated database; not a qualification pass")
    namespace = "sn_coord_" + uuid.uuid4().hex[:12]
    database = Database(dialect, url, namespace)
    if dialect == "postgresql":
        admin: Any = psycopg.connect(url, autocommit=True)
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{namespace}"')
    else:
        parsed = urlparse(url)
        admin = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            autocommit=True,
        )
        with admin.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE `{namespace}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
            )
    try:
        source = (ROOT / "migrations/0001_cluster_coordination_draft.sql").read_text()
        ddl = "\n".join(line for line in source.splitlines() if not line.startswith("--"))
        if dialect == "mysql":
            # Type mapping only: ordinary MySQL TEXT cannot hold valid 1M-char commands.
            ddl = re.sub(r"\bTEXT\b", "LONGTEXT", ddl)
        with database.transaction() as tx:
            for statement in ddl.split(";"):
                if statement.strip():
                    tx.execute(statement)
        with database.transaction() as tx:
            _seed(tx)
        yield database
    finally:
        with admin.cursor() as cursor:
            if dialect == "postgresql":
                cursor.execute(f'DROP SCHEMA "{namespace}" CASCADE')
            else:
                cursor.execute(f"DROP DATABASE `{namespace}`")
        admin.close()


def _seed(tx: Transaction) -> None:
    tx.execute("""INSERT INTO resource_pools (pool_id, pool_state, service_class)
           VALUES ('tts-realtime', 'active', 'realtime')""")
    tx.execute(
        """INSERT INTO workspace_quotas (workspace_id, max_attempts, policy_revision)
           VALUES ('workspace-test', 10, 'policy-test')"""
    )
    tx.execute(
        """INSERT INTO host_nodes
           (host_node_id, memory_architecture, node_state, topology_digest)
           VALUES ('host-test', 'unified_memory', 'ready', %s)""",
        (DIGEST,),
    )
    for dimension in DIMENSIONS:
        capacity = 0 if dimension in ("device_memory_bytes", "host_memory_bytes") else 100
        tx.execute(
            f"UPDATE host_nodes SET allocatable_{dimension} = %s WHERE host_node_id = 'host-test'",
            (capacity,),
        )
    _instance(tx, "instance-test", "tts-realtime")


def _instance(tx: Transaction, name: str, pool: str) -> None:
    tx.execute(
        """INSERT INTO engine_instances
           (instance_id, instance_generation, host_node_id, pool_id, gateway_id,
            binding_revision_id, manifest_id, config_digest, instance_state,
            max_slots, ready_at_us, base_budget_kind, approved_template_id,
            management_mode, loading_policy)
           VALUES (%s, 1, 'host-test', %s, 'gateway-test', 'binding-test',
                   'manifest-test', %s, 'ready', 10, %s, 'steady',
                   'template-test', 'managed', 'resident')""",
        (name, pool, DIGEST, tx.now()),
    )
    tx.execute(
        """INSERT INTO runtime_instance_heads
           (instance_id, current_generation, generation_high_water) VALUES (%s, 1, 1)""",
        (name,),
    )
    for dimension in DIMENSIONS:
        amount = 0 if dimension in ("device_memory_bytes", "host_memory_bytes") else 10
        tx.execute(
            f"""UPDATE engine_instances SET per_slot_{dimension} = %s
                WHERE instance_id = %s""",
            (amount, name),
        )


def _owner(database: Database, pool: str = "tts-realtime", owner: str = "scheduler-test") -> Row:
    with database.transaction() as tx:
        version = tx.lock_pool(pool)["version"]
        token = tx.acquire(
            {"pool_id": pool, "owner_id": owner, "expected_version": str(version), "ttl_ms": 60000}
        )
    authority = {"pool_id": pool, "owner_id": owner, "fencing_token": token}
    with database.transaction() as tx:
        tx.seal(authority, "gateway-test", DIGEST)
    with database.transaction() as tx:
        tx.activate(authority)
    return authority


def _request(
    database: Database, authority: Row, suffix: str = "test", recipe: Recipe = RECIPE
) -> Row:
    with database.transaction() as tx:
        quota = tx.one("SELECT version FROM workspace_quotas WHERE workspace_id = 'workspace-test'")
        instances = tx.rows("SELECT * FROM engine_instances")
        now = tx.now()
    return {
        "identity": {"operation_id": f"operation-{suffix}", "attempt_id": f"attempt-{suffix}"},
        "workspace_id": "workspace-test",
        "binding_revision_id": recipe.binding_revision_id,
        "manifest_id": recipe.manifest_id,
        "recipe_digest": recipe.digest,
        "policy_revision_id": "policy-test",
        "expected_quota_version": str(quota["version"]),
        "service_class": "realtime",
        "authorities": [authority],
        "deadline_at_us": now + 60_000_000,
        "stages": [
            {
                "stage_id": stage_id,
                "lease_id": f"lease-{suffix}-{stage_id}",
                "pool_id": pool,
                "host_node_id": "host-test",
                "gateway_id": "gateway-test",
                "instance_id": next(i["instance_id"] for i in instances if i["pool_id"] == pool),
                "instance_generation": 1,
                "expected_instance_version": str(
                    next(i["version"] for i in instances if i["pool_id"] == pool)
                ),
                "reserved_slots": 1,
            }
            for stage_id, pool in recipe.stages.items()
        ],
        "job": {"job_id": f"job-{suffix}", "event_id": f"event-{suffix}", "event_digest": DIGEST},
    }


def _admit(database: Database) -> Row:
    authority = _owner(database)
    request = _request(database, authority)
    with database.transaction() as tx:
        assert tx.admit(request, RECIPE) == "committed"
    return request


def _submit_request(admission: Row) -> Row:
    command = json.loads((ROOT / "contracts/internal/execution-command.schema.json").read_text())[
        "examples"
    ][0]
    stage = admission["stages"][0]
    wire = {
        "submit_intent_id": "submit-test",
        "lease_id": stage["lease_id"],
        **admission["identity"],
        **{
            key: stage[key]
            for key in (
                "stage_id",
                "pool_id",
                "host_node_id",
                "gateway_id",
                "instance_id",
                "instance_generation",
            )
        },
        "workspace_id": admission["workspace_id"],
        "binding_revision_id": admission["binding_revision_id"],
        "policy_revision_id": admission["policy_revision_id"],
        "fencing_token": admission["authorities"][0]["fencing_token"],
        "command_digest": digest(command),
        "command": command,
        "deadline_at": (
            datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=admission["deadline_at_us"])
        ).isoformat(),
        "max_output_audio_ms": 1000,
        "sample_rate_hz": 24000,
    }
    return {"execution_request": wire, "output_stream_id": "output-test"}


def _submitted(database: Database) -> tuple[Row, Row]:
    admission = _admit(database)
    submit = _submit_request(admission)
    with database.transaction() as tx:
        assert tx.submit(submit) == "committed"
    return admission, submit


def _stop_request(tx: Transaction) -> Row:
    return {
        "evidence": {
            "evidence_id": "evidence-test",
            "submit_intent_id": "submit-test",
            "evidence_kind": "engine_abort_ack",
            "evidence_digest": DIGEST,
            "observed_at_us": tx.now(),
            "execution_stopped": True,
            "resources_released": True,
            "reason": None,
        },
        "terminal_state": "aborted",
        "error_code": None,
    }


def _interval(start: int, end: int, name: str = "interval-test") -> Row:
    return {
        "interval_id": name,
        "submit_intent_id": "submit-test",
        "output_stream_id": "output-test",
        "sample_rate_hz": 24000,
        "meter_kind": "generated",
        "sample_start": start,
        "sample_end": end,
    }


def test_actual_engine_and_foreign_keys(database: Database) -> None:
    with database.transaction() as tx:
        if database.dialect == "mysql":
            rows = tx.rows(
                """SELECT ENGINE FROM information_schema.tables
                   WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"""
            )
            assert len(rows) == 15
            assert all(row["ENGINE"] == "InnoDB" for row in rows)
            assert (
                tx.one("SELECT @@transaction_isolation AS isolation_level")["isolation_level"]
                == "READ-COMMITTED"
            )
        else:
            assert tx.one("SHOW transaction_isolation")["transaction_isolation"] == "read committed"
    with (
        pytest.raises((psycopg.errors.ForeignKeyViolation, pymysql.err.IntegrityError)),
        database.transaction() as tx,
    ):
        tx.execute(
            """INSERT INTO coordination_outbox (event_id, job_id, event_digest)
               VALUES ('event-orphan', 'job-absent', %s)""",
            (DIGEST,),
        )


def test_owner_reconciliation_and_expiry_do_not_resurrect(database: Database) -> None:
    authority = _owner(database)
    with database.transaction() as tx:
        tx.execute("UPDATE resource_pools SET owner_expires_at_us = 0")
    with pytest.raises(RejectedError, match="stale_fencing_token"), database.transaction() as tx:
        tx.renew({"authority": authority, "expected_version": "3", "ttl_ms": 60000})
    newer = _owner(database, owner="scheduler-new")
    assert newer["fencing_token"] > authority["fencing_token"]
    with pytest.raises(RejectedError, match="stale_fencing_token"), database.transaction() as tx:
        tx.seal(authority, "gateway-test", DIGEST)


def test_owner_cannot_activate_without_sealed_entry(database: Database) -> None:
    with database.transaction() as tx:
        token = tx.acquire(
            {
                "pool_id": "tts-realtime",
                "owner_id": "scheduler-test",
                "expected_version": "1",
                "ttl_ms": 60000,
            }
        )
    authority = {"pool_id": "tts-realtime", "owner_id": "scheduler-test", "fencing_token": token}
    with (
        pytest.raises(RejectedError, match="reconciliation_required"),
        database.transaction() as tx,
    ):
        tx.activate(authority)


def test_complete_recipe_atomicity_replay_and_changed_digest(database: Database) -> None:
    recipe = Recipe(
        "binding-test",
        "manifest-test",
        {"stage-main": "tts-realtime", "stage-codec": "tts-realtime"},
    )
    request = _request(database, _owner(database), recipe=recipe)
    with database.transaction() as tx:
        assert tx.admit(request, recipe) == "committed"
    with database.transaction() as tx:
        assert tx.admit(request, recipe) == "replayed"
        assert tx.one("SELECT held_slots FROM engine_instances")["held_slots"] == 2
        assert len(tx.rows("SELECT * FROM admission_leases")) == 2
        assert len(tx.rows("SELECT * FROM coordination_jobs")) == 1
        assert len(tx.rows("SELECT * FROM coordination_outbox")) == 1
    changed = copy.deepcopy(request)
    changed["stages"][0]["reserved_slots"] = 2
    with pytest.raises(RejectedError, match="idempotency_conflict"), database.transaction() as tx:
        tx.admit(changed, recipe)


def test_missing_stage_and_cross_generation_are_rejected(database: Database) -> None:
    request = _request(database, _owner(database))
    bad_recipe = Recipe(
        "binding-test", "manifest-test", {"stage-main": "tts-realtime", "stage-two": "tts-realtime"}
    )
    with pytest.raises(RejectedError, match="recipe_mismatch"), database.transaction() as tx:
        tx.admit(request, bad_recipe)
    request["stages"][0]["instance_generation"] = 2
    with pytest.raises(RejectedError, match="not_found"), database.transaction() as tx:
        tx.admit(request, RECIPE)


def test_mid_transaction_fault_leaves_no_partial_budget_job_or_outbox(database: Database) -> None:
    with database.transaction() as tx:
        tx.execute("""INSERT INTO resource_pools (pool_id, pool_state, service_class)
               VALUES ('asr-realtime', 'active', 'realtime')""")
        _instance(tx, "instance-other", "asr-realtime")
    first = _owner(database)
    second = _owner(database, "asr-realtime")
    recipe = Recipe(
        "binding-test",
        "manifest-test",
        {"stage-main": "tts-realtime", "stage-review": "asr-realtime"},
    )
    request = _request(database, first, recipe=recipe)
    request["authorities"] = [first, second]

    def fail() -> None:
        raise RuntimeError("injected process boundary before commit")

    with pytest.raises(RuntimeError, match="injected"), database.transaction() as tx:
        tx.admit(request, recipe, after_first_lease=fail)
    with database.transaction() as tx:
        for table in (
            "admission_attempts",
            "admission_leases",
            "coordination_jobs",
            "coordination_outbox",
        ):
            assert tx.rows(f"SELECT * FROM {table}") == []
        assert all(
            row["held_slots"] == 0 for row in tx.rows("SELECT held_slots FROM engine_instances")
        )
        assert tx.one("SELECT held_attempts FROM workspace_quotas")["held_attempts"] == 0
        assert (
            tx.one("SELECT held_unified_physical_memory_bytes AS held FROM host_nodes")["held"] == 0
        )


def test_commit_response_loss_queries_original_identity(database: Database) -> None:
    request = _request(database, _owner(database))
    with database.transaction() as tx:
        tx.admit(request, RECIPE)
    # The response is deliberately discarded; the caller cannot infer rollback.
    with database.transaction() as tx:
        assert tx.admit(request, RECIPE) == "replayed"
        assert len(tx.rows("SELECT * FROM admission_attempts")) == 1
        assert tx.one("SELECT held_slots FROM engine_instances")["held_slots"] == 1


def test_ttl_never_substitutes_for_stop_evidence(database: Database) -> None:
    admission, submit = _submitted(database)
    with database.transaction() as tx:
        tx.execute("UPDATE admission_leases SET deadline_at_us = 1")
        tx.execute("UPDATE resource_pools SET owner_expires_at_us = 1")
        assert tx.submit(submit) == "replayed"
    with (
        pytest.raises(RejectedError, match="execution_cleanup_pending"),
        database.transaction() as tx,
    ):
        tx.release(admission["stages"][0]["lease_id"])
    with database.transaction() as tx:
        assert tx.one("SELECT held_slots FROM engine_instances")["held_slots"] == 1
        stop = _stop_request(tx)
        tx.stop(stop)
    with database.transaction() as tx:
        assert tx.stop(stop) == "replayed"
    with database.transaction() as tx:
        assert tx.release(admission["stages"][0]["lease_id"]) == "committed"
    with database.transaction() as tx:
        assert tx.release(admission["stages"][0]["lease_id"]) == "replayed"
        assert tx.one("SELECT held_slots FROM engine_instances")["held_slots"] == 0
        assert tx.one("SELECT held_attempts FROM workspace_quotas")["held_attempts"] == 0


def test_changed_submit_command_and_terminal_reversal_are_rejected(database: Database) -> None:
    _, submit = _submitted(database)
    changed = copy.deepcopy(submit)
    changed["execution_request"]["command"]["text"] = "Changed synthetic text."
    changed["execution_request"]["command_digest"] = digest(changed["execution_request"]["command"])
    with pytest.raises(RejectedError, match="submit_intent_conflict"), database.transaction() as tx:
        tx.submit(changed)
    with database.transaction() as tx:
        stop = _stop_request(tx)
        tx.stop(stop)
    stop["terminal_state"] = "failed"
    stop["error_code"] = "cancellation_unconfirmed"
    with pytest.raises(RejectedError, match="submit_intent_conflict"), database.transaction() as tx:
        tx.stop(stop)


def test_text_only_stage_requires_no_pcm_stream(database: Database) -> None:
    with database.transaction() as tx:
        tx.execute(
            """INSERT INTO resource_pools (pool_id, pool_state, service_class)
               VALUES ('asr-batch', 'active', 'batch')"""
        )
        _instance(tx, "instance-asr", "asr-batch")
    recipe = Recipe("binding-test", "manifest-test", {"stage-main": "asr-batch"})
    admission = _request(database, _owner(database, "asr-batch"), recipe=recipe)
    admission["service_class"] = "batch"
    with database.transaction() as tx:
        tx.admit(admission, recipe)
    request = _submit_request(admission)
    body = json.loads((ROOT / "contracts/internal/execution-command.schema.json").read_text())[
        "examples"
    ][1]
    body["audio"]["storage"]["workspace_id"] = admission["workspace_id"]
    request["execution_request"].update(
        {
            "command": body,
            "command_digest": digest(body),
            "sample_rate_hz": None,
            "max_output_audio_ms": None,
        }
    )
    request["output_stream_id"] = None
    with database.transaction() as tx:
        assert tx.submit(request) == "committed"
        assert tx.rows("SELECT * FROM output_stream_heads") == []
        assert tx.one("SELECT sample_rate_hz FROM submit_intents")["sample_rate_hz"] is None
    with (
        pytest.raises(RejectedError, match="invalid_execution_request"),
        database.transaction() as tx,
    ):
        tx.append(_interval(0, 100))


def test_complete_canonical_request_survives_restart_and_large_text(database: Database) -> None:
    admission = _admit(database)
    request = _submit_request(admission)
    request["execution_request"]["command"]["text"] = "Synthetic 合成数据. " * 6000
    request["execution_request"]["command_digest"] = digest(request["execution_request"]["command"])
    with database.transaction() as tx:
        tx.submit(request)
    with database.transaction() as tx:  # new connection, not a cached command
        stored = tx.one("SELECT request_document, request_digest FROM submit_intents")
        assert len(stored["request_document"].encode("utf-8")) > 65535
        assert json.loads(stored["request_document"]) == request
        assert stored["request_digest"] == digest(request)
        assert tx.submit(request) == "replayed"


def _redaction_request(tx: Transaction) -> Row:
    version = tx.one("SELECT version FROM submit_intents WHERE submit_intent_id = 'submit-test'")[
        "version"
    ]
    return {
        "submit_intent_id": "submit-test",
        "expected_version": str(version),
        "deletion_job_id": "deletion-test",
        "deletion_evidence_digest": DIGEST,
    }


def test_private_command_cleanup_preserves_coordination_evidence(database: Database) -> None:
    admission, submit = _submitted(database)
    with database.transaction() as tx:
        request = _redaction_request(tx)
    with (
        pytest.raises(RejectedError, match="execution_cleanup_pending"),
        database.transaction() as tx,
    ):
        tx.redact(request)
    with database.transaction() as tx:
        tx.stop(_stop_request(tx))
    with database.transaction() as tx:
        tx.release(admission["stages"][0]["lease_id"])
    with database.transaction() as tx:
        request = _redaction_request(tx)
        assert tx.redact(request) == "committed"
    with database.transaction() as tx:
        assert tx.redact(request) == "replayed"
        assert tx.submit(submit) == "replayed"  # query-only; cannot authorize new dispatch
        stored = tx.one("SELECT * FROM submit_intents")
        assert stored["request_document"] is None
        assert stored["command_redacted_at_us"] is not None
        assert stored["request_digest"] == digest(submit)
        assert stored["command_digest"] == submit["execution_request"]["command_digest"]
        assert len(tx.rows("SELECT * FROM execution_stop_evidence")) == 1
        assert tx.one("SELECT lease_state FROM admission_leases")["lease_state"] == "released"


def test_interval_overlap_replay_identity_and_ceiling(database: Database) -> None:
    _submitted(database)
    with database.transaction() as tx:
        assert tx.append(_interval(0, 100)) == "committed"
        assert tx.append(_interval(0, 100)) == "replayed"
    for command, error in (
        (_interval(50, 150, "interval-overlap"), "output_overlap"),
        (_interval(24000, 24001, "interval-overflow"), "output_limit_exceeded"),
        (_interval(0, 101), "idempotency_conflict"),
        (
            {**_interval(100, 200, "interval-other"), "output_stream_id": "output-other"},
            "invalid_execution_request",
        ),
    ):
        with pytest.raises(RejectedError, match=error), database.transaction() as tx:
            tx.append(command)
    with database.transaction() as tx:
        assert tx.append(_interval(100, 200, "interval-next")) == "committed"
        assert (
            tx.one("SELECT generated_pcm_samples FROM submit_intents")["generated_pcm_samples"]
            == 200
        )


def test_signed_counter_overflow_is_rejected_before_sql(database: Database) -> None:
    request = _request(database, _owner(database))
    request["stages"][0]["reserved_slots"] = 2**62
    with pytest.raises(RejectedError, match="invalid_counter"), database.transaction() as tx:
        tx.admit(request, RECIPE)
    with pytest.raises(RejectedError, match="invalid_counter"):
        counter(True)


def test_parallel_workers_claim_once_without_redis(database: Database) -> None:
    _admit(database)
    barrier = threading.Barrier(2)

    def claim(worker: str) -> str:
        barrier.wait(timeout=5)
        try:
            with database.transaction() as tx:
                tx.claim(
                    {
                        "job_id": "job-test",
                        "worker_id": worker,
                        "expected_claim_version": 0,
                        "ttl_ms": 60000,
                    }
                )
            return "won"
        except RejectedError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ["worker-one", "worker-two"]))
    assert sorted(results) == ["version_conflict", "won"]
    with database.transaction() as tx:
        assert tx.one("SELECT claim_version FROM coordination_jobs")["claim_version"] == 1
        # Persistent outbox polling finds the work without any wakeup notification.
        assert len(tx.rows("SELECT * FROM coordination_outbox WHERE delivered_at_us IS NULL")) == 1


def _interleave(
    database: Database, first: Callable[[Transaction], Any], second: Callable[[Transaction], Any]
) -> str:
    """Observe a real server-side lock wait before allowing the first commit."""
    started = threading.Event()
    waiter_id: list[int] = []
    pid_sql = (
        "SELECT pg_backend_pid() AS pid"
        if database.dialect == "postgresql"
        else "SELECT CONNECTION_ID() AS pid"
    )

    def wait() -> str:
        try:
            with database.transaction() as tx:
                waiter_id.append(tx.one(pid_sql)["pid"])
                started.set()
                second(tx)
            return "committed"
        except RejectedError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.transaction() as tx:
            blocker = tx.one(pid_sql)["pid"]
            first(tx)
            future = executor.submit(wait)
            assert started.wait(5)
            deadline = time.monotonic() + 4
            while True:
                with database.transaction() as observer:
                    if database.dialect == "postgresql":
                        waiting = observer.one(
                            "SELECT %s = ANY(pg_blocking_pids(%s)) AS waiting",
                            (blocker, waiter_id[0]),
                        )["waiting"]
                    else:
                        waiting = observer.rows(
                            """SELECT w.REQUESTING_THREAD_ID
                               FROM performance_schema.data_lock_waits w
                               JOIN performance_schema.threads a
                               ON a.THREAD_ID = w.REQUESTING_THREAD_ID
                               JOIN performance_schema.threads b
                               ON b.THREAD_ID = w.BLOCKING_THREAD_ID
                               WHERE a.PROCESSLIST_ID = %s AND b.PROCESSLIST_ID = %s""",
                            (waiter_id[0], blocker),
                        )
                if waiting:
                    break
                assert not future.done(), "waiter finished without contending on the authority lock"
                assert time.monotonic() < deadline, "server never reported the expected lock wait"
                threading.Event().wait(0.01)
        return future.result(timeout=5)


@pytest.mark.parametrize("submit_first", [True, False])
def test_submit_release_both_lock_orders(database: Database, submit_first: bool) -> None:
    admission = _admit(database)
    request = _submit_request(admission)
    lease_id = admission["stages"][0]["lease_id"]

    def submit(tx: Transaction) -> str:
        return tx.submit(request)

    def release(tx: Transaction) -> str:
        return tx.release(lease_id)

    outcome = _interleave(
        database, submit if submit_first else release, release if submit_first else submit
    )
    assert outcome == ("execution_cleanup_pending" if submit_first else "lease_not_active")
    with database.transaction() as tx:
        assert tx.one("SELECT held_slots FROM engine_instances")["held_slots"] == int(submit_first)


@pytest.mark.parametrize("admit_first", [True, False])
def test_admission_drain_both_lock_orders(database: Database, admit_first: bool) -> None:
    request = _request(database, _owner(database))

    def drain(tx: Transaction) -> None:
        tx.lock_pool("tts-realtime")
        tx.one("SELECT * FROM host_nodes WHERE host_node_id = 'host-test' FOR UPDATE")
        tx.execute("UPDATE engine_instances SET instance_state = 'draining'")

    def admit(tx: Transaction) -> str:
        return tx.admit(request, RECIPE)

    outcome = _interleave(
        database, admit if admit_first else drain, drain if admit_first else admit
    )
    assert outcome == ("committed" if admit_first else "instance_generation_mismatch")


@pytest.mark.parametrize("submit_first", [True, False])
def test_takeover_submit_both_lock_orders(database: Database, submit_first: bool) -> None:
    admission = _admit(database)
    request = _submit_request(admission)

    def takeover(tx: Transaction) -> None:
        tx.lock_pool("tts-realtime")
        tx.execute("UPDATE resource_pools SET owner_expires_at_us = 0")
        version = tx.lock_pool("tts-realtime")["version"]
        tx.acquire(
            {
                "pool_id": "tts-realtime",
                "owner_id": "scheduler-new",
                "expected_version": str(version),
                "ttl_ms": 60000,
            }
        )

    def submit(tx: Transaction) -> str:
        return tx.submit(request)

    outcome = _interleave(
        database, submit if submit_first else takeover, takeover if submit_first else submit
    )
    assert outcome == ("committed" if submit_first else "stale_fencing_token")
    with database.transaction() as tx:
        assert tx.lock_pool("tts-realtime")["owner_state"] == "reconciling_takeover"
        assert len(tx.rows("SELECT * FROM submit_intents")) == int(submit_first)


def test_pool_owner_acquisition_serializes(database: Database) -> None:
    def acquire(tx: Transaction) -> int:
        return tx.acquire(
            {
                "pool_id": "tts-realtime",
                "owner_id": "scheduler-test",
                "expected_version": "1",
                "ttl_ms": 60000,
            }
        )

    assert _interleave(database, acquire, acquire) == "version_conflict"


def test_overlapping_appends_serialize_without_gist(database: Database) -> None:
    _submitted(database)
    assert (
        _interleave(
            database,
            lambda tx: tx.append(_interval(0, 100)),
            lambda tx: tx.append(_interval(50, 150, "interval-other")),
        )
        == "output_overlap"
    )


def test_different_submits_share_the_attempt_stream_overlap_guard(database: Database) -> None:
    recipe = Recipe(
        "binding-test",
        "manifest-test",
        {"stage-main": "tts-realtime", "stage-codec": "tts-realtime"},
    )
    admission = _request(database, _owner(database), recipe=recipe)
    with database.transaction() as tx:
        tx.admit(admission, recipe)
    first = _submit_request(admission)
    second = copy.deepcopy(first)
    second["execution_request"].update(
        {
            "submit_intent_id": "submit-other",
            "stage_id": "stage-codec",
            "lease_id": admission["stages"][1]["lease_id"],
        }
    )
    with database.transaction() as tx:
        tx.submit(first)
        tx.submit(second)
    command = {**_interval(50, 150, "interval-other"), "submit_intent_id": "submit-other"}
    assert (
        _interleave(
            database, lambda tx: tx.append(_interval(0, 100)), lambda tx: tx.append(command)
        )
        == "output_overlap"
    )
    with database.transaction() as tx:
        assert len(tx.rows("SELECT * FROM output_stream_heads")) == 1
        assert len(tx.rows("SELECT * FROM output_sample_intervals")) == 1
        command.update({"sample_start": 100, "sample_end": 200})
        assert tx.append(command) == "committed"


def test_unknown_execution_keeps_quarantined_budget_until_verified_stop(database: Database) -> None:
    admission, _ = _submitted(database)
    lease_id = admission["stages"][0]["lease_id"]
    with database.transaction() as tx:
        tx.execute("UPDATE host_nodes SET quarantine_threshold_ppm = 1")
    with database.transaction() as tx:
        tx.mark_unknown("submit-test")  # gateway owner transaction
    with database.transaction() as tx:
        tx.quarantine(lease_id)  # scheduler owner transaction
    with database.transaction() as tx:
        tx.quarantine(lease_id)
        assert tx.one("SELECT held_slots, quarantined_slots FROM engine_instances") == {
            "held_slots": 0,
            "quarantined_slots": 1,
        }
        quota = tx.one("SELECT * FROM workspace_quotas")
        assert (quota["held_attempts"], quota["quarantined_attempts"]) == (0, 1)
        assert tx.one("SELECT node_state FROM host_nodes")["node_state"] == "quarantined"
    with (
        pytest.raises(RejectedError, match="execution_cleanup_pending"),
        database.transaction() as tx,
    ):
        tx.release(lease_id)
    with database.transaction() as tx:
        tx.stop(_stop_request(tx))
    with database.transaction() as tx:
        tx.release(lease_id)
        assert tx.one("SELECT quarantined_slots FROM engine_instances")["quarantined_slots"] == 0
        assert tx.one("SELECT node_state FROM host_nodes")["node_state"] == "quarantined"
        assert (
            tx.one("SELECT quarantined_attempts FROM workspace_quotas")["quarantined_attempts"] == 0
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_pool_last_physical_budget_has_one_winner(database: Database, reverse: bool) -> None:
    with database.transaction() as tx:
        tx.execute(
            """INSERT INTO resource_pools (pool_id, pool_state, service_class)
               VALUES ('asr-realtime', 'active', 'realtime')"""
        )
        _instance(tx, "instance-other", "asr-realtime")
        tx.execute("UPDATE host_nodes SET allocatable_unified_physical_memory_bytes = 10")
        tx.execute(
            """INSERT INTO workspace_quotas (workspace_id, max_attempts, policy_revision)
               VALUES ('workspace-other', 10, 'policy-test')"""
        )
    owner_a = _owner(database)
    owner_b = _owner(database, "asr-realtime")
    other_recipe = Recipe("binding-test", "manifest-test", {"stage-main": "asr-realtime"})
    first = _request(database, owner_a, "first")
    second = _request(database, owner_b, "second", other_recipe)
    second["workspace_id"] = "workspace-other"  # separate quota/pool locks; same physical parent

    def admit_first(tx: Transaction) -> str:
        return tx.admit(first, RECIPE)

    def admit_second(tx: Transaction) -> str:
        return tx.admit(second, other_recipe)

    assert (
        _interleave(
            database,
            admit_second if reverse else admit_first,
            admit_first if reverse else admit_second,
        )
        == "capacity_exhausted"
    )
    with database.transaction() as tx:
        assert len(tx.rows("SELECT * FROM admission_leases")) == 1
        assert (
            tx.one("SELECT held_unified_physical_memory_bytes AS held FROM host_nodes")["held"]
            == 10
        )


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_each_physical_dimension_blocks_admission(database: Database, dimension: str) -> None:
    with database.transaction() as tx:
        if dimension in ("device_memory_bytes", "host_memory_bytes"):
            tx.execute("UPDATE host_nodes SET memory_architecture = 'discrete_device_memory'")
            tx.execute("UPDATE engine_instances SET per_slot_unified_physical_memory_bytes = 0")
            tx.execute(f"UPDATE engine_instances SET per_slot_{dimension} = 10")
        tx.execute(f"UPDATE host_nodes SET allocatable_{dimension} = 9")
    request = _request(database, _owner(database))
    with pytest.raises(RejectedError, match="capacity_exhausted"), database.transaction() as tx:
        tx.admit(request, RECIPE)
    with database.transaction() as tx:
        assert tx.rows("SELECT * FROM admission_attempts") == []


def test_quota_is_checked_inside_complete_admission(database: Database) -> None:
    with database.transaction() as tx:
        tx.execute("UPDATE workspace_quotas SET max_attempts = 0")
    request = _request(database, _owner(database))
    with pytest.raises(RejectedError, match="quota_exceeded"), database.transaction() as tx:
        tx.admit(request, RECIPE)


def test_batch_cannot_borrow_realtime_floor(database: Database) -> None:
    with database.transaction() as tx:
        tx.execute(
            """INSERT INTO resource_pools (pool_id, pool_state, service_class)
               VALUES ('tts-batch', 'active', 'batch')"""
        )
        _instance(tx, "instance-batch", "tts-batch")
        tx.execute(
            """UPDATE engine_instances SET max_slots = 1, realtime_reserved_slots = 1
               WHERE instance_id = 'instance-batch'"""
        )
    recipe = Recipe("binding-test", "manifest-test", {"stage-main": "tts-batch"})
    request = _request(database, _owner(database, "tts-batch"), recipe=recipe)
    request["service_class"] = "batch"
    with pytest.raises(RejectedError, match="capacity_exhausted"), database.transaction() as tx:
        tx.admit(request, recipe)


@contextmanager
def _runtime_role(database: Database, role: str) -> Iterator[Transaction]:
    if database.dialect == "postgresql":
        with database.transaction() as tx:
            tx.execute(f'SET LOCAL ROLE "{role}"')
            yield tx
    else:
        parsed = urlparse(database.url)
        url = parsed._replace(
            netloc=f"{role}:synthetic-test-only@{parsed.hostname}:{parsed.port or 3306}"
        ).geturl()
        with replace(database, url=url).transaction() as tx:
            yield tx


def test_runtime_grants_keep_evidence_append_only_and_owners_separate(database: Database) -> None:
    spec = importlib.util.spec_from_file_location(
        "coordination_grants", ROOT / "scripts/dev-env/coordination-grants.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    speech = "sn_speech_" + uuid.uuid4().hex[:8]
    gateway = "sn_gateway_" + uuid.uuid4().hex[:8]
    created: list[str] = []
    try:
        for role in (speech, gateway):
            with database.transaction() as tx:
                if database.dialect == "postgresql":
                    tx.execute(
                        f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER '
                        "NOCREATEDB NOCREATEROLE NOINHERIT"
                    )
                else:
                    tx.execute(f"CREATE USER '{role}'@'%' IDENTIFIED BY 'synthetic-test-only'")
            created.append(role)
        with database.transaction() as tx:
            for statement in module.statements(
                database.dialect, database.namespace, speech, gateway
            ):
                tx.execute(statement)
        admission = _request(database, _owner(database))
        with _runtime_role(database, speech) as tx:
            tx.admit(admission, RECIPE)
        with _runtime_role(database, gateway) as tx:
            tx.submit(_submit_request(admission))
            tx.mark_unknown("submit-test")
        with _runtime_role(database, speech) as tx:
            tx.quarantine(admission["stages"][0]["lease_id"])
        with _runtime_role(database, gateway) as tx:
            tx.stop(_stop_request(tx))
        with _runtime_role(database, speech) as tx:
            tx.release(admission["stages"][0]["lease_id"])
        with _runtime_role(database, gateway) as tx:
            tx.redact(_redaction_request(tx))
        for role, statement in (
            (gateway, "UPDATE execution_stop_evidence SET evidence_digest = evidence_digest"),
            (gateway, "DELETE FROM output_sample_intervals"),
            (speech, "UPDATE submit_intents SET execution_state = 'succeeded'"),
            (gateway, "UPDATE admission_leases SET lease_state = 'released'"),
            (speech, "TRUNCATE admission_attempts"),
            (speech, "UPDATE engine_instances SET per_slot_device_memory_bytes = 0"),
        ):
            with (
                pytest.raises(
                    (psycopg.errors.InsufficientPrivilege, pymysql.err.OperationalError)
                ) as caught,
                _runtime_role(database, role) as tx,
            ):
                tx.execute(statement)
            if database.dialect == "mysql":
                assert caught.value.args[0] in (1142, 1143)  # denied table/column privilege
            else:
                assert getattr(caught.value, "sqlstate", None) == "42501"
    finally:
        with database.transaction() as tx:
            for role in created:
                if database.dialect == "postgresql":
                    tx.execute(f'DROP OWNED BY "{role}"')
                    tx.execute(f'DROP ROLE "{role}"')
                else:
                    tx.execute(f"DROP USER '{role}'@'%'")
