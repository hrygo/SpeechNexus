"""真实 PG oracle:`insert_if_absent` 冲突后事务仍可用,且能读回已有行."""

from __future__ import annotations

import os

import pytest

from speechnexus_portable_sql.psycopg_driver import connect
from speechnexus_portable_sql.transaction import insert_if_absent

pytestmark = [pytest.mark.integration]


def _dsn() -> str:
    value = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL", "")
    if not value:
        pytest.fail(
            "Database integration cases must fail instead of skipping: "
            "SPEECHNEXUS_TEST_DATABASE_URL is not set"
        )
    return value


def test_insert_then_conflict_without_losing_the_transaction() -> None:
    table = "portable_insert_if_absent_probe"
    with connect(_dsn()) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("DROP TABLE IF EXISTS " + table)
            cursor.execute("CREATE TABLE " + table + " (probe_id VARCHAR(32) PRIMARY KEY)")
            connection.commit()

            cursor.execute("BEGIN")
            assert insert_if_absent(cursor, f"INSERT INTO {table} (probe_id) VALUES (?)", ("a",))
            # 同一事务里再插同一主键:唯一冲突,保存点回滚后事务必须仍然可用。
            assert not insert_if_absent(
                cursor, f"INSERT INTO {table} (probe_id) VALUES (?)", ("a",)
            )
            cursor.execute(f"SELECT probe_id FROM {table} WHERE probe_id = ?", ("a",))
            assert cursor.fetchone() == ("a",)
            connection.rollback()
        finally:
            cursor.execute("DROP TABLE IF EXISTS " + table)
            connection.commit()
            cursor.close()
