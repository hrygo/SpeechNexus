"""0062 bounds the permanent debt read by the leases that still hold capacity.

``cancellation_debt``'s fifth read cannot be bounded by time -- the debt it exists for
is the debt that outlived the 24 h window -- and it cannot be bounded by the ledger
either: migration 0061's partial index over ``settled_at_us IS NULL OR last_outcome =
'exhausted'`` keeps every ``exhausted`` row forever (nothing ever settles one), so it
grows with the deployment's refused releases even after the capacity came back. 0062
drops that index and indexes the held leases instead, a set bounded by the instance
slots that reserve the capacity. Both directions must stay observable: the new index is
created, the superseded one is gone, and 0062's downgrade must not silently remove the
access path.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from psycopg import Connection
from tests.integration.test_permanent_debt_plans_pg import _coordination
from tests.integration.test_public_job_progress_migration_pg import (
    _isolated_schema,
    _url,
)
from tests.support.migrations import (
    HEAD,
    LEASE_DEBT_INDEX,
    LEASE_DEBT_INDEX_REVISION,
    LEDGER_DEBT_INDEX,
    LEDGER_DEBT_INDEX_REVISION,
    LEFT_CANCELLING_INDEX_REVISION,
    step_below_lease_debt_index,
)

pytestmark = pytest.mark.integration


def _definition(query: Connection[dict[str, Any]], index: str) -> str | None:
    row = query.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
        (index,),
    ).fetchone()
    return None if row is None else str(row["indexdef"])


def _revision(query: Connection[dict[str, Any]]) -> str:
    row = query.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row["version_num"])


def test_0062_indexes_the_held_leases_and_drops_the_superseded_ledger_index() -> None:
    with _isolated_schema(_url()) as (config, query):
        # The state 0061 shipped: the ledger index exists, the lease index does not.
        command.upgrade(config, LEDGER_DEBT_INDEX_REVISION)
        assert _definition(query, LEDGER_DEBT_INDEX) is not None
        assert _definition(query, LEASE_DEBT_INDEX) is None

        # The refusal below is 0062's own, so stop at 0062 rather than at the
        # current head: a later revision would refuse the downgrade first.
        command.upgrade(config, LEASE_DEBT_INDEX_REVISION)
        # 0061's index is not left behind: nothing reads it any more, and its predicate
        # keeps every `exhausted` row forever, so keeping it would be a pure write cost
        # that grows with the deployment's failures.
        assert _definition(query, LEDGER_DEBT_INDEX) is None
        definition = _definition(query, LEASE_DEBT_INDEX)
        assert definition is not None
        # The read and the index must state the same predicate, or the planner cannot
        # prove the implication and the access path silently disappears.
        assert "(operation_id)" in definition
        assert "WHERE" in definition
        assert "released" in definition
        assert "quarantined" in definition
        # It is partial because `admission_leases` keeps every lease forever; holding
        # the released history would put uptime back into the 5 s poll.
        assert "settled_at_us" not in definition

        # 0062's own refusal, not 0061's: the access path is a runtime safety property.
        with pytest.raises(RuntimeError) as refused:
            command.downgrade(config, LEDGER_DEBT_INDEX_REVISION)
        message = str(refused.value)
        assert "0062" in message
        assert LEASE_DEBT_INDEX in message
        assert LEDGER_DEBT_INDEX in message  # it says the superseded index is not restored
        assert "confirm its removal explicitly" in message

        # The refused downgrade leaves the head and the access path it ships intact.
        assert _revision(query) == LEASE_DEBT_INDEX_REVISION
        assert _definition(query, LEASE_DEBT_INDEX) == definition

        # A later upgrade to the current head keeps the index: 0063 only widens
        # the deletion ledger and must replay over the same schema.
        command.upgrade(config, "head")
        assert _definition(query, LEASE_DEBT_INDEX) == definition
        assert _definition(query, LEDGER_DEBT_INDEX) is None


def test_0063_refuses_its_own_downgrade_from_the_current_head() -> None:
    """The newest revision also refuses: the ledger's blocked reason is evidence."""
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        head = _revision(query)
        assert head == HEAD

        with pytest.raises(RuntimeError) as refused:
            command.downgrade(config, LEASE_DEBT_INDEX_REVISION)
        message = str(refused.value)
        assert "0063" in message
        assert "blocked" in message

        assert _revision(query) == head


