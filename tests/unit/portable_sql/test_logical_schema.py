"""逻辑 schema:类型白名单、引用完整性、指纹稳定性与 NULL 性进入指纹."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from speechnexus_portable_sql import SchemaDefinitionError, fingerprint, load_logical_schema
from speechnexus_portable_sql.logical_schema import LogicalSchema

LOGICAL = Path("packages/portable-sql/schema/logical.json")


def write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "logical.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def table(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "schema_meta",
        "columns": [
            {"name": "meta_id", "type": "varchar(32)", "nullable": False},
            {"name": "authority_epoch", "type": "decimal(38,0)", "nullable": False},
        ],
        "primary_key": ["meta_id"],
    }
    payload.update(overrides)
    return payload


def payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"revision": "0002", "tables": [table()]}
    body.update(overrides)
    return body


def test_the_committed_logical_schema_is_canonical() -> None:
    schema = load_logical_schema(LOGICAL)
    assert schema.revision == "0002"
    column = schema.table("schema_meta")
    assert column is not None
    assert [item.name for item in column.columns] == [
        "meta_id",
        "schema_rev",
        "schema_fingerprint",
        "authority_epoch",
    ]
    assert len(fingerprint(schema)) == 64


@pytest.mark.parametrize(
    "column_type",
    ["text", "varchar(0)", "decimal(38,2)", "timestamp", "boolean", "jsonb", "varchar(4001)"],
)
def test_non_canonical_types_are_rejected(tmp_path: Path, column_type: str) -> None:
    bad = payload(
        tables=[
            table(
                columns=[
                    {"name": "meta_id", "type": column_type, "nullable": False},
                ]
            )
        ]
    )
    with pytest.raises(SchemaDefinitionError, match=r"non-canonical type|outside 1"):
        load_logical_schema(write(tmp_path, bad))


@pytest.mark.parametrize("identifier", ["Schema_Meta", "schema-meta", "9schema", "a" * 31])
def test_identifiers_are_ascii_lowercase_and_bounded(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(SchemaDefinitionError, match="identifier"):
        load_logical_schema(write(tmp_path, payload(tables=[table(name=identifier)])))


def test_key_columns_must_exist(tmp_path: Path) -> None:
    with pytest.raises(SchemaDefinitionError, match="unknown"):
        load_logical_schema(write(tmp_path, payload(tables=[table(primary_key=["absent"])])))


def test_foreign_keys_must_resolve(tmp_path: Path) -> None:
    child = table(
        name="child",
        columns=[{"name": "meta_id", "type": "varchar(32)", "nullable": False}],
        foreign_keys=[
            {"columns": ["meta_id"], "references": {"table": "parent", "columns": ["meta_id"]}}
        ],
    )
    with pytest.raises(SchemaDefinitionError, match="unknown table"):
        load_logical_schema(write(tmp_path, payload(tables=[table(), child])))


def test_duplicate_tables_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SchemaDefinitionError, match="duplicate table"):
        load_logical_schema(write(tmp_path, payload(tables=[table(), table()])))


def test_nullability_is_part_of_the_fingerprint() -> None:
    schema = load_logical_schema(LOGICAL)
    relaxed = LogicalSchema(
        schema.revision,
        tuple(
            type(item)(
                item.name,
                tuple(
                    type(column)(
                        column.name, column.type, column.nullable or column.name == "meta_id"
                    )
                    for column in item.columns
                ),
                item.primary_key,
                item.uniques,
                item.foreign_keys,
                item.checks,
                item.indexes,
            )
            for item in schema.tables
        ),
    )
    assert fingerprint(relaxed) != fingerprint(schema)


def test_fingerprint_is_stable_across_runs() -> None:
    first = fingerprint(load_logical_schema(LOGICAL))
    second = fingerprint(load_logical_schema(LOGICAL))
    assert first == second
