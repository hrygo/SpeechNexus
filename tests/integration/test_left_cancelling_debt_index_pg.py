"""0060 adds the fence-age index the Job-left-cancelling debt read needs.

``cancellation_debt`` cannot reach a Job that already left ``cancelling`` through
``speech_jobs_live_state_scan`` (0056 holds only live states), and walking
``speech_job_cancellation_fences`` would make the 5 s poll grow with the
deployment's cancellation history. The index turns the read's acceptance-age
window into a range, so 0060's downgrade must not silently remove it.
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
    DELETION_LEDGER_REVISION,
    LEFT_CANCELLING_INDEX,
    LEFT_CANCELLING_INDEX_REVISION,
    step_below_ledger_debt_index,
)

pytestmark = pytest.mark.integration


def _definition(query: Connection[dict[str, Any]]) -> str | None:
    row = query.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
        (LEFT_CANCELLING_INDEX,),
    ).fetchone()
    return None if row is None else str(row["indexdef"])


def _revision(query: Connection[dict[str, Any]]) -> str:
    row = query.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row["version_num"])


def test_0060_indexes_the_fence_acceptance_age_and_refuses_its_own_downgrade() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, DELETION_LEDGER_REVISION)
        assert _definition(query) is None

        command.upgrade(config, "head")
        definition = _definition(query)
        assert definition is not None
        # The read's predicate is ``accepted_at_us >= now - window`` and its order
        # starts with ``accepted_at_us, job_id``: both need this key, and the index
        # must stay total -- the "unsettled" fact lives in another table.
        assert "USING btree (accepted_at_us, job_id)" in definition
        assert "WHERE" not in definition

        # 0061 and 0062 sit above 0060 and refuse first; walk back past both (the
        # helper removes 0062's index, removes 0061's, and stamps 0060) so this test
        # observes 0060's own guard.
        step_below_ledger_debt_index(query)

        # 0060's own refusal, not 0059's: the index is a runtime safety property.
        with pytest.raises(RuntimeError) as refused:
            command.downgrade(config, DELETION_LEDGER_REVISION)
        message = str(refused.value)
        assert "0060" in message
        assert LEFT_CANCELLING_INDEX in message
        assert "left-cancelling" in message

        # The refused downgrade leaves the head and the access path it ships intact.
        assert _revision(query) == LEFT_CANCELLING_INDEX_REVISION
        assert _definition(query) == definition

        # And a later upgrade is still a no-op rather than a duplicate CREATE INDEX.
        command.upgrade(config, "head")
        assert _definition(query) == definition
