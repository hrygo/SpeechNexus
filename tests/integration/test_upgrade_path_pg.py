"""#1058: the upgrade checklist's item 2 is an assertion, not a paragraph.

`docs/releases/unreleased.md` tells a deployment that is still on `v0.1.0` to re-run
`cancellation_debt_reader_grants`, because that grant widened the reconciliation ledger
from four columns to five (`attempts`). The instruction came from one manual run: nothing
replayed the real upgrade shape -- the **old ACL on the new schema** -- so the next edit to
the function, to the snippet, or to the statement the grant protects could rot the page
silently, and an operator following it would still get a degraded read face.

This module replays that shape on a real PostgreSQL at the shipped migration head:

1. provision the read-only debt identity through the reference provisioning (the same
   `cancellation_debt_reader_grants` the deployment runs), then `REVOKE SELECT (attempts)`
   -- exactly the four-column ACL a `v0.1.0` deployment still holds after the new
   migrations ran;
2. assert the symptom with the **production face**, not a copy of it: the probe's
   `attempts`-reading statements fail with `SQLSTATE 42501`, and `/readyz`'s
   `cancellation_debt` detail is `unavailable (permission)`; the identity then issues the
   same five columns directly, which is what reproduces the error text the page cites;
3. assert that it is **still** broken before the re-run -- without this line the module
   would pass in a world where the re-run was never needed, which is the whole risk
   #1058 names;
4. execute the checklist's own snippet, read out of the page verbatim, and assert the
   read face recovers -- including the ledger's `attempts` fact the missing grant hid;
5. run the page's own `curl` line against a loopback `/readyz` and check it prints the
   same sentence, so the verification command is exercised too, not just described.

Because the snippet is read from the page and executed by a shell, this module fails when
the page's command stops working, when it stops granting `attempts`, and when it is
replaced by something that only looks like a fix (`SELECT 1`).

Boundaries: synthetic roles and rows on an isolated database. The shell block runs
`uv run --all-packages python` exactly as the page prints it, with the two DSNs the page
requires already exported and `UV_NO_SYNC=1` in the environment, so the nested `uv` cannot
rewrite `uv.lock` or sync an environment the page never promised to prepare. The page's
`deploy/compose.env` prelude is the compose deployment's input, not this claim, and is not
replayed. `/readyz` here is the production route factory on loopback, not a live API
process. Nothing here is engine, gateway or object-storage evidence.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg.conninfo import conninfo_to_dict
from tests.integration.test_cancel_failure_slot_pg import (
    LEASE,
    PASSWORD,
    QUARANTINED_DOCUMENT_STATE,
    CancelSandbox,
    _database_now,
    _debt_probe,
    _sandbox,
)
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.application.readiness import (
    ProbeResult,
    ProbeStatus,
    ReadinessService,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    RECONCILIATION_TABLE,
)
from speechnexus_backend.speech.interfaces.http.health import create_health_router

pytestmark = [pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = ROOT / "docs/releases/unreleased.md"
ITEM_2 = "### 2. "
#: What `/readyz` says when the probe identity cannot read the ledger's fifth column.
UNREADABLE = "cancellation_debt: unavailable (permission)"
#: The columns `v0.1.0`'s grant covered; `attempts` is the fifth this slice adds.
V010_COLUMNS = ("job_id", "settled_at_us", "last_outcome", "next_check_at_us")
#: A claim time in the past keeps the seeded row ``due now``, so the recovered sentence
#: carries no wall-clock arithmetic and two reads of the same row are equal.
DUE_US = 1_000_000
#: ``attempts`` at the storage bound: the recovered sentence then prints the counter fact
#: (``retrying at attempt ceiling``), which is the value the missing grant hid. A mid-range
#: count would still be read and still fail under the old ACL, but it would not appear in
#: the detail, so the assertion could not show the fifth column arriving.
ATTEMPTS = 64
SHELL_TIMEOUT_SECONDS = 300


# --------------------------------------------------------------------------------------
# The checklist, read as the executable artifact it claims to be
# --------------------------------------------------------------------------------------


def _checklist_block(heading: str, first_line: str) -> str:
    """One fenced shell block, verbatim, out of the page that promises it is pasteable.

    Selecting by the block's own first line (instead of "the second block") keeps this
    honest when the page grows another snippet: a block that no longer starts the way the
    page said is a failure here, not a silently different command.
    """
    text = CHECKLIST.read_text(encoding="utf-8")
    start = text.find(heading)
    assert start >= 0, f"{CHECKLIST} no longer has the section {heading!r}"
    end = text.find("\n### ", start + len(heading))
    section = text[start:] if end < 0 else text[start:end]
    blocks: list[str] = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
    for body in blocks:
        block = body.rstrip("\n")
        if block.lstrip().startswith(first_line):
            return block
    raise AssertionError(
        f"{CHECKLIST} section {heading!r} no longer has a shell block starting with "
        f"{first_line!r}; the page's promise is that this block is pasteable"
    )


def _run_shell(block: str, **environment: str) -> subprocess.CompletedProcess[str]:
    """Run one checklist block as a shell would, with the DSNs the page asks for."""
    return subprocess.run(
        ["bash", "-c", block],
        cwd=ROOT,
        # ``UV_NO_SYNC`` keeps the nested ``uv`` from doing what the page does not promise:
        # measured, without it a stale lock makes ``uv run`` re-resolve and **rewrite
        # uv.lock**; with it the lock is untouched, and an environment that cannot run the
        # block fails at the block's own import instead of being silently built. A lock that
        # is already stale when the suite starts is caught upstream by ``uv sync --locked``.
        env={**os.environ, "UV_NO_CONFIG": "1", "UV_NO_SYNC": "1", **environment},
        text=True,
        capture_output=True,
        timeout=SHELL_TIMEOUT_SECONDS,
    )


# --------------------------------------------------------------------------------------
# The upgrade shape: a `v0.1.0` ACL on a head schema
# --------------------------------------------------------------------------------------


def _probe_url(database_url: str, role: str) -> str:
    """The probe identity as a URL DSN, the form the page's snippet parses a role out of.

    Every field of the configured DSN is carried over as a query item instead of rebuilding
    host/port/dbname by hand: ``sslmode``, ``application_name``, ``options`` and a Unix-socket
    directory all survive, so a deployment that requires TLS is still required to use it here.
    Dropping one would silently test a different connection than the deployment opens.
    """
    parts = conninfo_to_dict(database_url)
    parts.pop("user", None)
    parts.pop("password", None)
    database = str(parts.pop("dbname", ""))
    query = urlencode({key: str(value) for key, value in sorted(parts.items())})
    suffix = f"?{query}" if query else ""
    return f"postgresql://{quote(role)}:{quote(PASSWORD)}@/{quote(database)}{suffix}"


def _readable_columns(box: CancelSandbox) -> set[str]:
    rows = box.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "AND has_column_privilege(%s, %s, column_name, 'SELECT')",
        (box.schema, RECONCILIATION_TABLE, box.role, f"{box.schema}.{RECONCILIATION_TABLE}"),
    )
    return {str(row["column_name"]) for row in rows}


def _downgrade_to_v010(box: CancelSandbox) -> None:
    """Leave exactly the four ledger columns `v0.1.0` provisioned.

    The grant is column-level, so the whole difference between the two releases is one
    column: revoking ``attempts`` restores the ACL an upgraded deployment still holds.
    """
    box.execute(
        f'REVOKE SELECT (attempts) ON "{box.schema}".{RECONCILIATION_TABLE} FROM "{box.role}"'
    )
    assert _readable_columns(box) == set(V010_COLUMNS), _readable_columns(box)


@contextmanager
def _upgraded() -> Iterator[CancelSandbox]:
    """One Job, one held lease and one ledger row on a head schema, read by a `v0.1.0` ACL."""
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), debt_reader=True, coordination=False) as box:
        now = _database_now(box)
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) VALUES (%s, %s, %s, "
            "'stop_unconfirmed', NULL, %s)",
            (box.job_id, ATTEMPTS, now - DUE_US, now),
        )
        _downgrade_to_v010(box)
        yield box


# --------------------------------------------------------------------------------------
# The production read face, on both the probe and `/readyz`
# --------------------------------------------------------------------------------------


def _detail(box: CancelSandbox) -> str:
    detail = asyncio.run(_debt_probe(box).check())
    assert detail is not None, "a seeded held acceptance always has a detail"
    return detail


def _readyz_detail(box: CancelSandbox) -> ProbeResult:
    """The check `/readyz` would print, through the production readiness service."""
    service = ReadinessService([_debt_probe(box)], timeout_seconds=5)
    report = asyncio.run(service.inspect())
    return next(check for check in report.checks if check.name == "cancellation_debt")


def _denied_statement(box: CancelSandbox) -> str:
    """The five columns the probe reads, issued directly by the probe identity.

    Shaped like the probe instead of being its statement: the probe's whole sentence is
    ``_DEBT``, while this is the ledger slice of it. It exists to reproduce the error text
    the page quotes; that the probe itself degrades is asserted separately, above.
    """
    dsn = _probe_url(os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], box.role)
    with portable_pg_connect(dsn) as connection:
        connection.execute(f'SET search_path TO "{box.schema}", pg_catalog')
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
            connection.execute(
                f"SELECT {', '.join(V010_COLUMNS)}, attempts "
                f'FROM "{box.schema}".{RECONCILIATION_TABLE}'
            )
    assert denied.value.sqlstate == "42501", denied.value.sqlstate
    return str(denied.value)


@contextmanager
def _loopback_readyz(box: CancelSandbox) -> Iterator[int]:
    """Serve the production `/readyz` route on loopback and yield its port."""
    readiness = ReadinessService([_debt_probe(box)], timeout_seconds=5)
    app = FastAPI()
    app.include_router(
        create_health_router(
            service_name="speechnexus", version="upgrade-path", readiness=readiness
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "the loopback readiness server never started"
            yield port
        finally:
            server.should_exit = True
            worker.join(15)


# --------------------------------------------------------------------------------------
# The replay
# --------------------------------------------------------------------------------------


def test_a_v010_ledger_acl_is_broken_until_the_checklist_snippet_runs() -> None:
    """Red, then still red, then the page's own command, then green -- in that order."""
    with _upgraded() as box:
        # 2. The symptom, on the production faces. `/readyz` keeps answering 200: the
        # probe never fails readiness, it reports why it could not read.
        assert _detail(box) == UNREADABLE
        check = _readyz_detail(box)
        assert check.status is ProbeStatus.OK
        assert check.detail == UNREADABLE
        denied = _denied_statement(box)
        assert "permission denied for table speech_job_cancellation_reconciliations" in denied

        # Between step 2 and step 3, read the same face and the same ACL once more. This is
        # a restatement, not a necessity proof: step 2 already read the broken detail and
        # ``_downgrade_to_v010`` already proved the ACL is the four-column one, so deleting
        # these two lines leaves the module green.
        #
        # The necessity proof is the other mutation: delete step 3 alone and the assertion
        # after it reds while the broken state stays green, because nothing before the
        # command can make the face readable. Do not copy this shape elsewhere as if a
        # second "still broken" read were what proves a repair is required -- it is not.
        assert _detail(box) == UNREADABLE
        assert "attempts" not in _readable_columns(box)

        # 3. The page's command, executed verbatim. It derives the role from the probe DSN
        # and the schema from `SN_SCHEMA`, and prints which identity it re-granted.
        grant = _checklist_block(ITEM_2, "uv run --all-packages python")
        result = _run_shell(
            grant,
            SN_ADMIN_DSN=os.environ["SPEECHNEXUS_TEST_DATABASE_URL"],
            SPEECHNEXUS_READINESS_CANCELLATION_DSN=_probe_url(
                os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], box.role
            ),
            SN_SCHEMA=box.schema,
        )
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

        # After step 3: green, and the detail proves it read the column the old ACL hid.
        # The seeded ledger row's ``attempts`` is at the storage bound, so the recovered
        # sentence names the counter fact. Nothing here merely says "no longer an error".
        recovered = _detail(box)
        assert recovered != UNREADABLE, (
            "the checklist command did not restore the read face; the detail is still "
            f"{UNREADABLE!r}"
        )
        assert recovered.startswith("cancellation_debt: 1 accepted cancellation(s)")
        assert "due now: 1" in recovered
        assert "retrying at attempt ceiling: 1" in recovered
        assert box.job_id in recovered
        # The page promises it grants the identity named by the probe DSN, not a neighbour:
        # the block says which role it re-granted, so the replay can check that it was ours.
        assert box.role in result.stdout, result.stdout

        # The page's own verification line, against a real `/readyz` on the host port the
        # page now derives from `${SPEECHNEXUS_API_HOST_PORT:-8000}`.
        verify = _checklist_block(ITEM_2, "curl -sS")
        with _loopback_readyz(box) as port:
            printed = _run_shell(verify, SPEECHNEXUS_API_HOST_PORT=str(port))
        assert printed.returncode == 0, printed.stderr
        assert printed.stdout.strip() == recovered, printed.stdout
