"""0057 refuses every downgrade, preserving ASR settlement history."""

from __future__ import annotations

import pytest
from alembic import command
from tests.integration.test_public_job_progress_migration_pg import (
    _isolated_schema,
    _url,
)
from tests.support.migrations import step_below_deletion_plans

pytestmark = pytest.mark.integration

_PREDECESSOR = "0056_debt_probe_live_state_index"


def test_downgrade_refuses_even_when_no_root_exists() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        # Reach the pre-0058 head; 0057 is the revision that carries this guard.
        step_below_deletion_plans(query)

        with pytest.raises(RuntimeError, match="ASR settlement history requires preservation"):
            command.downgrade(config, _PREDECESSOR)

        revision = query.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision is not None
        assert revision["version_num"] == "0057_asr_settlement"
        present = query.execute(
            "SELECT to_regclass('speech_asr_settlement') IS NOT NULL AS present"
        ).fetchone()
        assert present is not None
        assert present["present"]
