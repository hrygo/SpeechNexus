"""Differential oracle: the pool summary counts ready instances the same way before
and after the filtered aggregate became a conditional ``CASE`` sum.

``count(*) FILTER (WHERE ...)`` is the PostgreSQL form this projection shipped, and
the portable ``sum(CASE ...)`` form differs from it in exactly two ways that a
hand-written expected row cannot catch:

* if the predicate is lost, the sum counts every row of the group instead of the
  ready ones;
* over an empty set the sum answers NULL where the count answers 0.

So the same fixture is read twice -- once with the frozen statement, once with the
text the adapter actually ships -- and the two results must be identical rows, with
identical column types.

The fixture is four pools, and between them they are every shape the projection can
meet:

* ``pool-empty``: the pool exists and no instance row does, so the aggregate subquery
  has no group for it at all and the LEFT JOIN misses;
* ``pool-all-ready``: three ready instances, so the count is 3;
* ``pool-none-ready``: two instances and neither is ready, so the count is 0, not NULL;
* ``pool-mixed``: ready, loading, quarantined and ``stopped`` in one pool, so the count
  is 1 -- the stopped instance is outside the aggregate, and a lost predicate would
  answer 3 here.

The empty set the portable form actually risks is the zero-row aggregate itself:
``sum`` over no rows is NULL where ``count`` is 0. That empty-set case is asserted by
``test_the_two_ready_count_forms_only_agree_on_an_empty_set_through_the_coalesce``,
which runs both aggregate expressions over a selection with no rows to show the
difference and that the ``COALESCE`` in the shipped expression is what closes it. It
cannot arise inside the statement -- a ``GROUP BY`` row implies at least one row -- so
it is asserted on the expression rather than on a fixture row.

Boundaries: real PostgreSQL, the packaged migration and the shipped statement text.
Not MySQL evidence (that engine has no ``FILTER``), not a concurrency case, and not a
claim about the HTTP route, which ``test_admin_resource_pools_pg.py`` covers.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.infrastructure.resource_pools_postgres import _QUERY

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "c" * 64

#: PostgreSQL's ``bigint`` type OID. The shipped expression casts for a reason: the
#: column must stay ``bigint`` so a count is a count, not a dialect's numeric type.
BIGINT_OID = 20

#: The statement as it shipped while the projection used a filtered aggregate. This
#: text is frozen here on purpose: it is the reference side of the oracle, so it must
#: not follow the adapter when the adapter changes.
FROZEN_QUERY = """
SELECT pool.pool_id, pool.pool_state, pool.service_class,
       COALESCE(CAST(aggregate.node_ids AS text[]), CAST(ARRAY[] AS text[])) AS node_ids,
       COALESCE(aggregate.ready_instances, 0) AS ready_instances,
       CAST(COALESCE(aggregate.max_active_slots, 0) AS bigint) AS max_active_slots,
       CAST(COALESCE(aggregate.realtime_reserved_slots, 0) AS bigint) AS realtime_reserved_slots,
       CAST(COALESCE(aggregate.active_slots, 0) AS bigint) AS active_slots,
       CAST(COALESCE(aggregate.quarantine_slots, 0) AS bigint) AS quarantine_slots
