"""0056 adds the partial index that keeps both readiness debt probes on the live set."""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from psycopg import Connection
from tests.integration.test_public_job_progress_migration_pg import (
    _isolated_schema,
    _url,
)
from tests.support.migrations import step_below_asr_settlement

pytestmark = pytest.mark.integration

_INDEX = "speech_jobs_live_state_scan"
_PREDECESSOR = "0055_operator_attestation"


def _definition(query: Connection[dict[str, Any]]) -> str | None:
    row = query.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
        (_INDEX,),
    ).fetchone()
    return None if row is None else str(row["indexdef"])


def test_live_state_index_covers_both_probe_predicates_and_is_dropped() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, _PREDECESSOR)
        assert _definition(query) is None

        command.upgrade(config, "head")
        definition = _definition(query)
        assert definition is not None
        # Both debt probes filter on a live state; the index must answer both keys and
        # carry the ordering column, or the planner still reads the whole history.
        assert "speech_jobs USING btree (state, updated_at_us, job_id)" in definition
        assert "'running'" in definition
        assert "'cancelling'" in definition
        assert "WHERE" in definition

        step_below_asr_settlement(query)
        command.downgrade(config, _PREDECESSOR)
        assert _definition(query) is None

        command.upgrade(config, "head")
        assert _definition(query) == definition
