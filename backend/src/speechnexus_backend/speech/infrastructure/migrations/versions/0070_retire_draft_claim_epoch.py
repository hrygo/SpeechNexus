"""退役草案 epoch 列 ``coordination_jobs.claim_expires_at_us``(S5,单向)。

计划 §5 的 S5。canonical 列 ``claim_expires_at``(``0064``)自 #1176 起是认领的唯一权威,
草案 epoch 列在 ``claims_sql`` 随 S3 整口删除之后已经没有读者,也没有写者:

* 认领(R)读写的只有 ``claim_expires_at`` / ``claim_version`` / ``job_state``
  (``portable_sql.claims`` 的 ``job.claim`` 家族)。
* 写入者重放路径读``coordination_jobs``的 canonical 列;它的 INSERT 列声明
  (``COORDINATION_JOB_INSERT_COLUMNS``)从来不含这个 epoch 列。
* 剩下的引用都是**声明面**与**迁移史**,不是运行路径:本修订连同同一提交把声明面
  (``coordination_grants.SPEECH_UPDATES``、``job_runtime._JOB_UPDATE_SURFACE``、
  ``provision-job-roles.WRITER_BASELINE_UPDATE``、``check-async-job-prereqs`` 的副本、
  ``scripts/dev-env/coordination-grants.py``)与 ``job_schema.JOB_DDL`` 里的那一行一起删掉。

**``coordination_outbox.delivered_at_us`` 不在本修订里。** 计划 §5 的 S5 把它与这一列并列,
但代码证据不支持:

* ``runtime_delivery_sql`` 的 ACK/读取语句按 ``delivered_at_us`` 判定「未投递」
  (``... o.event_kind='runtime' AND o.delivered_at_us IS NULL``)并写它
  (``UPDATE coordination_outbox SET delivered_at_us=?``);
* ``runtime_delivery_schema.runtime_delivery_grants`` 授的正是这一列,``runtime_intent_schema``
  的 ``idx_outbox_pending_kind`` 也建在它上面;
* 消费方是独立的生产单元 ``speechnexus-backend`` 的 ``runtime_delivery_worker`` 入口,
  不在 #1102 的派发链路里。

它属于 runtime 意图投递,不是派发草案列;删它要求先把那条投递路径切到 canonical
``delivered_at``(格式从 BIGINT epoch 换成 ``TIMESTAMP(6)``),那是另一个切片的判据面。本修订
留着它,并在 §6 的回退表里保持「S5 只删 ``claim_expires_at_us``」的口径。

``downgrade`` 拒绝执行:重跑 ``0064``/``0065`` 也不再是合法回退 —— 它们的回填语句读这一列,
删列之后是 ``42703``。回退只能走已批准的备份恢复(计划 §6)。
"""

from __future__ import annotations

from alembic import op

revision = "0070_retire_draft_claim_epoch"
down_revision = "0069_tighten_job_bucket_due"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE coordination_jobs DROP COLUMN IF EXISTS claim_expires_at_us")


def downgrade() -> None:
    raise RuntimeError(
        "Retiring coordination_jobs.claim_expires_at_us is a one-way step (plan §6): re-running "
        "0064/0065 now fails with 42703, so restore an approved backup instead"
    )
