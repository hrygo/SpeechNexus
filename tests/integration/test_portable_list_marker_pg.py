"""Real PostgreSQL: one `?` binds a runtime-sized set.

`= ANY(array)` is outside the portable subset, and a fixed-arity `IN (?, ?, ?)` cannot
express a cleanup batch whose size is only known at run time. The expansion therefore
happens in the layer that owns marker syntax; this exercise proves the real driver
round-trips the expanded statement and returns exactly the named rows.
"""

from __future__ import annotations

import os

import pytest
from psycopg.rows import dict_row

from speechnexus_portable_sql.contract import ListMarker
from speechnexus_portable_sql.psycopg_driver import connect

DSN = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DSN, reason="real database DSN not configured")

_NAMES = (
    "SELECT CAST(? AS text) AS name UNION ALL SELECT CAST(? AS text) "
    "UNION ALL SELECT CAST(? AS text)"
)


def test_a_runtime_sized_set_binds_through_the_driver() -> None:
    with connect(DSN, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"SELECT name FROM ({_NAMES}) AS t WHERE name IN (?) ORDER BY name",
            ("a", "b", "c", ListMarker(["a", "c"])),
        )
        assert [row["name"] for row in cursor.fetchall()] == ["a", "c"]


def test_an_empty_batch_is_refused_before_the_statement_is_sent() -> None:
    # 空批次不展开成 `IN ()` 也不猜「匹配不到任何行」:调用方应当根本不发这条语句。
    with (
        connect(DSN, row_factory=dict_row) as connection,
        connection.cursor() as cursor,
        pytest.raises(ValueError, match="at least one value"),
    ):
        cursor.execute(
            f"SELECT name FROM ({_NAMES}) AS t WHERE name IN (?)",
            ("a", "b", "c", ListMarker([])),
        )