FROM resource_pools AS pool
LEFT JOIN (
    SELECT pool_id,
           array_agg(DISTINCT host_node_id ORDER BY host_node_id) AS node_ids,
           count(*) FILTER (WHERE instance_state = 'ready') AS ready_instances,
           CAST(sum(max_slots) AS bigint) AS max_active_slots,
           CAST(sum(realtime_reserved_slots) AS bigint) AS realtime_reserved_slots,
           CAST(sum(held_slots) AS bigint) AS active_slots,
           CAST(sum(quarantined_slots) AS bigint) AS quarantine_slots
    FROM engine_instances
    WHERE instance_state <> 'stopped'
    GROUP BY pool_id
) AS aggregate ON aggregate.pool_id = pool.pool_id
ORDER BY pool.pool_id
"""

#: ``(pool_id, service_class, instance rows)``; a row is
#: ``(instance_id, host_node_id, instance_state)``. The slot columns are constant: this
#: oracle reads the ready count, and the remaining sums have their own cases.
FIXTURE: tuple[tuple[str, str, tuple[tuple[str, str, str], ...]], ...] = (
    ("pool-empty", "batch", ()),
    (
        "pool-all-ready",
        "realtime",
        (
            ("instance-a1", "node-a", "ready"),
            ("instance-a2", "node-a", "ready"),
            ("instance-b1", "node-b", "ready"),
        ),
    ),
    (
        "pool-none-ready",
        "batch",
        (("instance-n1", "node-a", "loading"), ("instance-n2", "node-b", "quarantined")),
    ),
    (
        "pool-mixed",
        "realtime",
        (
            ("instance-m1", "node-a", "ready"),
            ("instance-m2", "node-b", "loading"),
            ("instance-m3", "node-a", "quarantined"),
            # Released budget: outside the aggregate, so it must not be counted.
            ("instance-m4", "node-b", "stopped"),
        ),
    ),
)


@dataclass
class Schema:
    connection: psycopg.Connection[dict[str, Any]]
    schema: str

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> Any:
        return self.connection.execute(statement, parameters or None)


@pytest.fixture
def migrated() -> Iterator[Schema]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not aggregate evidence"
        )
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_pool_oracle_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    connection: psycopg.Connection[dict[str, Any]] = portable_pg_connect(
        url, row_factory=dict_row, autocommit=True
    )
    try:
        with engine.connect() as migration:
            config.attributes["connection"] = migration
            command.upgrade(config, "head")
        connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        yield Schema(connection=connection, schema=schema)
    finally:
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.close()
        engine.dispose()


def seed(fixture: Schema) -> None:
    for node_id in ("node-a", "node-b"):
        fixture.execute(
            "INSERT INTO host_nodes "
            "(host_node_id, memory_architecture, node_state, topology_digest) "
            "VALUES (%s, 'unified_memory', 'active', %s)",
            (node_id, DIGEST),
        )
    for pool_id, service_class, instances in FIXTURE:
        fixture.execute(
            "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
            "VALUES (%s, %s, 'active')",
            (pool_id, service_class),
        )
        for instance_id, node_id, state in instances:
            fixture.execute(
                "INSERT INTO engine_instances (instance_id, instance_generation, "
                "host_node_id, pool_id, gateway_id, binding_revision_id, manifest_id, "
                "approved_template_id, management_mode, loading_policy, config_digest, "
                "instance_state, max_slots) VALUES "
                "(%s, 1, %s, %s, 'gateway-test', 'binding-test', 'manifest-test', "
                "'template-test', 'managed', 'on_demand', %s, %s, 1)",
                (instance_id, node_id, pool_id, DIGEST, state),
            )


def rows_with_types(connection: psycopg.Connection[dict[str, Any]], statement: str) -> Any:
    cursor = connection.execute(statement)
    records = [dict(row) for row in cursor.fetchall()]
    types = [column.type_code for column in cursor.description or ()]
    return records, types


def test_both_forms_project_identical_pool_rows_on_the_same_fixture(migrated: Schema) -> None:
    seed(migrated)
    frozen, frozen_types = rows_with_types(migrated.connection, FROZEN_QUERY)
    shipped, shipped_types = rows_with_types(migrated.connection, _QUERY)

    # The comparison comes first so a broken statement fails on the rows it produced,
    # not on a shape assertion that could have been reworded.
    assert shipped == frozen

    # The oracle only means something while the shipped statement is the portable
    # form: a vendor ``FILTER`` reintroduced later would make both sides one statement.
    assert _QUERY != FROZEN_QUERY
    assert "FILTER (WHERE" in FROZEN_QUERY
    assert "count(*)" not in _QUERY
    assert "sum(CASE WHEN instance_state = 'ready' THEN 1 ELSE 0 END)" in _QUERY
    assert shipped_types == frozen_types
    # ``ready_instances`` is the fifth column of both statements.
    assert shipped_types[4] == BIGINT_OID
    assert [row["pool_id"] for row in shipped] == sorted(name for name, _, _ in FIXTURE)
    # Expected values reasoned from the fixture, so two copies of one wrong statement
    # cannot agree their way to green.
    assert {row["pool_id"]: row["ready_instances"] for row in shipped} == {
        "pool-empty": 0,
        "pool-all-ready": 3,
        "pool-none-ready": 0,
        "pool-mixed": 1,
    }


def test_the_two_ready_count_forms_only_agree_on_an_empty_set_through_the_coalesce(
    migrated: Schema,
) -> None:
    seed(migrated)
    # A pool name no instance carries: the selection has no rows, which is the case a
    # ``GROUP BY`` cannot reach and the only case where the two forms differ.
    empty = "FROM engine_instances WHERE pool_id = 'pool-absent'"
    forms = {
        "filtered": f"SELECT count(*) FILTER (WHERE instance_state = 'ready') AS counted {empty}",
        "bare_case_sum": (
            f"SELECT sum(CASE WHEN instance_state = 'ready' THEN 1 ELSE 0 END) AS counted {empty}"
        ),
        "shipped": (
            f"SELECT CAST(COALESCE(sum(CASE WHEN instance_state = 'ready' THEN 1 ELSE 0 END), 0) "
            f"AS bigint) AS counted {empty}"
        ),
    }
    values: dict[str, Any] = {}
    types: dict[str, int] = {}
    for name, statement in forms.items():
        cursor = migrated.connection.execute(statement)
        row = cursor.fetchone()
        description = cursor.description
        assert row is not None and description
        values[name] = row["counted"]
        types[name] = description[0].type_code
    assert values["filtered"] == 0
    assert values["bare_case_sum"] is None  # the difference the COALESCE closes
    assert values["shipped"] == values["filtered"] == 0
    assert types["shipped"] == types["filtered"] == BIGINT_OID
