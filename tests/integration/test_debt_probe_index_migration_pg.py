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

pytestmark = pytest.mark.integration

_INDEX = "speech_jobs_live_state_scan"
_PREDECESSOR = "0055_operator_attestation"
#: 拥有这个索引的修订。回环只在这条链上走:``0070`` 的单向退役之后,从 head 往下
#: 走的第一条 downgrade 就被拒绝,而 0056 的索引根本不需要经过它。
_REVISION = "0056_debt_probe_live_state_index"


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

        command.upgrade(config, _REVISION)
        definition = _definition(query)
        assert definition is not None
        # Both debt probes filter on a live state; the index must answer both keys and
        # carry the ordering column, or the planner still reads the whole history.
        assert "speech_jobs USING btree (state, updated_at_us, job_id)" in definition
        assert "'running'" in definition
        assert "'cancelling'" in definition
        assert "WHERE" in definition

        # The round trip belongs to 0056↔0055. Walking back from head would be refused by
        # 0070's one-way step before 0056's own downgrade could run at all.
        command.downgrade(config, _PREDECESSOR)
        assert _definition(query) is None

        command.upgrade(config, _REVISION)
        assert _definition(query) == definition

        # Later revisions must keep the same access path, not recreate it differently.
        command.upgrade(config, "head")
        assert _definition(query) == definition
