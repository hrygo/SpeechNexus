"""Durable non-content roots for synchronous ASR lease settlement.

The root is a work item, not evidence: it carries only the lease identity and
its route. Every release precondition is re-read under the release transaction's
locks, so a root can never authorize a reservation return by itself.

Historical rows are adopted here so an upgraded deployment can boot with the
pre-existing #974 lease and let the settlement worker return it.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.asr_settlement_sql import (
    ASR_SETTLEMENT_BACKFILL,
)

revision = "0057_asr_settlement"
down_revision = "0056_debt_probe_live_state_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE speech_asr_settlement (
        lease_id VARCHAR(128) COLLATE "C" PRIMARY KEY REFERENCES admission_leases (lease_id),
        operation_id VARCHAR(128) COLLATE "C" NOT NULL,
        attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
        gateway_id VARCHAR(128) COLLATE "C" NOT NULL,
        host_node_id VARCHAR(128) COLLATE "C" NOT NULL,
        next_check_at_us BIGINT NOT NULL DEFAULT 0 CHECK (next_check_at_us >= 0),
        settled_at_us BIGINT CHECK (settled_at_us > 0),
        FOREIGN KEY (operation_id, attempt_id)
            REFERENCES admission_attempts (operation_id, attempt_id)
    )""")
    op.execute(
        "CREATE INDEX speech_asr_settlement_operation ON speech_asr_settlement (operation_id)"
    )
    op.execute(
        "CREATE INDEX speech_asr_settlement_due ON speech_asr_settlement "
        "(next_check_at_us, lease_id) WHERE settled_at_us IS NULL"
    )
    op.execute(ASR_SETTLEMENT_BACKFILL)


def downgrade() -> None:
    raise RuntimeError("ASR settlement history requires preservation")
