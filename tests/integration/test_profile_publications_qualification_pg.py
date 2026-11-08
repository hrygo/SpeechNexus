"""#415 isolated-PG gaps: concurrent publish, stale CAS no-write, column grants.

Complements ``test_profile_publications.py`` with assertions #415 only witnesses
indirectly there: two real publishers of one head, a fresh key on an already
published revision, the stale CAS's untouched head/history, and a refused column
update. It adds no product behavior.
Every success here is a synthetic TEST DOUBLE; none of it qualifies a real
deployment, a real publication authority or a real engine.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from tests.integration.test_profile_publications import Sandbox
from tests.integration.test_profile_publications import (  # noqa: F401
    sandbox as _profile_sandbox,
)

pytestmark = pytest.mark.integration


def isolated_sandbox(request: pytest.FixtureRequest) -> Sandbox:
    """A missing DSN fails this case; a skip would be non-evidence (AGENTS.md)."""
    if not os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL"):
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    value = request.getfixturevalue("_profile_sandbox")
    assert isinstance(value, Sandbox)
    return value


def wait_for_lock_waits(sandbox: Sandbox, expected: int, seconds: float = 3) -> list[str]:
    """Return the executing statements of that many actually blocked PG backends."""
    blocked: list[str] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rows = sandbox.admin.execute(
            "SELECT query FROM pg_stat_activity WHERE usename=%s "
            "AND cardinality(pg_blocking_pids(pid))>0",
            (sandbox.role,),
        ).fetchall()
        blocked = [row["query"] or "" for row in rows]
        if len(blocked) == expected:
            return blocked
        time.sleep(0.01)
    pytest.fail(f"Expected {expected} actual independent PG lock waits; observed {len(blocked)}")


def rows(sandbox: Sandbox, statement: str) -> list[dict[str, Any]]:
    return list(sandbox.admin.execute(statement).fetchall())


def test_competing_publications_of_one_head_serialize_to_one_winner(
    request: pytest.FixtureRequest,
) -> None:
    """Two writers publish the same head at once; exactly one publication exists."""
    s = isolated_sandbox(request)
    s.draft()
    commands = [s.publish(key="qualify-publish-alpha"), s.publish(key="qualify-publish-bravo")]
    blocked: list[str] = []
    blocker = psycopg.connect(s.url)
    try:
        blocker.execute(f'SET search_path TO "{s.schema}",pg_catalog')
        blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(s.port.mutate, command) for command in commands]
            try:
                blocked = wait_for_lock_waits(s, 2)
            finally:
                blocker.rollback()
            results = [future.result(timeout=5) for future in futures]
    finally:
        blocker.close()
    # The family row FOR UPDATE is the only serializing statement; prove the wait
    # came from it instead of an incidental same-value write.
    assert len(blocked) == 2
    assert all("FOR UPDATE" in query and "speech_profile_families" in query for query in blocked), (
        blocked
    )
    assert sorted(result.outcome for result in results) == ["completed", "rejected"]
    assert [result.error_code for result in results if result.outcome == "rejected"] == [
        "configuration_changed"
    ]
    assert len(rows(s, "SELECT * FROM speech_profile_publications")) == 1
    assert rows(s, "SELECT current_revision FROM speech_profile_families") == [
        {"current_revision": "1"}
    ]
    assert len(rows(s, "SELECT * FROM speech_profile_revisions")) == 1


def test_same_revision_under_a_new_key_is_not_a_replay_and_cannot_republish(
    request: pytest.FixtureRequest,
) -> None:
    """Replay is keyed by intent: a fresh key cannot publish the same revision again."""
    s = isolated_sandbox(request)
    s.draft()
    first = s.port.mutate(s.publish())
    assert first.outcome == "completed" and first.publication is not None
    again = s.port.mutate(s.publish("1", "1", "qualify-republish-key"))
    assert (again.outcome, again.error_code) == ("rejected", "profile_already_published")
    assert len(rows(s, "SELECT * FROM speech_profile_publications")) == 1
    assert len(rows(s, "SELECT * FROM speech_profile_revisions")) == 1


def test_stale_cas_rejection_leaves_head_and_history_untouched(
    request: pytest.FixtureRequest,
) -> None:
    """A stale activation records only its rejection; the pointer and history stand."""
    s = isolated_sandbox(request)
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    s.draft("2")
    assert s.port.mutate(s.publish("2", "1", "qualify-publish-two")).outcome == "completed"
    assert s.port.mutate(s.activate("1", "2", "qualify-activate-one")).outcome == "completed"
    before = s.snapshot()
    stale = s.activate("2", "2", "qualify-stale-key")
    result = s.port.mutate(stale)
    assert (result.outcome, result.error_code) == ("rejected", "configuration_changed")
    after = s.snapshot()
    for table in (
        "speech_profile_families",
        "speech_profile_revisions",
        "speech_profile_publications",
    ):
        assert after[table] == before[table], table
    rejected = [
        row
        for row in after["speech_profile_lifecycle_results"]
        if row["key_digest"] == stale.key_digest
    ]
    assert rejected == [
        {
            "key_digest": stale.key_digest,
            "error_code": "configuration_changed",
            "result_json": None,
            "result_digest": None,
        }
    ]
    assert (
        len(after["speech_profile_lifecycle_results"])
        == len(before["speech_profile_lifecycle_results"]) + 1
    )
    # The pointer never advanced: a CAS against the still-current revision succeeds,
    # and the failed command keeps returning its stored original rejection.
    assert s.port.mutate(s.activate("2", "1", "qualify-after-stale")).outcome == "completed"
    assert s.port.mutate(stale).error_code == "configuration_changed"


def test_missing_column_and_table_grants_refuse_before_any_write(
    request: pytest.FixtureRequest,
) -> None:
    """The real LOGIN writer has only the granted columns; denied statements write nothing."""
    s = isolated_sandbox(request)
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    before = s.snapshot()
    denied = (
        # profile_draft_grants grants UPDATE(latest_revision); profile_publication_grants
        # grants UPDATE(current_revision); every other families column is not updatable.
        "UPDATE speech_profile_families SET profile_id='forged'",
        # publications grants SELECT/INSERT only; no UPDATE on any column.
        "UPDATE speech_profile_publications SET status='retired', retired_at_us=1",
        "UPDATE speech_profile_lifecycle_results SET error_code='changed'",
    )
    for statement in denied:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), s.pool.connection() as connection:
            connection.execute(statement)
    assert s.snapshot() == before
