"""Both debt probes read the live set, never the Job or cancellation history.

``/readyz`` polls both probes every 5 s, so their cost must follow the debt rather than how
much unrelated history the deployment accumulated:

* ``stop_evidence_debt`` selects ``state = 'running'`` Jobs ordered by ``updated_at_us``;
* ``cancellation_debt`` reads ``speech_job_cancellation_fences``, which keeps one row per
  accepted cancellation for the deployment's whole life (nothing prunes it) and joins back
  to ``state = 'cancelling'`` Jobs.

Before ``0056`` added ``speech_jobs_live_state_scan`` neither predicate had an access path,
so the plans sampled these shapes:

* a held-lease backlog that makes a Job scan cheaper than a lease-driven nested loop:
  ``Seq Scan on speech_jobs``, measured at 37.8 ms on 1 000 000 Jobs with 20 000 held
  leases against 0.6 ms with 64;
* a long deployment: ``Seq Scan on speech_job_cancellation_fences`` plus the
  reconciliation table, measured at 43.6 ms with 200 000 historical cancellations against
  0.4 ms with 1 000.

The seeded shape below reproduces both at a size CI can afford, and asserts the two plan
properties that only the index gives: neither query sequentially scans the history it is
not asking about, and both reach ``speech_jobs`` through the live-state index. Dropping the
index makes every assertion here fail, which is what makes this the counter-example for the
migration.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest
from alembic import command
from psycopg import Connection
from tests.integration.test_public_job_progress_migration_pg import (
    _isolated_schema,
    _url,
)

from speechnexus_backend.speech.infrastructure.readiness import (
    _DEBT,
    _LEFT_CANCELLING_DEBT,
    _STOP_EVIDENCE_DEBT,
    _VERIFIED_STOP_PARAMETERS,
    LEFT_CANCELLING_HOLDING_CAPACITY,
)

pytestmark = pytest.mark.integration

_INDEX = "speech_jobs_live_state_scan"
_AGE_INDEX = "speech_job_cancellation_fence_age_scan"
_DIGEST = "sha256:" + "d" * 64
# A held-lease backlog large enough that the pre-index plan stops driving from the leases,
# over a Job table large enough for the sequential scan to dominate it.
_JOBS = 20_000
_HELD = 2_000
# Settled cancellations: the perpetual history ``cancellation_debt`` used to walk per poll.
_SETTLED = 20_000
_OLD_US = 10_000_000


def _seed(conn: Connection[dict[str, Any]]) -> None:
    """Synthetic shape: a Job history, a held-lease backlog, and a cancellation history."""
    conn.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest)"
        " VALUES ('host-1', 'unified', 'ready', %s)",
        (_DIGEST,),
    )
    conn.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state)"
        " VALUES ('pool-1', 'realtime', 'ready')"
    )
    conn.execute(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, policy_revision)"
        " VALUES ('workspace-1', 1000000, 'policy-1')"
    )
    conn.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id,"
        " gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode,"
        " loading_policy, config_digest, instance_state, max_slots)"
        " VALUES ('instance-1', 1, 'host-1', 'pool-1', 'gateway-1', 'binding-1', 'manifest-1',"
        " 'template-1', 'managed', 'on_demand', %s, 'ready', 128)",
        (_DIGEST,),
    )
    # Terminal Jobs: the read will keep them out of both queries.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, result_json, error_code, created_at_us, updated_at_us)"
        " SELECT 'job-' || lpad(i::text, 8, '0'), 'workspace-1', 'asr', 'asr-model',"
        " %s, CASE WHEN i %% 2 = 0 THEN 'succeeded' ELSE 'failed' END, 2,"
        " CASE WHEN i %% 2 = 0 THEN '{}' END, CASE WHEN i %% 2 = 1 THEN 'engine_failure' END,"
        " %s, %s FROM generate_series(1, %s) AS i",
        (_DIGEST, _OLD_US, _OLD_US, _JOBS),
    )
    # The live set: ``_HELD`` running Jobs holding their lease, 30 released cancellations.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'live-' || lpad(i::text, 8, '0'), 'workspace-1', 'asr', 'asr-model', %s,"
        " CASE WHEN i < %s THEN 'running' ELSE 'cancelling' END, 2, %s, %s"
        " FROM generate_series(0, %s + 29) AS i",
        (_DIGEST, _HELD, _OLD_US - 1, _OLD_US, _HELD),
    )
    conn.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id,"
        " binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class,"
        " request_digest, required_stage_ids)"
        " SELECT j.job_id, 'attempt-1', 'workspace-1', 'binding-1', 'manifest-1', %s,"
        " 'policy-1', 'realtime', %s, 'stage-1' FROM speech_jobs AS j WHERE j.state <> 'failed'"
        " AND j.state <> 'succeeded'",
        (_DIGEST, _DIGEST),
    )
    conn.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id,"
        " host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token,"
        " reserved_slots, lease_state, deadline_at_us)"
        " SELECT 'lease-' || j.job_id, j.job_id, 'attempt-1', 'stage-1', 'host-1', 'instance-1',"
        " 1, 'pool-1', 'gateway-1', 1, 1,"
        " CASE WHEN j.state = 'running' THEN 'reserved' ELSE 'released' END, %s"
        " FROM speech_jobs AS j WHERE j.state <> 'failed' AND j.state <> 'succeeded'",
        (_OLD_US,),
    )
    conn.execute(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id,"
        " command_digest, request_digest, request_document, deadline_at_us, recorded_at_us,"
        " updated_at_us, execution_state, cleanup_state, dispatched_at_us, engine_request_id)"
        " SELECT 'intent-' || j.job_id, 'lease-' || j.job_id, j.job_id, 'attempt-1', %s, %s, '{}',"
        " %s, %s, %s, CASE WHEN j.state = 'running' THEN 'failed' ELSE 'cancelled' END, 'pending',"
        " CASE WHEN j.state = 'running' THEN %s::bigint END, 'engine-' || j.job_id"
        " FROM speech_jobs AS j WHERE j.state <> 'failed' AND j.state <> 'succeeded'",
        (_DIGEST, _DIGEST, _OLD_US, _OLD_US, _OLD_US, _OLD_US),
    )
    # Verified stop for every held running Job except the first: that one is the debt row.
    conn.execute(
        "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind,"
        " evidence_digest, observed_at_us, execution_stopped, resources_released)"
        " SELECT 'evidence-' || j.job_id, 'intent-' || j.job_id, 'engine_terminal_response', %s,"
        " %s, 1, 1 FROM speech_jobs AS j WHERE j.state = 'running' AND j.job_id <> 'live-00000000'",
        (_DIGEST, _OLD_US),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT j.job_id, %s, 'request-' || j.job_id FROM speech_jobs AS j"
        " WHERE j.state = 'cancelling'",
        (_OLD_US,),
    )
    # Settled cancellations the deployment keeps forever: one fence plus one settled
    # reconciliation each, and the Job itself is already `cancelled`.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), 'workspace-1', 'asr', 'asr-model', %s,"
        " 'cancelled', 3, %s, %s FROM generate_series(1, %s) AS i",
        (_DIGEST, _OLD_US, _OLD_US, _SETTLED),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), %s, 'hist-' || i"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, _SETTLED),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts,"
        " next_check_at_us, last_outcome, settled_at_us, updated_at_us)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), 1, %s, 'settled', %s, %s"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, _OLD_US, _OLD_US, _SETTLED),
    )
    # Jobs that already left ``cancelling`` while their lease still holds capacity:
    # the fourth read's shape, with an acceptance inside its window and no ledger row.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, error_code, created_at_us, updated_at_us)"
        " SELECT 'left-' || lpad(i::text, 8, '0'), 'workspace-1', 'asr', 'asr-model', %s,"
        " 'cancelled', 3, NULL, %s, %s FROM generate_series(1, 5) AS i",
        (_DIGEST, _OLD_US, _OLD_US),
    )
    conn.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id,"
        " binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class,"
        " request_digest, required_stage_ids)"
        " SELECT 'left-' || lpad(i::text, 8, '0'), 'attempt-1', 'workspace-1', 'binding-1',"
        " 'manifest-1', %s, 'policy-1', 'realtime', %s, 'stage-1'"
        " FROM generate_series(1, 5) AS i",
        (_DIGEST, _DIGEST),
    )
    conn.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id,"
        " host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token,"
        " reserved_slots, lease_state, deadline_at_us)"
        " SELECT 'lease-left-' || lpad(i::text, 8, '0'), 'left-' || lpad(i::text, 8, '0'),"
        " 'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1', 'gateway-1', 1, 1,"
        " 'reserved', %s FROM generate_series(1, 5) AS i",
        (_OLD_US,),
    )
    conn.execute(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id,"
        " command_digest, request_digest, request_document, deadline_at_us, recorded_at_us,"
        " updated_at_us, execution_state, cleanup_state, dispatched_at_us, engine_request_id)"
        " SELECT 'intent-left-' || lpad(i::text, 8, '0'),"
        " 'lease-left-' || lpad(i::text, 8, '0'), 'left-' || lpad(i::text, 8, '0'),"
        " 'attempt-1', %s, %s, '{}', %s, %s, %s, 'unknown_quarantined', 'quarantined',"
        " %s, 'engine-left-' || i FROM generate_series(1, 5) AS i",
        (_DIGEST, _DIGEST, _OLD_US, _OLD_US, _OLD_US, _OLD_US),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'left-' || lpad(i::text, 8, '0'),"
        " CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - 3600000000,"
        " 'request-left-' || i FROM generate_series(1, 5) AS i"
    )
    for table in (
        "speech_jobs",
        "admission_leases",
        "submit_intents",
        "execution_stop_evidence",
        "speech_job_cancellation_fences",
        "speech_job_cancellation_reconciliations",
    ):
        conn.execute(f"ANALYZE {table}")


def _plan(
    conn: Connection[dict[str, Any]],
    statement: str,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # Every debt read's parameter set: the two reads here need different subsets, and
    # psycopg ignores the keys a statement does not name.
    values: dict[str, Any] = {
        "grace_us": 300 * 1_000_000,
        "limit": 96,
        "debt_class": LEFT_CANCELLING_HOLDING_CAPACITY,
        **_VERIFIED_STOP_PARAMETERS,
    }
    if parameters is not None:
        values.update(parameters)
    row = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, values).fetchone()
    assert row is not None
    plan: dict[str, Any] = row["QUERY PLAN"][0]["Plan"]
    return plan


def _nodes(plan: dict[str, Any]) -> Iterable[dict[str, Any]]:
    pending = [plan]
    while pending:
        node = pending.pop()
        pending.extend(node.get("Plans", []))
        yield node


def _scans(plan: dict[str, Any], relation: str) -> list[dict[str, Any]]:
    return [node for node in _nodes(plan) if node.get("Relation Name") == relation]


def _assert_no_sequential_scan(plan: dict[str, Any], relation: str) -> None:
    sequential = [
        node["Node Type"]
        for node in _scans(plan, relation)
        if node["Node Type"] in ("Seq Scan", "Parallel Seq Scan")
    ]
    assert not sequential, f"{relation} must not be sequentially scanned: {sequential}"


def _assert_index_serves(plan: dict[str, Any]) -> None:
    assert [node for node in _nodes(plan) if node.get("Index Name") == _INDEX], (
        f"the plan must reach speech_jobs through {_INDEX}"
    )


def _assert_age_index_serves(plan: dict[str, Any]) -> None:
    assert [node for node in _nodes(plan) if node.get("Index Name") == _AGE_INDEX], (
        f"the plan must read the fence age through {_AGE_INDEX}"
    )


def test_debt_probes_read_the_live_set_not_the_job_or_cancellation_history() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _seed(query)

        stop_plan = _plan(query, _STOP_EVIDENCE_DEBT)
        # The held-lease backlog made a Job scan cheaper than the lease-driven join; with
        # the live-state index the scan reads only the running Jobs.
        _assert_no_sequential_scan(stop_plan, "speech_jobs")
        _assert_index_serves(stop_plan)

        cancel_plan = _plan(query, _DEBT)
        # The fence table is the deployment's whole cancellation history: walking it per
        # poll is the unbounded term this index removes.
        _assert_no_sequential_scan(cancel_plan, "speech_job_cancellation_fences")
        _assert_no_sequential_scan(cancel_plan, "speech_job_cancellation_reconciliations")
        _assert_index_serves(cancel_plan)

        now_us = query.execute(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        ).fetchone()
        assert now_us is not None
        left_plan = _plan(
            query,
            _LEFT_CANCELLING_DEBT,
            {"now_us": now_us["now_us"], "window_us": 86_400 * 1_000_000},
        )
        # The fourth read has no live-state access path: migration 0060's acceptance-age
        # index is what keeps its window off the 20 000-row settled history and off the
        # Job table. Dropping that index turns the assertions below red.
        _assert_no_sequential_scan(left_plan, "speech_job_cancellation_fences")
        _assert_no_sequential_scan(left_plan, "speech_job_cancellation_reconciliations")
        _assert_no_sequential_scan(left_plan, "speech_jobs")
        _assert_age_index_serves(left_plan)
