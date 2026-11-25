"""Production coordination Job identity and atomic initial outbox fact."""

from alembic import op

from speechnexus_backend.speech.infrastructure.job_schema import JOB_DDL

revision = "0001_jobs"
down_revision = None
branch_labels = None
depends_on = None

#: ``0001`` 的职责是创建**当时**的草案形状。草案 epoch 列 ``claim_expires_at_us`` 已在
#: ``0070`` 退役,所以它不再属于 ``JOB_DDL``(那是 head 形状,同时是 InnoDB 验证 schema 的
#: 来源);但 ``0064``/``0065`` 的回填与对账语句读这一列,迁移链必须能从空库重放到 ``0069``。
#: 因此历史列留在这条已发布修订里:对已在 ``0001`` 之后的库无影响,对空库则保持与当时一致的
#: 形状。
_LEGACY_DRAFT_COLUMNS: tuple[str, ...] = (
    "ALTER TABLE coordination_jobs ADD COLUMN claim_expires_at_us BIGINT NOT NULL "
    "DEFAULT 0 CHECK (claim_expires_at_us >= 0)",
)


def upgrade() -> None:
    for statement in JOB_DDL:
        op.execute(statement)
    for statement in _LEGACY_DRAFT_COLUMNS:
        op.execute(statement)


def downgrade() -> None:
    # These tables contain durable execution/idempotency evidence. Roll back
    # application code or restore an approved backup; never silently erase them.
    raise RuntimeError("Destructive Job/outbox downgrade requires an explicit recovery plan")
