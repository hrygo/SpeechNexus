"""Capture targets only for the worker's gateway and preserve the first timeout."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import psycopg
import pytest
from psycopg.rows import dict_row
from tests.integration.test_binding_disable_intent_pg import _command
from tests.integration.test_binding_lifecycle_pg import Sandbox, _seed
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.integration.test_coordination_foundation import seed
from tests.integration.test_production_jobs import JobDatabase

from speechnexus_backend.speech.infrastructure.binding_disable_targets_postgres import (
    capture_disable_targets,
)

pytestmark = pytest.mark.integration


def test_capture_is_scoped_and_concurrent_config_uses_original_timeout(sandbox: Sandbox) -> None:
    _seed(sandbox)

    class SeedDatabase:
        def query(self, sql: str, params: tuple[object, ...] = ()) -> Any:
            return sandbox.admin.execute(sql, params)

    seed(cast(JobDatabase, SeedDatabase()))
    sandbox.admin.execute("UPDATE admission_attempts SET binding_revision_id='binding-rev-2'")
    sandbox.lifecycle.retire(_command())
    row = sandbox.admin.execute("SELECT event_id FROM speech_binding_disable_intents").fetchone()
    assert row is not None and isinstance(row[0], str)
    event_id = row[0]
    with sandbox.admin.transaction(), sandbox.admin.cursor(row_factory=dict_row) as cursor:
        assert (
            capture_disable_targets(
                cursor,
                event_id,
                wait_timeout_ms=1000,
                limit=1,
                gateway_id="gateway-other",
                node_id="host-other",
            )
            == []
        )
    acquired, release = threading.Event(), threading.Event()
    holder_pid: list[int] = []

    def capture(timeout: int, hold: bool) -> Any:
        with psycopg.connect(sandbox.url, row_factory=dict_row) as connection:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            with connection.cursor() as cursor:
                result = capture_disable_targets(
                    cursor,
                    event_id,
                    wait_timeout_ms=timeout,
                    limit=1,
                    gateway_id="gateway-test",
                    node_id="host-test",
                )
                if hold:
                    holder_pid.append(connection.info.backend_pid)
                    acquired.set()
                    if not release.wait(8):
                        raise RuntimeError("capture was not released")
                return result

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(capture, 1000, True)
        assert acquired.wait(5)
        second = workers.submit(capture, 2000, False)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if sandbox.admin.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (holder_pid[0],),
                ).fetchone():
                    break
                time.sleep(0.01)
            else:
                pytest.fail("second target capture did not wait for the original transaction")
        finally:
            release.set()
        original, replay = first.result(10), second.result(10)
    assert len(original) == len(replay) == 1
    assert original[0] == replay[0]
    assert original[0].wait_timeout_ms == 1000
    assert original[0].submit_intent_id == "submit-test"
