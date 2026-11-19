"""The console script prints the line, in a real process, with default logging.

``#1041``'s product is a log line, and every in-process test that reads it through
``caplog`` has already changed the root logger's level to ``INFO`` -- which is
exactly the defect this case exists to catch: the worker called no
``logging.basicConfig``, so ``INFO`` lines were dropped in a container and the one
``WARNING`` the alert used lost its fields to ``logging.lastResort``. That is why
the assertion here is made over a *subprocess*: a fresh interpreter, the installed
console script, the environment a deployment would set, and nothing but what the
process itself writes to its streams.

The child runs against the same real coordination database as the in-process case
and the same admission role, so a line here is the deployment's real answer.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO

import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import (
    runtime_database as runtime_database,
)
from tests.integration.test_public_job_admission_http_pg import HOST_NODE, WORKSPACE
from tests.integration.test_public_job_atomic_admission_pg import _seed

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

#: The console script the image installs; it must be the one under test, not a
#: module invoked through the interpreter that could differ from the entrypoint.
ENTRYPOINT = Path(sys.executable).with_name("speechnexus-admission-watch")


def _deployment_environment(admission: AdmissionDatabase) -> dict[str, str]:
    db = admission.runtime.foundation.database
    assert admission.admission_role is not None
    assert db.dialect == "postgresql"
    # The admission role the report reads through; the same key a deployment sets.
    dsn = make_conninfo(db.url, user=admission.admission_role, password="synthetic_foundation_test")
    return {
        **os.environ,
        "SPEECHNEXUS_ADMISSION_WATCH_ENABLED": "true",
        "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": dsn,
        "SPEECHNEXUS_DATABASE_SCHEMA": db.schema,
        "SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS": "30",
        "SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER": "3",
        "SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES": WORKSPACE,
    }


def _poll_once(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert ENTRYPOINT.is_file(), f"{ENTRYPOINT} is missing; run `uv sync --all-packages`"
    return subprocess.run(
        [str(ENTRYPOINT), "--once"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _only_line(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Exactly one JSON object on stderr and nothing on stdout."""
    assert completed.stdout == ""
    lines = [line for line in completed.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, completed.stderr
    document = json.loads(lines[0])
    assert isinstance(document, dict)
    return document


def test_the_installed_entrypoint_writes_one_json_line_per_poll(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    environment = _deployment_environment(admission_database)

    db.query(
        "UPDATE host_nodes SET node_state = 'quarantined' WHERE host_node_id = %s",
        (HOST_NODE,),
    )
    refused = _poll_once(environment)
    print(f"ENTRYPOINT degraded rc={refused.returncode} stderr={refused.stderr!r}")
    assert refused.returncode == 1
    document = _only_line(refused)
    assert document["event"] == "admission_watch_started"
    assert document["status"] == "degraded"
    assert document["cause"] == "engine_resource_disabled"
    assert document["workspace_id"] == WORKSPACE
    assert document["level"] == "INFO"
    assert document["logger"] == "speechnexus_backend.speech.application.admission_watch"
    assert isinstance(document["time"], str)

    db.query(
        "UPDATE host_nodes SET node_state = 'active' WHERE host_node_id = %s",
        (HOST_NODE,),
    )
    admitted = _poll_once(environment)
    print(f"ENTRYPOINT admitting rc={admitted.returncode} stderr={admitted.stderr!r}")
    # `--once` answers the same question the alert does: deployment-level refusal.
    assert admitted.returncode == 0
    document = _only_line(admitted)
    assert document["event"] == "admission_watch_started"
    assert document["status"] == "admitting"
    assert document["cause"] is None
    assert document["workspace_id"] == WORKSPACE


class _StderrPump:
    """Deliver the child's stderr lines as they arrive, without blocking a pipe."""

    def __init__(self, stream: IO[str]) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: IO[str]) -> None:
        for line in stream:
            self._lines.put(line)
        self._lines.put(None)

    def next_line(self, timeout: float) -> str:
        item = self._lines.get(timeout=timeout)
        assert item is not None, "the process closed stderr before the expected line"
        return item


def test_the_running_loop_writes_the_change_and_the_alert_through_the_formatter(
    admission_database: AdmissionDatabase,
) -> None:
    """The headline lines of a *running* process: ``changed`` and ``alerted``.

    ``--once`` cannot produce either (a fresh process only ever writes ``started``),
    so the loop is exercised with a wide interval: read the ``started`` line, flip the
    deployment fact, and the next poll must write both the change line and -- with a
    threshold of one -- the WARNING alert, fields intact.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    environment = _deployment_environment(admission_database)
    environment["SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS"] = "5"
    environment["SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER"] = "1"
    assert ENTRYPOINT.is_file(), f"{ENTRYPOINT} is missing; run `uv sync --all-packages`"
    process = subprocess.Popen(
        [str(ENTRYPOINT)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stderr is not None
    try:
        pump = _StderrPump(process.stderr)
        started = json.loads(pump.next_line(60))
        assert started["event"] == "admission_watch_started"
        assert started["status"] == "admitting"

        db.query(
            "UPDATE host_nodes SET node_state = 'quarantined' WHERE host_node_id = %s",
            (HOST_NODE,),
        )
        changed = json.loads(pump.next_line(60))
        alerted = json.loads(pump.next_line(60))
        print(f"LOOP changed={changed}")
        print(f"LOOP alerted={alerted}")
        assert changed["event"] == "admission_watch_changed"
        assert changed["workspace_id"] == WORKSPACE
        assert changed["from"] == {"status": "admitting", "cause": None}
        assert changed["to"] == {"status": "degraded", "cause": "engine_resource_disabled"}
        assert alerted["event"] == "admission_watch_alerted"
        assert alerted["level"] == "WARNING"
        assert alerted["cause"] == "engine_resource_disabled"
        assert alerted["consecutive_degraded"] == 1
        assert alerted["workspace_id"] == WORKSPACE
        assert isinstance(alerted["since_us"], int)
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
    assert process.returncode == 0
    assert process.stdout is not None
    assert process.stdout.read() == ""
