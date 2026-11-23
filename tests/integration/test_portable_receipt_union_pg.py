"""Differential oracle: the receipt union answers the same rows before and after the
typed NULLs in the erased arm became bare ``NULL``.

The statement reads the live receipt and the erased receipt in one statement so that
``READ COMMITTED`` cannot see a gap between two snapshots. The erased arm carries no
project/transcript identity, and its columns used to be written as
``CAST(NULL AS text)`` / ``CAST(NULL AS numeric)``: a vendor spelling that names the
type of a value that is always NULL. Only the first arm's columns decide a UNION's
column types, so the names were redundant -- and ``CAST(... AS text)`` is not even
parseable on every engine the contract targets.

The oracle runs the frozen pre-change text and the shipped text on one fixture and
requires identical rows *and* identical column type OIDs. That is what a hand-written
expected row cannot catch: a lost column, a flipped ``erased`` flag or a changed arm
type would all still produce "a row".

The fixture is three receipts and between them every shape the union can meet:

* ``k-erased``: a live receipt *and* its erased counterpart, so both arms return a row
  and ``ORDER BY erased DESC`` must pick the erased one;
* ``k-live``: a live receipt only, so the first arm answers;
* ``k-transcript``: the transcript flavour, where the project columns are NULL from the
  data rather than from the arm;
* ``k-absent``: neither table has the key, so the statement returns nothing.

The erased receipt hangs off a deletion record whose plan carries a ``jsonb`` array with
non-ASCII text and nested arrays, so the erased path is exercised through the same plan
document the deletion flow writes; the test asserts that document still round-trips.

Boundaries: real PostgreSQL, the module's own migration DDL and the shipped statement
text. Not MySQL evidence, not a concurrency case, and not a claim about the HTTP route.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.workbench.infrastructure.deletion_schema import deletion_migration_sql
from speechnexus_backend.workbench.infrastructure.postgres_versions import _RECEIPT_UNION
from speechnexus_backend.workbench.infrastructure.version_schema import migration_sql

pytestmark = pytest.mark.integration
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"

#: PostgreSQL type OIDs for the six projected columns, in statement order.
TEXT_OID = 25
NUMERIC_OID = 1700
BOOL_OID = 16

#: The statement as it shipped while the erased arm named its NULL types. Frozen here on
#: purpose: it is the reference side of the oracle, so it must not follow the adapter.
FROZEN_QUERY = """
            SELECT * FROM (
                SELECT intent_digest, project_id, project_revision, transcript_id,
                    transcript_revision, false AS erased
                FROM {s}.mutation_receipts
                WHERE operation = ? AND scope_id = ? AND key_digest = ?
                UNION ALL
                SELECT intent_digest, CAST(NULL AS text), CAST(NULL AS numeric), CAST(NULL AS text),
                    CAST(NULL AS numeric), true AS erased
                FROM {s}.erased_mutation_receipts
                WHERE operation = ? AND scope_id = ? AND key_digest = ?
            ) AS candidates ORDER BY erased DESC LIMIT 1
        """

#: Non-ASCII text and nested arrays in the plan document the erased receipt points at.
DEPENDENCIES = '[[{"kind":"project","名称":"项目甲"}],["语音",["甲","乙"]]]'
CONTENT_SNAPSHOT = json.dumps({"标题": "项目甲 · 中文", "嵌套": [["甲", "乙"]]}, ensure_ascii=False)
SCOPE = "项目-甲-🎧"
DIGEST = "a" * 64
TIMESTAMP = "2026-11-24T00:00:00Z"


@dataclass
class Schema:
    connection: psycopg.Connection[dict[str, Any]]
    schema: str

    def statement(self, text: str) -> sql.Composed:
        return sql.SQL(text).format(s=sql.Identifier(self.schema))

    def row(self, text: str, parameters: tuple[object, ...]) -> Any:
        return self.connection.execute(self.statement(text), parameters)


@pytest.fixture
def migrated() -> Iterator[Schema]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not union evidence"
        )
    schema = "sn_receipt_union_" + uuid.uuid4().hex[:12]
    # One explicit transaction: ``projects_head`` and ``transcripts_head`` are
    # DEFERRABLE INITIALLY DEFERRED, so a forward reference (projects before
    # project_versions) is only legal while the commits are suppressed.
    connection: psycopg.Connection[dict[str, Any]] = portable_pg_connect(
        url, row_factory=dict_row, autocommit=False
    )
    try:
        connection.execute(migration_sql(schema))
        connection.execute(deletion_migration_sql(schema))
        connection.commit()
        yield Schema(connection=connection, schema=schema)
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
        connection.commit()
        connection.close()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def execute(fixture: Schema, text: str, parameters: tuple[object, ...] = ()) -> Any:
    return fixture.connection.execute(
        sql.SQL(text).format(s=sql.Identifier(fixture.schema)), parameters or None
    )


def seed(fixture: Schema) -> None:
    for project in ("proj-live", "proj-erased"):
        execute(
            fixture,
            "INSERT INTO {s}.projects VALUES (%s, 'ws-synthetic', %s, 7)",
            (project, TIMESTAMP),
        )
        execute(
            fixture,
            "INSERT INTO {s}.project_versions VALUES (%s, 7, '合成项目', '描述', 'active', %s)",
            (project, TIMESTAMP),
        )
    execute(
        fixture,
        "INSERT INTO {s}.transcripts VALUES ('tr-1', 'proj-live', '合成转写', %s, 3)",
        (TIMESTAMP,),
    )
    for revision in (1, 2, 3):
        execute(
            fixture,
            "INSERT INTO {s}.transcript_revisions (id, transcript_id, revision, status, text, "
            "source_revision, source_artifact_id, source_receipt_id, created_at) "
            "VALUES (%s, 'tr-1', %s, 'draft', %s, %s, NULL, NULL, %s)",
            (
                f"tr-1-r{revision}",
                revision,
                f"合成文本-{revision} · 中文",
                revision - 1 if revision > 1 else None,
                TIMESTAMP,
            ),
        )
    execute(
        fixture,
        "INSERT INTO {s}.deletion_plans (id, workspace_id, target_kind, target_id, "
        "target_revision, mode, created_at, expires_at, dependencies, content_snapshot, "
        "plan_digest) VALUES ('plan-1', 'ws-synthetic', 'project', 'proj-erased', 7, "
        "'ordinary', %s, '2026-11-24T00:05:00Z', %s, %s, %s)",
        (TIMESTAMP, DEPENDENCIES, CONTENT_SNAPSHOT, "sha256:" + DIGEST),
    )
    execute(
        fixture,
        "INSERT INTO {s}.deletion_records VALUES ('del-1', 'ws-synthetic', 'plan-1', %s, %s)",
        ("sha256:" + DIGEST, TIMESTAMP),
    )
    receipts: tuple[tuple[str, str, str | None, str | None], ...] = (
        ("project.create", "k-erased", "proj-erased", None),
        ("project.create", "k-live", "proj-live", None),
        ("transcript.create", "k-transcript", None, "tr-1"),
    )
    for operation, key, project_id, transcript_id in receipts:
        execute(
            fixture,
            "INSERT INTO {s}.mutation_receipts (operation, scope_id, key_digest, intent_digest, "
            "project_id, project_revision, transcript_id, transcript_revision) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                operation,
                SCOPE,
                digest(key),
                DIGEST,
                project_id,
                7 if project_id else None,
                transcript_id,
                3 if transcript_id else None,
            ),
        )
    execute(
        fixture,
        "INSERT INTO {s}.erased_mutation_receipts "
        "(operation, scope_id, key_digest, intent_digest, deletion_id) "
        "VALUES ('project.create', %s, %s, %s, 'del-1')",
        (SCOPE, digest("k-erased"), DIGEST),
    )
    fixture.connection.commit()


def rows_with_types(fixture: Schema, text: str, parameters: tuple[object, ...]) -> Any:
    cursor = fixture.row(text, parameters)
    records = [dict(row) for row in cursor.fetchall()]
    types = [column.type_code for column in cursor.description or ()]
    return records, types


def parameters(key: str) -> tuple[object, ...]:
    operation = "transcript.create" if key == "k-transcript" else "project.create"
    return (operation, SCOPE, digest(key), operation, SCOPE, digest(key))


def test_both_forms_project_identical_receipt_rows_on_the_same_fixture(
    migrated: Schema,
) -> None:
    seed(migrated)

    for key in ("k-erased", "k-live", "k-transcript"):
        frozen, frozen_types = rows_with_types(migrated, FROZEN_QUERY, parameters(key))
        shipped, shipped_types = rows_with_types(migrated, _RECEIPT_UNION, parameters(key))
        # The comparison comes first so a broken statement fails on the rows it produced,
        # not on a shape assertion that could have been reworded.
        assert shipped == frozen, key
        assert shipped_types == frozen_types, key
        assert shipped_types == [TEXT_OID, TEXT_OID, NUMERIC_OID, TEXT_OID, NUMERIC_OID, BOOL_OID]

    absent = parameters("k-absent")
    assert rows_with_types(migrated, _RECEIPT_UNION, absent) == rows_with_types(
        migrated, FROZEN_QUERY, absent
    )
    assert rows_with_types(migrated, _RECEIPT_UNION, absent)[0] == []


def test_the_oracle_only_means_something_while_the_shipped_arm_has_no_vendor_type(
    migrated: Schema,
) -> None:
    seed(migrated)
    # A vendor type reintroduced here later would make both sides one statement again.
    assert _RECEIPT_UNION != FROZEN_QUERY
    assert "CAST(NULL AS text)" in FROZEN_QUERY and "CAST(NULL AS numeric)" in FROZEN_QUERY
    assert "CAST(NULL AS" not in _RECEIPT_UNION
    # ``erased`` is the only column that may be a literal in the second arm.
    assert "true AS erased" in _RECEIPT_UNION

    # Expected values reasoned from the fixture, so two copies of one wrong statement
    # cannot agree their way to green.
    erased = rows_with_types(migrated, _RECEIPT_UNION, parameters("k-erased"))[0]
    assert erased == [
        {
            "intent_digest": DIGEST,
            "project_id": None,
            "project_revision": None,
            "transcript_id": None,
            "transcript_revision": None,
            "erased": True,
        }
    ]
    live = rows_with_types(migrated, _RECEIPT_UNION, parameters("k-live"))[0]
    assert live == [
        {
            "intent_digest": DIGEST,
            "project_id": "proj-live",
            "project_revision": Decimal(7),
            "transcript_id": None,
            "transcript_revision": None,
            "erased": False,
        }
    ]
    transcript = rows_with_types(migrated, _RECEIPT_UNION, parameters("k-transcript"))[0]
    assert transcript == [
        {
            "intent_digest": DIGEST,
            "project_id": None,
            "project_revision": None,
            "transcript_id": "tr-1",
            "transcript_revision": Decimal(3),
            "erased": False,
        }
    ]


def test_the_erased_path_reads_through_a_plan_document_with_non_ascii_and_nested_arrays(
    migrated: Schema,
) -> None:
    seed(migrated)
    # The erased receipt is reachable only through its deletion record and plan, so a
    # plan document carrying non-ASCII text and nested arrays is the path, not decoration.
    cursor = execute(
        migrated,
        "SELECT p.dependencies::text AS dependencies, p.content_snapshot::text AS snapshot "
        "FROM {s}.deletion_plans p JOIN {s}.deletion_records r ON r.plan_id = p.id "
        "JOIN {s}.erased_mutation_receipts e ON e.deletion_id = r.id "
        "WHERE e.key_digest = %s",
        (digest("k-erased"),),
    )
    row = cursor.fetchone()
    assert row is not None
    assert json.loads(row["dependencies"])[1] == ["语音", ["甲", "乙"]]
    assert json.loads(row["snapshot"])["嵌套"] == [["甲", "乙"]]
    assert "项目甲" in row["dependencies"]
    # And the union still resolves that receipt to its erased identity.
    erased = rows_with_types(migrated, _RECEIPT_UNION, parameters("k-erased"))[0]
    assert [entry["erased"] for entry in erased] == [True]
