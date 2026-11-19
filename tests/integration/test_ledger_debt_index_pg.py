"""0061 adds the ledger's open-debt partial index the permanent debt read needs.

``cancellation_debt``'s fifth read cannot be bounded by time -- the debt it exists
for is the debt that outlived the 24 h window -- and it cannot walk
``speech_job_cancellation_reconciliations`` either, which keeps one row per accepted
cancellation for the deployment's whole life. The bound is the partial index: it
holds only the rows that can still describe held capacity, so its size follows the
open debt. 0061's downgrade must not silently remove that.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from psycopg import Connection
from tests.integration.test_public_job_progress_migration_pg import (
    _isolated_schema,
    _url,
)
from tests.support.migrations import (
    LEDGER_DEBT_INDEX,
    LEDGER_DEBT_INDEX_REVISION,
    LEFT_CANCELLING_INDEX_REVISION,
)

pytestmark = pytest.mark.integration


def _definition(query: Connection[dict[str, Any]]) -> str | None:
    row = query.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
        (LEDGER_DEBT_INDEX,),
    ).fetchone()
    return None if row is None else str(row["indexdef"])


def _revision(query: Connection[dict[str, Any]]) -> str:
    row = query.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row["version_num"])


def test_0061_indexes_only_the_ledgers_open_rows_and_refuses_its_own_downgrade() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, LEFT_CANCELLING_INDEX_REVISION)
        assert _definition(query) is None

        command.upgrade(config, "head")
        definition = _definition(query)
        assert definition is not None
        # The read selects by the ledger key and is bounded by this predicate alone.
        assert "(job_id)" in definition
        assert "WHERE" in definition
        assert "settled_at_us IS NULL" in definition
        assert "last_outcome" in definition
        assert "'exhausted'" in definition
        # The index is partial because it must not hold the settled history: without
        # the predicate it would be a copy of the whole ledger and the read's cost
        # would follow uptime again -- which is the counter-example this assertion
        # makes observable.
        assert "settled_at_us IS NOT NULL" not in definition

        # 0061's own refusal, not 0060's: the index is a runtime safety property.
        with pytest.raises(RuntimeError) as refused:
            command.downgrade(config, LEFT_CANCELLING_INDEX_REVISION)
        message = str(refused.value)
        assert "0061" in message
        assert LEDGER_DEBT_INDEX in message
        assert "permanent" in message

        # The refused downgrade leaves the head and the access path it ships intact.
        assert _revision(query) == LEDGER_DEBT_INDEX_REVISION
        assert _definition(query) == definition

        # And a later upgrade is still a no-op rather than a duplicate CREATE INDEX.
        command.upgrade(config, "head")
        assert _definition(query) == definition


def _size(query: Connection[dict[str, Any]]) -> int:
    row = query.execute(
        "SELECT pg_relation_size(c.oid) AS bytes FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s",
        (LEDGER_DEBT_INDEX,),
    ).fetchone()
    assert row is not None
    return int(row["bytes"])


def _seed(query: Connection[dict[str, Any]], *, prefix: str, settled: int, open_rows: int) -> None:
    """A ledger whose settled history is large and whose open debt is tiny."""
    digest = "sha256:" + "e" * 64
    total = settled + open_rows
    query.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) "
        "SELECT %s || '-' || lpad(i::text, 6, '0'), 'workspace-1', 'tts', 'tts-quality', %s, "
        "'cancelled', 3, 1, 1 FROM generate_series(1, %s) AS i",
        (prefix, digest, total),
    )
    query.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id) "
        "SELECT %s || '-' || lpad(i::text, 6, '0'), 1, 'request-' || %s || '-' || i "
        "FROM generate_series(1, %s) AS i",
        (prefix, prefix, total),
    )
    query.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts, "
        "next_check_at_us, last_outcome, settled_at_us, updated_at_us) "
        "SELECT %s || '-' || lpad(i::text, 6, '0'), 1, 1, 'settled', 1, 1 "
        "FROM generate_series(1, %s) AS i",
        (prefix, settled),
    )
    query.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts, "
        "next_check_at_us, last_outcome, settled_at_us, updated_at_us) "
        "SELECT %s || '-' || lpad((%s + i)::text, 6, '0'), 8, 0, 'exhausted', NULL, 1 "
        "FROM generate_series(1, %s) AS i",
        (prefix, settled, open_rows),
    )


def test_the_index_holds_the_open_debt_not_the_settled_history() -> None:
    """The partial predicate is what makes the read's cost follow the debt.

    A deployment with 2 000 settled cancellations and 3 permanently held slots must
    not carry a 2 003-entry index for the 5 s poll to walk: the index may only grow
    with the open rows. This is the size half of the counter-example -- dropping the
    ``WHERE`` turns the index into a copy of the ledger, and the assertion below is
    what makes that observable without timing a query.
    """
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _seed(query, prefix="hist", settled=2_000, open_rows=0)
        settled_only = _size(query)

        _seed(query, prefix="held", settled=0, open_rows=3)
        with_open = _size(query)

    # Three entries -- job ids of ten bytes -- fit in one leaf page. A non-partial
    # index over 2 003 rows cannot: its directory alone is tens of kilobytes.
    assert settled_only <= 16 * 1024
    assert with_open <= 24 * 1024
    assert with_open - settled_only <= 8 * 1024
