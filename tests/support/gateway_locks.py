"""Observe real gateway row-lock waits; elapsed time alone is never concurrency evidence."""

import time

import pytest

from tests.integration.test_coordination_foundation import Foundation


def wait_for_gateway_locks(foundation: Foundation, table: str, *, minimum: int = 1) -> None:
    assert table in {"resource_pools", "submit_intents", "output_stream_heads"}
    db = foundation.database
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if db.dialect == "postgresql":
            rows = db.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = %s "
                "AND cardinality(pg_blocking_pids(pid)) > 0 AND query LIKE %s",
                (foundation.gateway_role, f"%FROM {table}%"),
            )
        else:
            rows = db.query(
                "SELECT count(*) AS n FROM performance_schema.data_lock_waits w JOIN "
                "performance_schema.data_locks l "
                "ON w.REQUESTING_ENGINE_LOCK_ID = l.ENGINE_LOCK_ID "
                "WHERE l.OBJECT_SCHEMA = %s AND l.OBJECT_NAME = %s",
                (db.schema, table),
            )
        if rows[0]["n"] >= minimum:
            return
        time.sleep(0.01)
    pytest.fail(f"No observed {table} row-lock wait for {minimum} gateway writers")
