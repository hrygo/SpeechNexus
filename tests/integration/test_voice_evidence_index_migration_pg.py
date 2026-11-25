"""0048 adds the partial index that keeps the evidence scan bounded."""

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

_INDEX = "speech_jobs_voice_evidence_scan"
_PREDECESSOR = "0047_voice_publication_evidence"
#: 拥有这个索引的修订。回环只在这条链上走:``0070`` 的单向退役之后,从 head 往下走
#: 的第一条 downgrade 就被拒绝,而 0048 的索引根本不需要经过它。
_REVISION = "0048_voice_evidence_scan_index"


def _definition(query: Connection[dict[str, Any]]) -> str | None:
    row = query.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
        (_INDEX,),
    ).fetchone()
    return None if row is None else str(row["indexdef"])


def test_scan_index_is_created_with_its_predicate_and_dropped() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, _PREDECESSOR)
        assert _definition(query) is None

        command.upgrade(config, _REVISION)
        definition = _definition(query)
        assert definition is not None
        assert "updated_at_us, job_id" in definition
        assert "WHERE" in definition
        assert "'voice_validation'" in definition
        assert "'succeeded'" in definition

        command.downgrade(config, _PREDECESSOR)
        assert _definition(query) is None

        command.upgrade(config, _REVISION)
        assert _definition(query) == definition

        # Later revisions must replay over the same access path.
        command.upgrade(config, "head")
        assert _definition(query) == definition