def test_0061_still_refuses_its_own_downgrade_from_the_head_0062_inherited() -> None:
    """0062 does not weaken the earlier guard: reaching 0061 still refuses.

    0062 dropped 0061's index, so the pre-0062 head has to be reconstructed (remove the
    lease index, restore the ledger index, stamp 0061) before the older revision's own
    refusal can be observed at all.
    """
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        step_below_lease_debt_index(query)
        assert _revision(query) == LEDGER_DEBT_INDEX_REVISION
        assert _definition(query, LEDGER_DEBT_INDEX) is not None

        with pytest.raises(RuntimeError) as refused:
            command.downgrade(config, LEFT_CANCELLING_INDEX_REVISION)
        message = str(refused.value)
        assert "0061" in message
        assert LEDGER_DEBT_INDEX in message
        assert _revision(query) == LEDGER_DEBT_INDEX_REVISION


def _size(query: Connection[dict[str, Any]]) -> int:
    row = query.execute(
        "SELECT pg_relation_size(c.oid) AS bytes FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s",
        (LEASE_DEBT_INDEX,),
    ).fetchone()
    assert row is not None
    return int(row["bytes"])


def _seed_released_leases(query: Connection[dict[str, Any]], *, count: int) -> None:
    """The lease history a long-running deployment keeps: released, never pruned."""
    digest = "sha256:" + "e" * 64
    query.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) "
        "SELECT 'hist-' || lpad(i::text, 6, '0'), 'workspace-1', 'tts', 'tts-quality', %s, "
        "'cancelled', 3, 1, 1 FROM generate_series(1, %s) AS i",
        (digest, count),
    )
    query.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) "
        "SELECT 'hist-' || lpad(i::text, 6, '0'), 'attempt-1', 'workspace-1', 'binding-1', "
        "'manifest-1', %s, 'policy-1', 'realtime', %s, 'stage-1' "
        "FROM generate_series(1, %s) AS i",
        (digest, digest, count),
    )
    query.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
        "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
        "reserved_slots, lease_state, deadline_at_us) "
        "SELECT 'lease-hist-' || lpad(i::text, 6, '0'), 'hist-' || lpad(i::text, 6, '0'), "
        "'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1', 'gateway-1', 1, 1, "
        "'released', 1 FROM generate_series(1, %s) AS i",
        (count,),
    )


def _seed_held_lease(query: Connection[dict[str, Any]], *, index: int) -> None:
    """One reservation that still holds capacity, with its accepted cancellation."""
    job_id = f"held-{index:06d}"
    query.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) "
        "VALUES (%s, 'workspace-1', 'tts', 'tts-quality', %s, 'cancelled', 3, 1, 1)",
        (job_id, "sha256:" + "f" * 64),
    )
    query.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id) "
        "VALUES (%s, 1, %s)",
        (job_id, f"request-held-{index}"),
    )
    query.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) "
        "VALUES (%s, 'attempt-1', 'workspace-1', 'binding-1', 'manifest-1', %s, 'policy-1', "
        "'realtime', %s, 'stage-1')",
        (job_id, "sha256:" + "f" * 64, "sha256:" + "f" * 64),
    )
    query.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
        "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
        "reserved_slots, lease_state, deadline_at_us) "
        "VALUES (%s, %s, 'attempt-1', 'stage-1', 'host-1', 'instance-1', 1, 'pool-1', "
        "'gateway-1', 1, 1, 'reserved', 1)",
        (f"lease-held-{index}", job_id),
    )


def test_the_index_holds_the_held_leases_not_the_released_history() -> None:
    """The partial predicate is what makes the read's cost follow the held capacity.

    A deployment that has released 2 000 leases must not carry a 2 000-entry index for
    the 5 s poll to walk: the index may only hold the reservations that still exist.
    This is the size half of the counter-example -- dropping the ``WHERE`` turns the
    index into a copy of the lease history, and the assertion below makes that
    observable without timing a query.
    """
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _coordination(query)
        _seed_released_leases(query, count=2_000)
        released_only = _size(query)

        for index in range(3):
            _seed_held_lease(query, index=index)
        with_held = _size(query)

    # Three entries -- operation ids of twelve bytes -- fit in one leaf page. A
    # non-partial index over 2 003 leases cannot: its directory alone is tens of
    # kilobytes.
    assert released_only <= 16 * 1024
    assert with_held <= 24 * 1024
    assert with_held - released_only <= 8 * 1024
