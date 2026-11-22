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

VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _dsn() -> str:
    """缺 DSN 时失败而不是跳过:跳过不会被当成证据。"""

    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not portable-SQL evidence")
    return url


_NAMES = (
    "SELECT CAST(? AS text) AS name UNION ALL SELECT CAST(? AS text) "
    "UNION ALL SELECT CAST(? AS text)"
)


def test_a_runtime_sized_set_binds_through_the_driver() -> None:
    with connect(_dsn(), row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"SELECT name FROM ({_NAMES}) AS t WHERE name IN (?) ORDER BY name",
            ("a", "b", "c", ListMarker(["a", "c"])),
        )
        assert [row["name"] for row in cursor.fetchall()] == ["a", "c"]


def test_an_empty_batch_is_refused_before_the_statement_is_sent() -> None:
    # 空批次不展开成 `IN ()` 也不猜「匹配不到任何行」:调用方应当根本不发这条语句。
    with (
        connect(_dsn(), row_factory=dict_row) as connection,
        connection.cursor() as cursor,
        pytest.raises(ValueError, match="at least one value"),
    ):
        cursor.execute(
            f"SELECT name FROM ({_NAMES}) AS t WHERE name IN (?)",
            ("a", "b", "c", ListMarker([])),
        )


async def test_a_list_marker_expands_on_the_async_path_too() -> None:
    # workbench 走异步连接;同步路径的展开在这里必须同样发生,否则标记对象会直接交给驱动。

    from speechnexus_portable_sql.psycopg_driver import connect_async

    connection = await connect_async(_dsn(), row_factory=dict_row)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT name FROM ({_NAMES}) AS t WHERE name IN (?) ORDER BY name",
                ("a", "b", "c", ListMarker(["b"])),
            )
            assert [row["name"] for row in await cursor.fetchall()] == ["b"]
    finally:
        await connection.close()


async def test_a_list_marker_expands_inside_a_composed_statement() -> None:
    # workbench 的语句是 `sql.SQL(...).format(s=schema)` 拼出来的 `Composed`:
    # 标记只出现在 SQL 片段里,展开必须按片段顺序消耗参数,标识符片段保持原样。
    from psycopg import sql

    from speechnexus_portable_sql.psycopg_driver import connect_async

    connection = await connect_async(_dsn(), row_factory=dict_row)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                sql.SQL("SELECT name FROM ({}) AS t WHERE name IN (?) ORDER BY name").format(
                    sql.SQL(_NAMES)
                ),
                ("a", "b", "c", ListMarker(["a", "c"])),
            )
            assert [row["name"] for row in await cursor.fetchall()] == ["a", "c"]
    finally:
        await connection.close()
