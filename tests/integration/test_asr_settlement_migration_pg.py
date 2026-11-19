"""0057 refuses every downgrade, preserving ASR settlement history."""

from __future__ import annotations

import pytest
from alembic import command
from tests.integration.test_public_job_progress_migration_pg import (
    _isolated_schema,
    _url,
)
from tests.support.migrations import (
    ASR_SETTLEMENT_REVISION,
    DELETION_LEDGER_REVISION,
    DELETION_PLAN_REVISION,
    step_below_deletion_ledger,
    step_below_deletion_plans,
    step_below_left_cancelling_index,
)

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


def test_0058_downgrade_refuses_to_discard_issued_plans() -> None:
    """0058 is the first hop; its own refusal must stop the downgrade."""
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        # 0059 sits above 0058 and refuses first; reach the pre-0059 head so this
        # test observes 0058's own guard.
        step_below_deletion_ledger(query)
        query.execute(
            "INSERT INTO speech_deletion_plans "
            "(id, workspace_id, target_owner, target_kind, target_id, target_revision, "
            " target_digest, mode, plan_revision, plan_digest, idempotency_key_digest, "
            " intent_digest, created_at, expires_at, dependencies, content_snapshot) "
            "VALUES ('dp-0058-guard', 'workspace-default', 'speech', 'asset', "
            " 'asset-0058-guard', '1', %s, 'ordinary', '1', %s, %s, %s, "
            " now(), now() + interval '300 seconds', '[]'::jsonb, '{}'::jsonb)",
            ("sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64, "d" * 64),
        )

        with pytest.raises(RuntimeError, match="Deletion plan history requires preservation"):
            command.downgrade(config, ASR_SETTLEMENT_REVISION)

        retained = query.execute("SELECT count(*) AS n FROM speech_deletion_plans").fetchone()
        assert retained is not None and retained["n"] == 1
        revision = query.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision is not None and revision["version_num"] == DELETION_PLAN_REVISION


def test_0059_downgrade_refuses_to_discard_the_ledger() -> None:
    """0059 refuses: the ledger is the only proof a deletion happened."""
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        # 0060 sits above 0059 and refuses first; reach the pre-0060 head so this
        # test observes 0059's own guard.
        step_below_left_cancelling_index(query)
        query.execute(
            "INSERT INTO speech_deletion_plans (id, workspace_id, target_owner, target_kind, "
            "target_id, target_revision, target_digest, mode, plan_revision, plan_digest, "
            "idempotency_key_digest, intent_digest, created_at, expires_at, dependencies, "
            "content_snapshot) VALUES ('dp-0059-guard', 'workspace-default', 'speech', 'asset', "
            "'asset-0059-guard', '1', %s, 'ordinary', '1', %s, %s, %s, now(), "
            "now() + interval '300 seconds', '[]'::jsonb, '{}'::jsonb)",
            ("sha256:" + "d" * 64, "sha256:" + "e" * 64, "f" * 64, "1" * 64),
        )
        query.execute(
            "INSERT INTO speech_deletion_ledger (id, workspace_id, plan_id, plan_revision, "
            "plan_digest, target_owner, target_kind, target_id, target_revision, "
            "idempotency_key_digest, intent_digest, deleted, retained, status, cleanup_state, "
            "created_at, updated_at, completed_at) VALUES "
            "('dl-0059-guard', 'workspace-default', 'dp-0059-guard', '1', %s, 'speech', "
            "'asset', 'asset-0059-guard', '1', %s, %s, %s, '[]'::jsonb, 'succeeded', "
            "'complete', now(), now(), now())",
            (
                "sha256:" + "a" * 64,
                "b" * 64,
                "c" * 64,
                '[["speech","asset","asset-0059-guard","1"]]',
            ),
        )

        with pytest.raises(RuntimeError, match="Deletion ledger history requires preservation"):
            command.downgrade(config, DELETION_PLAN_REVISION)

        retained = query.execute("SELECT count(*) AS n FROM speech_deletion_ledger").fetchone()
        assert retained is not None and retained["n"] == 1
        revision = query.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision is not None
        assert revision["version_num"] == DELETION_LEDGER_REVISION
