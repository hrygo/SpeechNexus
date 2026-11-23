"""Differential oracle: the admission readiness read picks the same row per binding
after ``DISTINCT ON`` became an ordered read plus a first-row collapse.

``DISTINCT ON (p.revision_id) ... ORDER BY p.revision_id, ...`` is a PostgreSQL
construct outside the four-engine subset, so the statement now returns one row per
(binding head, ready sibling) in a total order and the reader keeps the first row of
each binding's run. "One row per binding" is a behavioural property, not a formatting
detail: the readiness report walks these rows to name the one instance a create would
pin, so a second row for a binding would report an instance no create can reach, and a
different first row would pin a different instance.

The strongest check available is not an expected row list -- that only repeats the
author's reasoning -- but the same fixture read twice: once with the frozen statement
that shipped with ``DISTINCT ON``, once through the reader that ships now. The two
results must be identical rows in identical order.

The fixture is one publication head per shape the statement distinguishes:

* ``rev-siblings``: two ready instances, inserted larger-``instance_id`` first, so the
  winner cannot be an accident of insertion or page order;
* ``rev-lex``: two ready instances whose ids are ``inst-9`` and ``inst-10``. They tie
  on the leading ordering column and are separated by the trailing one, and the
  columns are ``COLLATE "C"``, so byte order decides -- ``inst-10`` sorts first even
  though 9 is the smaller number;
* ``rev-no-instance``: a published head with no ready instance, which must survive as
  a NULL row instead of disappearing;
* ``rev-stopped``: a ``stopped`` instance that sorts before the ready one, which must
  be filtered out of the candidate rows entirely;
* ``rev-stale``: a ready instance from generation 1 while the head is generation 2
  (``loading``), so the head join leaves no candidate;
* ``rev-no-prepare``: a ready instance whose head carries no prepare intent;
* ``rev-retired``: a retired publication, dropped by the retirement predicate;
* ``rev-super-1`` / ``rev-super-2``: two published revisions where the head names the
  second, so the first cannot be named by a create and must not appear.

Why the order is total: ``speech_binding_revisions.revision_id`` is UNIQUE, so a
publication head joins one revision row; ``runtime_instance_heads.instance_id`` is that
table's primary key, so one instance contributes at most one generation; and
``engine_instances`` is keyed by ``(instance_id, instance_generation)``. One head
therefore emits exactly one row per ready sibling, distinct on the full ordering key,
and the trailing publication keys only break ties between rows of one revision. A head
with no candidate is a run of one NULL row, which ``DISTINCT ON`` also kept.

Boundaries: real PostgreSQL with the packaged migration and hand-built coordination
rows. It is not the readiness verdict itself (``test_public_job_admission_readiness_pg``
drives that end to end), not a concurrency case, and not MySQL evidence.
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

from speechnexus_backend.speech.infrastructure.public_job_admission import _readiness_candidates

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "d" * 64
NOW_US = 1_700_000_000_000_000

#: The statement as it shipped while the read used ``DISTINCT ON``. Frozen here on
#: purpose: it is the reference side of the oracle, so it must not follow the reader.
FROZEN_QUERY = """
SELECT DISTINCT ON (p.revision_id) p.revision_id AS binding_revision_id,
r.definition_json, i.instance_id, i.instance_generation, i.host_node_id, i.pool_id
FROM speech_publications AS p
JOIN speech_binding_revisions AS r ON r.binding_id = p.object_id
AND r.revision = p.revision AND r.revision_id = p.revision_id
LEFT JOIN (
SELECT i.binding_revision_id, i.instance_id, i.instance_generation,
i.host_node_id, i.pool_id FROM engine_instances AS i
JOIN runtime_instance_heads AS h ON h.instance_id = i.instance_id
AND h.current_generation = i.instance_generation
WHERE i.instance_state='ready' AND i.instance_generation IS NOT NULL
AND h.current_prepare_intent_id IS NOT NULL
) AS i ON i.binding_revision_id = p.revision_id
WHERE p.kind = 'binding'
AND NOT EXISTS (
SELECT 1 FROM speech_publication_retirements AS retired
WHERE retired.kind = p.kind
AND retired.object_id = p.object_id AND retired.revision = p.revision
AND retired.revision_id = p.revision_id)
AND EXISTS (
SELECT 1 FROM speech_publication_heads AS head
WHERE head.kind = p.kind AND head.object_id = p.object_id
AND head.revision = p.revision AND head.revision_id = p.revision_id)
ORDER BY p.revision_id, i.instance_id, i.instance_generation
"""


@dataclass(frozen=True)
class Instance:
    """One ``engine_instances`` row plus the head row the statement joins on."""

    instance_id: str
    state: str = "ready"
    generation: int = 1
    prepare_intent: str | None = "prepare-intent"
    #: Publication revision the instance is registered behind; ``None`` means the head.
    revision: str | None = None


@dataclass(frozen=True)
class Binding:
    binding_id: str
    revision_id: str
    instances: tuple[Instance, ...]
    #: Published revisions in order; the last one is the head.
    revisions: tuple[str, ...] = ("1",)
    retired: bool = False
    #: The revision id of the last published revision, when it differs from the first.
    tail_revision_id: str | None = None


BINDINGS: tuple[tuple[Binding, str | None], ...] = (
    # (binding, the instance a create would pin; ``None`` means "no pin target")
    (
        Binding(
            "b-siblings",
            "rev-siblings",
            # Inserted larger first: insertion order cannot produce the answer.
            (Instance("inst-b"), Instance("inst-a")),
        ),
        "inst-a",
    ),
    (Binding("b-lex", "rev-lex", (Instance("inst-9"), Instance("inst-10"))), "inst-10"),
    (Binding("b-no-instance", "rev-no-instance", ()), None),
    (
        Binding(
            "b-stopped",
            "rev-stopped",
            (Instance("inst-0", state="stopped"), Instance("inst-d")),
        ),
        "inst-d",
    ),
    (
        Binding(
            "b-stale",
            "rev-stale",
            # The head generation goes in first so the head row points at it; the ready
            # generation 1 is then not the head and contributes no candidate.
            (Instance("inst-h", state="loading", generation=2), Instance("inst-h")),
        ),
        None,
    ),
    (Binding("b-no-prepare", "rev-no-prepare", (Instance("inst-p", prepare_intent=None),)), None),
    (Binding("b-retired", "rev-retired", (Instance("inst-r"),), retired=True), None),
    (
        Binding(
            "b-superseded",
            "rev-super-1",
            (Instance("inst-s1", revision="1"), Instance("inst-s2")),
            revisions=("1", "2"),
            tail_revision_id="rev-super-2",
        ),
        "inst-s2",
    ),
)


@dataclass
class Fixture:
    connection: psycopg.Connection[dict[str, Any]]
    schema: str


@pytest.fixture
def migrated() -> Iterator[Fixture]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not candidate-selection evidence"
        )
    schema = "sn_cand_oracle_" + uuid.uuid4().hex[:12]
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
        yield Fixture(connection=connection, schema=schema)
    finally:
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.close()
        engine.dispose()


def _revision_id(binding: Binding, revision: str) -> str:
    if revision == binding.revisions[0]:
        return binding.revision_id
    assert binding.tail_revision_id is not None
    return binding.tail_revision_id


def head_revision_id(binding: Binding) -> str:
    return _revision_id(binding, binding.revisions[-1])


def expected_pins() -> dict[str, str | None]:
    """The row per nameable head: retired publications and non-heads are not nameable."""

    return {
        head_revision_id(binding): pinned for binding, pinned in BINDINGS if not binding.retired
    }


def seed(fixture: Fixture) -> None:
    connection = fixture.connection
    connection.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, "
        "topology_digest) VALUES ('node-a', 'unified_memory', 'active', %s), "
        "('node-b', 'unified_memory', 'active', %s)",
        (DIGEST, DIGEST),
    )
    connection.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('realtime', 'realtime', 'active')"
    )
    for binding, _ in BINDINGS:
        connection.execute(
            "INSERT INTO speech_binding_families (binding_id, latest_revision) VALUES (%s, %s)",
            (binding.binding_id, binding.revisions[-1]),
        )
        for revision in binding.revisions:
            revision_id = _revision_id(binding, revision)
            key_digest = f"key-{binding.binding_id}-{revision}"
            connection.execute(
                "INSERT INTO speech_binding_commands (key_digest, binding_id, "
                "document_version, intent_json, request_digest) "
                "VALUES (%s, %s, 1, '{}', %s)",
                (key_digest, binding.binding_id, f"request-{key_digest}"),
            )
            connection.execute(
                "INSERT INTO speech_binding_revisions (binding_id, revision, revision_id, "
                "key_digest, document_version, definition_json, definition_digest, "
                "request_digest, created_at_us) VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s)",
                (
                    binding.binding_id,
                    revision,
                    revision_id,
                    key_digest,
                    # One distinct definition per revision, so a wrong de-duplication
                    # is visible in the selected row rather than only in the instance.
                    f'{{"tasks": ["tts"], "revision": "{revision}"}}',
                    DIGEST,
                    f"request-{key_digest}",
                    NOW_US,
                ),
            )
            connection.execute(
                "INSERT INTO speech_publications (kind, object_id, revision, revision_id, "
                "published_at_us) VALUES ('binding', %s, %s, %s, %s)",
                (binding.binding_id, revision, revision_id, NOW_US),
            )
        head = binding.revisions[-1]
        connection.execute(
            "INSERT INTO speech_publication_heads (kind, object_id, revision, revision_id, "
            "updated_at_us) VALUES ('binding', %s, %s, %s, %s)",
            (binding.binding_id, head, _revision_id(binding, head), NOW_US),
        )
        if binding.retired:
            connection.execute(
                "INSERT INTO speech_publication_retirements (kind, object_id, revision, "
                "revision_id, retired_at_us) VALUES ('binding', %s, %s, %s, %s)",
                (binding.binding_id, head, _revision_id(binding, head), NOW_US),
            )
        for index, instance in enumerate(binding.instances):
            _instance_row(fixture, binding, instance, node_id="node-b" if index % 2 else "node-a")


def _instance_row(fixture: Fixture, binding: Binding, instance: Instance, *, node_id: str) -> None:
    connection = fixture.connection
    revision = binding.revisions[-1] if instance.revision is None else instance.revision
    connection.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
        "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
        "management_mode, loading_policy, config_digest, instance_state, max_slots) VALUES "
        "(%s, %s, %s, 'realtime', 'gateway-test', %s, 'manifest-test', 'template-test', "
        "'managed', 'on_demand', %s, %s, 1)",
        (
            instance.instance_id,
            instance.generation,
            node_id,
            _revision_id(binding, revision),
            DIGEST,
            instance.state,
        ),
    )
    # The head row is written by the first row of an instance; the later generation of
    # a stale instance must not move it.
    head = connection.execute(
        "SELECT 1 FROM runtime_instance_heads WHERE instance_id = %s",
        (instance.instance_id,),
    ).fetchone()
    if head is not None:
        return
    connection.execute(
        "INSERT INTO runtime_instance_heads (instance_id, current_generation, "
        "generation_high_water, current_prepare_intent_id) VALUES (%s, %s, %s, %s)",
        (
            instance.instance_id,
            instance.generation,
            instance.generation,
            instance.prepare_intent,
        ),
    )


def test_the_shipped_reader_selects_the_same_row_per_binding_as_the_frozen_statement(
    migrated: Fixture,
) -> None:
    seed(migrated)
    frozen = [dict(row) for row in migrated.connection.execute(FROZEN_QUERY).fetchall()]
    shipped = _readiness_candidates(migrated.connection.cursor())

    # The oracle compares the same question on both sides, so an empty fixture or a
    # statement that lost a predicate fails here before it can agree by accident.
    assert shipped == frozen
    assert len(shipped) == len(expected_pins())
    observed = {row["binding_revision_id"]: row["instance_id"] for row in shipped}
    assert observed == expected_pins()
    # The two heads the de-duplication actually decides, named so the reason is read
    # rather than inferred from the fixture.
    assert observed["rev-siblings"] == "inst-a"
    assert observed["rev-lex"] == "inst-10"
