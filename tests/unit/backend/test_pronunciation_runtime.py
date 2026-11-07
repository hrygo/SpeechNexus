"""Publisher startup verification rejects a table UPDATE hidden behind columns.

A real PostgreSQL role with table-level UPDATE also makes
``has_column_privilege`` true for every column, so the integration test cannot
show which check rejected it. This unit test isolates the table-level branch: the
observed column set is exactly the declared one and no other privilege is held.
"""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.pronunciation_runtime import verify_publisher_role

_FAMILIES = "speech_pronunciation_families"
_REVISIONS = "speech_pronunciation_revisions"
_COMMANDS = "speech_pronunciation_commands"
_PUBLICATIONS = "speech_publications"
_HEADS = "speech_publication_heads"
_CATALOG_STATE = "speech_catalog_state"
_INSERT_TABLES = frozenset({_FAMILIES, _REVISIONS, _COMMANDS, _PUBLICATIONS, _HEADS})
_COLUMNS = {
    (_FAMILIES, "latest_revision"),
    (_COMMANDS, "command_state"),
    (_COMMANDS, "pronunciation_id"),
    (_COMMANDS, "revision"),
    (_COMMANDS, "error_code"),
    (_HEADS, "revision"),
    (_HEADS, "revision_id"),
    (_HEADS, "updated_at_us"),
    (_CATALOG_STATE, "revision"),
    (_CATALOG_STATE, "generated_at_us"),
}


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    """Dispatch by the marker each verifier query carries, not by call order."""

    def __init__(self, table_rows: list[dict[str, Any]]) -> None:
        self._table_rows = table_rows

    def execute(self, query: str, parameters: Any = None) -> _Result:
        if "rolsuper" in query:
            return _Result([{"forbidden": False}])
        if "can_select" in query:
            return _Result(self._table_rows)
        if "attname" in query:
            return _Result(
                [{"relname": table, "attname": column} for table, column in sorted(_COLUMNS)]
            )
        if "AS forbidden" in query:
            return _Result([{"forbidden": False}])
        return _Result([])


def _table_rows(**overrides: Any) -> list[dict[str, Any]]:
    rows = [
        {
            "relname": table,
            "can_select": True,
            "can_insert": table in _INSERT_TABLES,
            "can_update": False,
            "destructive": False,
            "column_reference": False,
        }
        for table in (
            _FAMILIES,
            _REVISIONS,
            _COMMANDS,
            _PUBLICATIONS,
            _HEADS,
            _CATALOG_STATE,
        )
    ]
    for row in rows:
        if row["relname"] in overrides:
            row.update(overrides[row["relname"]])
    return rows


def test_publisher_table_update_is_rejected_even_when_columns_match() -> None:
    connection = _Connection(_table_rows(**{_FAMILIES: {"can_update": True}}))
    with pytest.raises(ValueError, match="broad privilege"):
        verify_publisher_role(connection, "synthetic", "synthetic-publisher")  # type: ignore[arg-type]
