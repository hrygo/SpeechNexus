"""The permanent debt read is bounded by the held leases, not by any history.

``/readyz`` polls the debt probe every 5 s, and the fifth read (#1008) deliberately
has no lookback window -- the debt it exists for is the one that outlived the window.
Its bound therefore has to be structural: migration 0062's partial index over the
leases that still hold capacity. A lease that holds capacity is a live reservation, and
reservations cannot exceed the deployment's instance slots, so the read's cost is
bounded by capacity rather than by how much the deployment has accumulated.

Three histories are grown together, tenfold between the two scales, because each of them
is a real term in the alternatives and a seed that grows only one of them cannot tell the
designs apart:

* settled ledger rows (what a whole-ledger scan would pay for);
* ``exhausted`` ledger rows (what migration 0061's ledger index kept forever -- an
  ``exhausted`` row is never settled, so it is a permanent member of that partial index
  even after its lease was released);
* released leases (what a whole-lease scan would pay for).

The plan assertions are structural (which relations are never sequentially scanned, how many
rows each read returns, and that the permanent read's access path is migration 0062's index);
the timings and buffer counts are printed so the numbers in the change's evidence come from
the same run as the gate. The fourth read's assertions name the degradation it forbids rather
than the index it must pick: it may be answered from the fence window or from the held leases,
and a plan outside those two is not a failure if it stays bounded.
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
from tests.support.migrations import LEASE_DEBT_INDEX

from speechnexus_backend.speech.infrastructure.readiness import (
    _LEFT_CANCELLING_DEBT,
    _PERMANENT_DEBT,
)

pytestmark = pytest.mark.integration

_DIGEST = "sha256:" + "d" * 64
_OLD_US = 10_000_000
#: The debt itself: held leases whose acceptance no bounded read names any more.
#: 6 exhausted (never claimable), 2 unsettled but past the window, 2 with no ledger row
#: at all -- the three shapes the fifth read exists for.
_OPEN_EXHAUSTED = 6
_OPEN_UNSETTLED = 2
_OPEN_NO_LEDGER = 2
_OPEN = _OPEN_EXHAUSTED + _OPEN_UNSETTLED + _OPEN_NO_LEDGER
#: In-window rows: the fourth read's business, not the fifth's. They are held leases too.
_IN_WINDOW = 5
_HELD = _OPEN + _IN_WINDOW
#: (settled ledger rows, exhausted ledger rows, released leases) -- each term 10x.
_SCALES = ((10_000, 100, 10_000), (100_000, 1_000, 100_000))
_WINDOW_US = 86_400 * 1_000_000
_GRACE_US = 300 * 1_000_000


def _coordination(conn: Connection[dict[str, Any]]) -> None:
    """The rows every lease and attempt in this seed needs to exist first."""
    conn.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest)"
        " VALUES ('host-1', 'unified', 'ready', %s) ON CONFLICT DO NOTHING",
        (_DIGEST,),
    )
    conn.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state)"
        " VALUES ('pool-1', 'realtime', 'ready') ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, policy_revision)"
        " VALUES ('workspace-1', 1000000, 'policy-1') ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id,"
        " gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode,"
        " loading_policy, config_digest, instance_state, max_slots)"
        " VALUES ('instance-1', 1, 'host-1', 'pool-1', 'gateway-1', 'binding-1', 'manifest-1',"
        " 'template-1', 'managed', 'on_demand', %s, 'ready', 128) ON CONFLICT DO NOTHING",
        (_DIGEST,),
    )


def _seed_attempt_and_lease(
    conn: Connection[dict[str, Any]],
    *,
    job_id: str,
    lease_id: str,
    lease_state: str,
) -> None:
    """One accepted-cancellation Job with one lease, in the state the case needs."""
    conn.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id,"
        " binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class,"
        " request_digest, required_stage_ids)"
        " VALUES (%s, 'attempt-1', 'workspace-1', 'binding-1', 'manifest-1', %s, 'policy-1',"
        " 'realtime', %s, 'stage-1')",
        (job_id, _DIGEST, _DIGEST),
    )
    conn.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id,"
        " host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token,"
        " reserved_slots, lease_state, deadline_at_us)"
        " VALUES (%s, %s, 'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1',"
        " 'gateway-1', 1, 1, %s, %s)",
        (lease_id, job_id, lease_state, _OLD_US),
    )


def _seed(conn: Connection[dict[str, Any]], *, settled: int, exhausted: int, released: int) -> None:
    """Three histories plus the held debt, all in one schema."""
    _coordination(conn)
    # -- histories: settled cancellations, exhausted cancellations, released leases.
    # Their leases are released: the capacity came back, so none of this is debt.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), 'workspace-1', 'tts', 'tts-quality', %s,"
        " 'cancelled', 3, %s, %s FROM generate_series(1, %s) AS i",
        (_DIGEST, _OLD_US, _OLD_US, settled),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'hist-' || lpad(i::text, 8, '0'), %s, 'request-hist-' || i"
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
    # The exhausted history: the rows migration 0061's index kept forever. Their leases
    # are released, so they are not debt -- but they sit in the ledger's partial index.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'dead-' || lpad(i::text, 8, '0'), 'workspace-1', 'tts', 'tts-quality', %s,"
        " 'cancelled', 3, %s, %s FROM generate_series(1, %s) AS i",
        (_DIGEST, _OLD_US, _OLD_US, exhausted),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'dead-' || lpad(i::text, 8, '0'), %s, 'request-dead-' || i"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, exhausted),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts,"
        " next_check_at_us, last_outcome, settled_at_us, updated_at_us)"
        " SELECT 'dead-' || lpad(i::text, 8, '0'), 8, 0, 'exhausted', NULL, %s"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, exhausted),
    )
    conn.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id,"
        " binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class,"
        " request_digest, required_stage_ids)"
        " SELECT 'rel-' || lpad(i::text, 8, '0'), 'attempt-1', 'workspace-1', 'binding-1',"
        " 'manifest-1', %s, 'policy-1', 'realtime', %s, 'stage-1'"
        " FROM generate_series(1, %s) AS i",
        (_DIGEST, _DIGEST, released),
    )
    conn.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id,"
        " host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token,"
        " reserved_slots, lease_state, deadline_at_us)"
        " SELECT 'lease-rel-' || lpad(i::text, 8, '0'), 'rel-' || lpad(i::text, 8, '0'),"
        " 'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1', 'gateway-1', 1, 1,"
        " 'released', %s FROM generate_series(1, %s) AS i",
        (_OLD_US, released),
    )
    # -- the debt: held leases, acceptance long past the fourth read's window.
    for index in range(_OPEN_EXHAUSTED):
        job_id = f"open-dead-{index:03d}"
        conn.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest,"
            " state, status_version, created_at_us, updated_at_us)"
            " VALUES (%s, 'workspace-1', 'tts', 'tts-quality', %s, 'cancelled', 3, %s, %s)",
            (job_id, _DIGEST, _OLD_US, _OLD_US),
        )
        conn.execute(
            "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
            " VALUES (%s, %s, %s)",
            (job_id, _OLD_US, f"request-{job_id}"),
        )
        conn.execute(
            "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts,"
            " next_check_at_us, last_outcome, settled_at_us, updated_at_us)"
            " VALUES (%s, 8, 0, 'exhausted', NULL, %s)",
            (job_id, _OLD_US),
        )
        _seed_attempt_and_lease(
            conn, job_id=job_id, lease_id=f"lease-{job_id}", lease_state="reserved"
        )
    for index in range(_OPEN_UNSETTLED):
        job_id = f"open-due-{index:03d}"
        conn.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest,"
            " state, status_version, created_at_us, updated_at_us)"
            " VALUES (%s, 'workspace-1', 'tts', 'tts-quality', %s, 'cancelled', 3, %s, %s)",
            (job_id, _DIGEST, _OLD_US, _OLD_US),
        )
        conn.execute(
            "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
            " VALUES (%s, %s, %s)",
            (job_id, _OLD_US, f"request-{job_id}"),
        )
        conn.execute(
            "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts,"
            " next_check_at_us, last_outcome, settled_at_us, updated_at_us)"
            " VALUES (%s, 2, 0, 'pending', NULL, %s)",
            (job_id, _OLD_US),
        )
        _seed_attempt_and_lease(
            conn, job_id=job_id, lease_id=f"lease-{job_id}", lease_state="reserved"
        )
    for index in range(_OPEN_NO_LEDGER):
        # The worker never claimed this acceptance: no ledger row at all. A ledger-driven
        # read cannot see it, which is why this shape is in the seed.
        job_id = f"open-never-{index:03d}"
        conn.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest,"
            " state, status_version, created_at_us, updated_at_us)"
            " VALUES (%s, 'workspace-1', 'tts', 'tts-quality', %s, 'cancelled', 3, %s, %s)",
            (job_id, _DIGEST, _OLD_US, _OLD_US),
        )
        conn.execute(
            "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
            " VALUES (%s, %s, %s)",
            (job_id, _OLD_US, f"request-{job_id}"),
        )
        _seed_attempt_and_lease(
            conn, job_id=job_id, lease_id=f"lease-{job_id}", lease_state="reserved"
        )
    # -- the fourth read's rows: inside the window and inside the grace-free band.
    conn.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, state,"
        " status_version, created_at_us, updated_at_us)"
        " SELECT 'win-' || lpad(i::text, 4, '0'), 'workspace-1', 'tts', 'tts-quality', %s,"
        " 'cancelled', 3, %s, %s FROM generate_series(1, %s) AS i",
        (_DIGEST, _OLD_US, _OLD_US, _IN_WINDOW),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id)"
        " SELECT 'win-' || lpad(i::text, 4, '0'),"
        " CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - %s,"
        " 'request-win-' || i FROM generate_series(1, %s) AS i",
        # One hour: inside the 24 h window, past the 300 s grace, so the fourth read
        # owns these rows and the fifth must not claim them.
        (3_600 * 1_000_000, _IN_WINDOW),
    )
    conn.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts,"
        " next_check_at_us, last_outcome, settled_at_us, updated_at_us)"
        " SELECT 'win-' || lpad(i::text, 4, '0'), 1, 0, 'pending', NULL, %s"
        " FROM generate_series(1, %s) AS i",
        (_OLD_US, _IN_WINDOW),
    )
    conn.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id,"
        " binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class,"
        " request_digest, required_stage_ids)"
        " SELECT 'win-' || lpad(i::text, 4, '0'), 'attempt-1', 'workspace-1', 'binding-1',"
        " 'manifest-1', %s, 'policy-1', 'realtime', %s, 'stage-1'"
        " FROM generate_series(1, %s) AS i",
        (_DIGEST, _DIGEST, _IN_WINDOW),
    )
    conn.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id,"
        " host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token,"
        " reserved_slots, lease_state, deadline_at_us)"
        " SELECT 'lease-win-' || lpad(i::text, 4, '0'), 'win-' || lpad(i::text, 4, '0'),"
        " 'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1', 'gateway-1', 1, 1,"
        " 'reserved', %s FROM generate_series(1, %s) AS i",
        (_OLD_US, _IN_WINDOW),
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


def _nodes(plan: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    pending: list[dict[str, Any]] = [dict(plan)]
    while pending:
        node = pending.pop()
        pending.extend(node.get("Plans", []))
        yield node


def _scans(plan: Mapping[str, Any], relation: str) -> list[dict[str, Any]]:
    return [node for node in _nodes(plan) if node.get("Relation Name") == relation]


#: The relations the polled debt reads join that grow with the deployment: released
#: leases, settled and exhausted ledger rows, the fence history, and the Job table. A
#: sequential scan of any of them puts the deployment's history back into the 5 s poll,
#: which is the degradation these assertions exist for.
#:
#: `submit_intents` is deliberately absent. It is empty in this seed, so the planner reads
#: it at `Total Cost` 0.0 -- the plan CI reported in #1026 did exactly that -- and a scan
#: of an empty table says nothing about whether a read follows the window rather than
#: history. Listing it would fail otherwise bounded plans.
_GROWTH_RELATIONS = (
    "admission_leases",
    "speech_job_cancellation_reconciliations",
    "speech_job_cancellation_fences",
    "speech_jobs",
)

#: Scan types whose rows come from reading the relation end to end.
_SEQUENTIAL_SCANS = ("Seq Scan", "Parallel Seq Scan")
#: Scan types whose rows come from an index -- the window range or the held capacity,
#: never the relation's size. A `Bitmap Heap Scan` is the row source of a `Bitmap Index
#: Scan`, so both count as index-backed. No index is named, because any index that answers
#: the same range is equally bounded.
_INDEX_SCANS = ("Index Scan", "Index Only Scan", "Bitmap Index Scan", "Bitmap Heap Scan")


def _sequential_scans(plan: Mapping[str, Any], relation: str) -> list[dict[str, Any]]:
    return [node for node in _scans(plan, relation) if node["Node Type"] in _SEQUENTIAL_SCANS]


def _assert_no_sequential_scan(plan: Mapping[str, Any], relation: str) -> None:
    for node in _sequential_scans(plan, relation):
        raise AssertionError(
            f"{relation} must not be sequentially scanned: {node['Node Type']} returned"
            f" {int(node.get('Actual Rows', 0))} rows in"
            f" {int(node.get('Actual Loops', 0))} loop(s), so the read's cost follows the"
            " deployment's history"
        )


def _access_summary(plan: Mapping[str, Any], relations: Iterable[str]) -> str:
    """Name the access path of each relation, instead of printing the whole plan tree.

    A failed plan assertion used to print the entire EXPLAIN tree -- one line of several
    thousand characters -- which hid the premise that had failed. What decides a premise
    is the scan type, the index and the relation, so the message carries exactly those,
    plus the rows and loops each access returned.
    """
    wanted = tuple(relations)
    parts: list[str] = []
    for node in _nodes(plan):
        relation = node.get("Relation Name")
        if relation not in wanted:
            continue
        index = node.get("Index Name")
        access = f"{node['Node Type']} {index}" if index else str(node["Node Type"])
        parts.append(
            f"{access} on {relation} rows={int(node.get('Actual Rows', 0))}"
            f" loops={int(node.get('Actual Loops', 0))}"
        )
    return "[" + ", ".join(parts) + "]"


def _assert_bounded_read(
    plan: Mapping[str, Any], *, expected_rows: int, relations: Iterable[str] = _GROWTH_RELATIONS
) -> None:
    """The bounded read must return the window's rows through an index, never a scan.

    This states the degradation the read must not fall into, not the index it must choose.
    The fourth read has several bounded paths -- migration 0060's fence-age index,
    migration 0062's held-lease index, a bitmap plan over either, or a future index that
    answers the same range -- and none of them is its contract; staying bounded is.
    Naming one index (or a short list) is what CI #1026 hit: the plan was bounded and
    driven from the held leases, so the fence index was absent and the lease index carried
    the 15 held leases rather than the 5 in-window rows, and the test failed on a plan
    that had not degraded.

    Three facts are required, and each failure names the one that is missing:

    * no relation that grows with the deployment is read by a sequential scan;
    * the read returns exactly the in-window rows -- asserted on the read's own output
      rather than on an index node's count, because a nested-loop inner node reports rows
      *per loop*: the fence point lookup of a lease-driven plan reports 0 for the 5 rows it
      returns over 15 loops, so "some index node reports 5" is not a property every
      bounded plan shares;
    * those rows came from index-backed access to one of the read's relations.
    """
    wanted = tuple(relations)
    rows = int(plan.get("Actual Rows", 0))
    summary = _access_summary(plan, wanted)
    degraded = sorted(
        (node for relation in wanted for node in _sequential_scans(plan, relation)),
        key=lambda node: str(node["Relation Name"]),
    )
    if degraded:
        first = degraded[0]
        raise AssertionError(
            f"the bounded read returned {rows} rows from a sequential scan of"
            f" {first['Relation Name']} ({first['Node Type']},"
            f" rows={int(first.get('Actual Rows', 0))},"
            f" loops={int(first.get('Actual Loops', 0))}); a relation that grows with the"
            f" deployment must be read through an index: {summary}"
        )
    if rows != expected_rows:
        raise AssertionError(
            f"the bounded read returned {rows} rows, not the {expected_rows} in-window rows:"
            f" {summary}"
        )
    index_nodes = [
        node
        for node in _nodes(plan)
        if node.get("Node Type") in _INDEX_SCANS and node.get("Relation Name") in wanted
    ]
    if not index_nodes:
        raise AssertionError(
            f"no index-backed node carried the {expected_rows} in-window rows: {summary}"
        )


def _index_rows(plan: Mapping[str, Any], index: str) -> int | None:
    rows = [
        int(node.get("Actual Rows", 0)) for node in _nodes(plan) if node.get("Index Name") == index
    ]
    return max(rows) if rows else None


@pytest.mark.parametrize("settled,exhausted,released", _SCALES)
def test_the_permanent_read_is_bounded_by_the_held_leases(
    settled: int, exhausted: int, released: int
) -> None:
    """The lease index serves the read and returns only the held leases, at both scales."""
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _seed(query, settled=settled, exhausted=exhausted, released=released)

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
            f"\nsettled={settled} exhausted={exhausted} released={released}:"
            f" permanent={permanent['Actual Total Time']:.3f} ms"
            f" ({permanent.get('Shared Hit Blocks', 0)} hit +"
            f" {permanent.get('Shared Read Blocks', 0)} read blocks),"
            f" left_cancelling={bounded['Actual Total Time']:.3f} ms"
            f" ({bounded.get('Shared Hit Blocks', 0)} hit +"
            f" {bounded.get('Shared Read Blocks', 0)} read blocks)"
        )

        # The whole point of migration 0062: the held leases are the access path, and
        # every history the deployment accumulated is never read.
        assert [node for node in _nodes(permanent) if node.get("Index Name") == LEASE_DEBT_INDEX], (
            f"the plan must start from {LEASE_DEBT_INDEX}: {permanent}"
        )
        assert _index_rows(permanent, LEASE_DEBT_INDEX) == _HELD
        # The rows the polled read returns, not just the index entries the plan touches:
        # a read that silently drops a shape must fail here, not only in the probe tests.
        assert permanent["Actual Rows"] == _HELD
        # None of the four tables the read joins may be sequentially scanned: each one
        # grows with the deployment (released leases, settled and exhausted ledger rows,
        # the fence history), so a scan of any of them would put uptime back into the 5 s
        # poll. `admission_leases` is included now that it is the driving table.
        for relation in _GROWTH_RELATIONS:
            _assert_no_sequential_scan(permanent, relation)
        # The bounded fourth read keeps its own access path. Migration 0062 gives it a
        # second bounded path: with the held leases indexed, the planner may answer it from
        # the lease side and reach the fences by primary key instead; either path is
        # bounded by the window or by capacity. What must never happen is a scan of a
        # history table, so the assertion is the degradation it forbids -- no sequential
        # scan of a relation that grows with the deployment, and the in-window rows
        # returned through index-backed access -- not the index the planner picked.
        _assert_bounded_read(bounded, expected_rows=_IN_WINDOW)


def test_the_permanent_reads_cost_does_not_follow_the_three_histories() -> None:
    """Tenfold history, same debt: the same rows and the same number of pages."""
    readings: list[dict[str, float]] = []
    for settled, exhausted, released in _SCALES:
        with _isolated_schema(_url()) as (config, query):
            command.upgrade(config, "head")
            _seed(query, settled=settled, exhausted=exhausted, released=released)
            now_us = query.execute(
                "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
            ).fetchone()
            assert now_us is not None
            permanent = _plan(
                query,
                _PERMANENT_DEBT,
                {"now_us": now_us["now_us"], "grace_us": _GRACE_US, "limit": 96},
            )
            readings.append(
                {
                    "settled": float(settled),
                    "exhausted": float(exhausted),
                    "released": float(released),
                    "ms": float(permanent["Actual Total Time"]),
                    "blocks": float(permanent.get("Shared Hit Blocks", 0)),
                    "rows": float(permanent["Actual Rows"]),
                }
            )

    print(f"\nscaling: {readings}")
    assert [reading["rows"] for reading in readings] == [_HELD, _HELD]
    # 40 000 extra history rows (settled ledger, exhausted ledger, released leases
    # together) may move the buffer count by the handful of pages the debt's own probes
    # touch, but not by their order.
    assert readings[1]["blocks"] - readings[0]["blocks"] <= 64
    assert readings[1]["ms"] <= readings[0]["ms"] * 3 + 0.05
