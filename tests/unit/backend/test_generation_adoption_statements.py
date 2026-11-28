"""The adoption audit's column list has one definition point, and this checks it.

``generation_adoption_schema`` declares the table, derives the insert column list from
it, and ``generation_adoption`` writes a literal INSERT (the restricted SQL subset forbids
building SQL with f-strings). The expected values below are written **independently**,
not derived from the module: a check that reads its expectation from the thing it checks
drifts with it and can never turn red.

Mutations that must turn this module red:

- renaming, reordering or dropping a column in the schema declaration;
- adding a column to the declaration without adding it to the writer's INSERT;
- changing the audit table's name in one place but not the other;
- granting ``UPDATE``/``DELETE`` to the adoption writer (the rows are append-only);
- dropping a CHECK that keeps an adoption from moving the ledger backwards.
"""

from __future__ import annotations

import re

from speechnexus_backend.generation_adoption import _AUDIT_INSERT, _AUDIT_READ
from speechnexus_backend.speech.infrastructure.generation_adoption_grants import (
    generation_adoption_reader_grants,
    generation_adoption_writer_grants,
)
from speechnexus_backend.speech.infrastructure.generation_adoption_schema import (
    GENERATION_ADOPTION_COLUMNS,
    GENERATION_ADOPTION_DDL,
    GENERATION_ADOPTION_PRIMARY_KEY,
    GENERATION_ADOPTION_TABLE,
)

#: 独立写一遍,不 import 上面的定义:判据与实现同源时注入永远不红。
EXPECTED_COLUMNS = (
    "instance_id",
    "adopted_generation",
    "ledger_generation",
    "node_high_water",
    "node_last_fencing_token",
    "previous_stop_evidence",
    "operator_id",
    "adopted_at_us",
    "document",
)
EXPECTED_TABLE = "runtime_generation_adoptions"


def test_the_declaration_names_the_columns_in_this_order() -> None:
    assert GENERATION_ADOPTION_COLUMNS == EXPECTED_COLUMNS
    assert GENERATION_ADOPTION_TABLE == EXPECTED_TABLE


def test_the_create_table_states_every_column_once() -> None:
    assert len(GENERATION_ADOPTION_DDL) == 1, "one CREATE TABLE, not a script of DDL"
    ddl = GENERATION_ADOPTION_DDL[0]
    assert ddl.startswith(f"CREATE TABLE IF NOT EXISTS {EXPECTED_TABLE} (")
    body = ddl.split("(", 1)[1]
    declared = re.findall(r"^\s{4}([a-z_]+)\s", body, re.MULTILINE)
    assert tuple(declared) == EXPECTED_COLUMNS
    assert f"PRIMARY KEY ({', '.join(GENERATION_ADOPTION_PRIMARY_KEY)})" in ddl


def test_the_table_refuses_an_adoption_that_moves_the_ledger_backwards() -> None:
    ddl = GENERATION_ADOPTION_DDL[0]
    # Every guard whose absence would let a replayed or crossed audit row exist.
    for fact in (
        "CHECK (adopted_generation > ledger_generation)",
        "CHECK (ledger_generation >= 1)",
        "CHECK (node_high_water >= adopted_generation)",
        "CHECK (node_last_fencing_token >= 1)",
        "CHECK (adopted_at_us >= 1)",
        "REFERENCES engine_instances (instance_id, instance_generation)",
    ):
        assert fact in ddl, f"the audit table must keep {fact}"


def test_the_writer_inserts_every_declared_column_in_order() -> None:
    columns = _AUDIT_INSERT.split("(", 1)[1].split(")", 1)[0]
    written = tuple(part.strip() for part in columns.split(","))
    assert written == EXPECTED_COLUMNS
    assert _AUDIT_INSERT.startswith(f"INSERT INTO {EXPECTED_TABLE} (")
    # One placeholder per declared column, so a column added to the list cannot silently
    # shift the values tuple.
    assert _AUDIT_INSERT.count("?") == len(EXPECTED_COLUMNS) == 9
    assert _AUDIT_READ.startswith(f"SELECT * FROM {EXPECTED_TABLE} ")


def test_the_writer_grants_append_only() -> None:
    statements = generation_adoption_writer_grants("speech", "speechnexus_operator")
    assert len(statements) == 2
    insert = statements[1]
    assert insert.startswith("GRANT INSERT (")
    granted = insert.split("(", 1)[1].split(")", 1)[0]
    assert tuple(part.strip() for part in granted.split(",")) == EXPECTED_COLUMNS
    assert EXPECTED_TABLE in insert
    joined = " ".join(statements).upper()
    for forbidden in ("UPDATE", "DELETE", "TRUNCATE", "ALTER"):
        assert f" {forbidden}" not in joined


def test_the_reader_can_only_select() -> None:
    statements = generation_adoption_reader_grants("speech", "speechnexus_audit")
    assert any(statement.startswith("GRANT SELECT ON") for statement in statements)
    assert not any("INSERT" in statement for statement in statements)


def test_the_grants_reject_an_injected_identifier() -> None:
    # The schema and role are interpolated into the grant text, so an attacker-controlled
    # value must not pass as an identifier.
    for bad in ("speech; DROP TABLE x", '"speech"', "speech schema"):
        try:
            generation_adoption_writer_grants(bad, "speechnexus_operator")
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be rejected as a schema name")
