"""The permanent debt read is bounded by the debt, not by the cancellation history.

``/readyz`` polls the debt probe every 5 s, and the fifth read (#1008) deliberately
has no time window -- the debt it exists for is the one that outlived the window. So
its bound has to be structural: migration 0061's partial index over the ledger's
open rows (``settled_at_us IS NULL OR last_outcome = 'exhausted'``). Two deployments
differing only in how much settled cancellation history they accumulated must make
this read touch the same number of rows.

The seeded shape is 10 000 and 50 000 settled cancellations plus ten permanently
blocked ones, which is the whole point: the settled majority is what the partial
predicate excludes and what a whole-ledger scan would pay for. The plan assertions
are structural (which index serves the read, which relations are never sequentially
scanned, how many ledger rows the index returns); the timings and buffer counts are
printed so the numbers in the change's evidence come from the same run as the gate.
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
from tests.support.migrations import LEDGER_DEBT_INDEX

from speechnexus_backend.speech.infrastructure.readiness import (
    _LEFT_CANCELLING_DEBT,
    _PERMANENT_DEBT,
)

pytestmark = pytest.mark.integration

_DIGEST = "sha256:" + "d" * 64
_OLD_US = 10_000_000
#: The permanently blocked rows: the read's answer must not depend on the scale.
_OPEN = 10
#: Settled cancellations the deployment keeps forever -- the term the index excludes.
_SCALES = (10_000, 50_000)
_WINDOW_US = 86_400 * 1_000_000
_GRACE_US = 300 * 1_000_000


def _seed(conn: Connection[dict[str, Any]], *, settled: int) -> None:
    """A Job history, a settled cancellation history, and a handful of open debt rows."""
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
    # The settled history: Jobs that closed, fences that stay forever, and a settled
    # reconciliation row each. The fifth read must not pay for any of this.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), 'workspace-1', 'tts', 'tts-quality',"
        " %s, 'cancelled', 3, %s, %s FROM generate_series(1, %s) AS i",
        (_DIGEST, _OLD_US, _OLD_US, settled),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), %s, 'hist-' || i"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, settled),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts,"
        " next_check_at_us, last_outcome, settled_at_us, updated_at_us)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), 1, %s, 'settled', %s, %s"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, _OLD_US, _OLD_US, settled),
    )
    # The lease history those closed cancellations left behind: `admission_leases`
    # keeps every lease, released ones included, so it grows with the deployment too.
    # The read may not walk it either.
    conn.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id,"
        " binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class,"
        " request_digest, required_stage_ids)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), 'attempt-1', 'workspace-1', 'binding-1',"
        " 'manifest-1', %s, 'policy-1', 'realtime', %s, 'stage-1'"
        " FROM generate_series(1, %s) AS i",
        (_DIGEST, _DIGEST, settled),
    )
    conn.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id,"
        " host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token,"
        " reserved_slots, lease_state, deadline_at_us)"
        " SELECT 'lease-hist-' || lpad(i::text, 8, '0'), 'hist-' || lpad(i::text, 8, '0'),"
        " 'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1', 'gateway-1', 1, 1,"
        " 'released', %s FROM generate_series(1, %s) AS i",
        (_OLD_US, settled),
    )
    # The open debt: permanently held capacity, accepted long before the fourth read's
    # window -- the shape that has no other reader.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'held-' || lpad(i::text, 8, '0'), 'workspace-1', 'tts', 'tts-quality',"
        " %s, 'cancelled', 3, %s, %s FROM generate_series(1, %s) AS i",
        (_DIGEST, _OLD_US, _OLD_US, _OPEN),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'held-' || lpad(i::text, 8, '0'), %s, 'held-' || i"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, _OPEN),
    )
    # Nine exhausted (never claimable again) and one merely unsettled, which is what
    # the partial predicate's first disjunct exists for. No stop evidence row: an
    # unconfirmed stop is exactly the shape that leaves the subclass
    # ``execution-unknown``.
    conn.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts,"
        " next_check_at_us, last_outcome, settled_at_us, updated_at_us)"
        " SELECT 'held-' || lpad(i::text, 8, '0'), 8, 0,"
        " CASE WHEN i = %s THEN 'pending' ELSE 'exhausted' END, NULL, %s"
        " FROM generate_series(1, %s) AS i",
        (_OPEN, _OLD_US, _OPEN),
    )
    conn.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id,"
        " binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class,"
        " request_digest, required_stage_ids)"
        " SELECT 'held-' || lpad(i::text, 8, '0'), 'attempt-1', 'workspace-1', 'binding-1',"
        " 'manifest-1', %s, 'policy-1', 'realtime', %s, 'stage-1'"
        " FROM generate_series(1, %s) AS i",
        (_DIGEST, _DIGEST, _OPEN),
    )
    conn.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id,"
        " host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token,"
        " reserved_slots, lease_state, deadline_at_us)"
        " SELECT 'lease-held-' || lpad(i::text, 8, '0'), 'held-' || lpad(i::text, 8, '0'),"
        " 'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1', 'gateway-1', 1, 1,"
        " 'reserved', %s FROM generate_series(1, %s) AS i",
        (_OLD_US, _OPEN),
    )
    conn.execute(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id,"
        " command_digest, request_digest, request_document, deadline_at_us, recorded_at_us,"
        " updated_at_us, execution_state, cleanup_state, dispatched_at_us, engine_request_id)"
        " SELECT 'intent-held-' || lpad(i::text, 8, '0'),"
        " 'lease-held-' || lpad(i::text, 8, '0'), 'held-' || lpad(i::text, 8, '0'),"
        " 'attempt-1', %s, %s, '{}', %s, %s, %s, 'unknown_quarantined', 'quarantined',"
        " %s, 'engine-held-' || i FROM generate_series(1, %s) AS i",
        (_DIGEST, _DIGEST, _OLD_US, _OLD_US, _OLD_US, _OLD_US, _OPEN),
    )
    # The fourth read's own shape, inside its window, so the comparison measures both
    # reads doing their job rather than one of them finding nothing.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'left-' || lpad(i::text, 8, '0'), 'workspace-1', 'tts', 'tts-quality',"
        " %s, 'cancelled', 3, %s, %s FROM generate_series(1, 5) AS i",
        (_DIGEST, _OLD_US, _OLD_US),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'left-' || lpad(i::text, 8, '0'),"
        " CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - 3600000000,"
        " 'request-left-' || i FROM generate_series(1, 5) AS i"
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
    for table in (
        "speech_jobs",
        "admission_attempts",
        "admission_leases",
        "submit_intents",
        "speech_job_cancellation_fences",
        "speech_job_cancellation_reconciliations",
    ):
        conn.execute(f"ANALYZE {table}")


def _plan(
    conn: Connection[dict[str, Any]],
    statement: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    row = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
    ).fetchone()
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


def _index_rows(plan: dict[str, Any], index: str) -> int | None:
    rows = [
        int(node.get("Actual Rows", 0)) for node in _nodes(plan) if node.get("Index Name") == index
    ]
    return max(rows) if rows else None


@pytest.mark.parametrize("settled", _SCALES)
def test_the_permanent_read_is_bounded_by_the_open_debt(settled: int) -> None:
    """The partial index serves the read and returns only the open rows, at both scales."""
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _seed(query, settled=settled)

        now_us = query.execute(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        ).fetchone()
        assert now_us is not None
        permanent = _plan(
            query,
            _PERMANENT_DEBT,
            {"now_us": now_us["now_us"], "grace_us": _GRACE_US, "limit": 96},
        )
        bounded = _plan(
            query,
            _LEFT_CANCELLING_DEBT,
            {
                "now_us": now_us["now_us"],
                "window_us": _WINDOW_US,
                "grace_us": _GRACE_US,
                "limit": 96,
            },
        )

        print(
            f"\nsettled={settled}: permanent={permanent['Actual Total Time']:.3f} ms "
            f"({permanent.get('Shared Hit Blocks', 0)} hit + "
            f"{permanent.get('Shared Read Blocks', 0)} read blocks), "
            f"left_cancelling={bounded['Actual Total Time']:.3f} ms "
            f"({bounded.get('Shared Hit Blocks', 0)} hit + "
            f"{bounded.get('Shared Read Blocks', 0)} read blocks)"
        )

        # The whole point of migration 0061: the ledger is reached through its partial
        # index, and the settled majority is never read.
        assert [
            node for node in _nodes(permanent) if node.get("Index Name") == LEDGER_DEBT_INDEX
        ], f"the plan must read the ledger through {LEDGER_DEBT_INDEX}: {permanent}"
        assert _index_rows(permanent, LEDGER_DEBT_INDEX) == _OPEN
        # None of the four tables the read joins may be sequentially scanned: each
        # one grows with the deployment (released leases and settled ledger rows stay
        # forever), so a scan of any of them would put uptime back into the 5 s poll.
        for relation in (
            "speech_job_cancellation_reconciliations",
            "speech_job_cancellation_fences",
            "speech_jobs",
            "admission_leases",
        ):
            _assert_no_sequential_scan(permanent, relation)

        # The fourth read keeps its own bounded path (migration 0060) beside it.
        assert [
            node
            for node in _nodes(bounded)
            if node.get("Index Name") == "speech_job_cancellation_fence_age_scan"
        ], f"the bounded read must still use its age index: {bounded}"
        for relation in (
            "speech_job_cancellation_reconciliations",
            "speech_job_cancellation_fences",
            "speech_jobs",
        ):
            _assert_no_sequential_scan(bounded, relation)


def test_the_permanent_reads_cost_does_not_follow_the_settled_history() -> None:
    """Ten thousand and fifty thousand settled rows cost the read the same rows.

    Comparing the two scales in one test is what makes "the cost follows the debt"
    observable: a whole-ledger scan would return 10 000 rows at the first scale and
    50 000 at the second, so the ledger index's row count -- not a wall-clock budget
    -- is the assertion. The timings are printed for the change's evidence.
    """
    observations: list[dict[str, float]] = []
    for settled in _SCALES:
        with _isolated_schema(_url()) as (config, query):
            command.upgrade(config, "head")
            _seed(query, settled=settled)
            now_us = query.execute(
                "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
            ).fetchone()
            assert now_us is not None
            permanent = _plan(
                query,
                _PERMANENT_DEBT,
                {"now_us": now_us["now_us"], "grace_us": _GRACE_US, "limit": 96},
            )
            observations.append(
                {
                    "settled": settled,
                    "ms": float(permanent["Actual Total Time"]),
                    "blocks": float(
                        permanent.get("Shared Hit Blocks", 0)
                        + permanent.get("Shared Read Blocks", 0)
                    ),
                    "rows": float(_index_rows(permanent, LEDGER_DEBT_INDEX) or 0),
                }
            )
            assert _index_rows(permanent, LEDGER_DEBT_INDEX) == _OPEN
            _assert_no_sequential_scan(permanent, "speech_job_cancellation_reconciliations")

    print(f"\nscaling: {observations}")
    small, large = observations
    # Rows the read touches are identical; that is the property the index buys.
    assert small["rows"] == large["rows"] == _OPEN
    # Blocks are the deterministic cost signal: a 5x larger settled history may not
    # cost 5x the work. The generous bound keeps this a regression guard rather than
    # a timer.
    assert large["blocks"] <= max(4 * small["blocks"], small["blocks"] + 64)
