"""coordination_jobs 补 canonical 列并回填(0064,加法段 A)。

canonical 形状定义在 ``migrations/0003_portable_claim_buckets.sql``:``bucket_no`` 决定
Job 落在哪个固定桶,``due_at`` / ``claim_expires_at`` 用 UTC ``TIMESTAMP(6)`` 表达时刻,
``last_event_seq`` 是每 Job 的事件头。当前生产表还只有 epoch 微秒的
``claim_expires_at_us``,没有这些 canonical 列。

本修订只做加法:不改任何既有列,不切任何读写路径。旧代码继续读 ``claim_expires_at_us``
等草案列,新列暂时没有读取者;真正删掉 epoch 列、把应用切到 canonical 列属于后续修订。
因此这里的回填可以先落地并用 SQL 对账,而不需要一次上线同时改代码。

回填口径与 ``speechnexus_portable_sql.claims.canonical_job_from_draft`` 同一份定义:

* ``claim_expires_at``:``0`` 与负数是草案的「未领取」,换算成 ``NULL``;其余
  ``to_timestamp(us / 1e6) AT TIME ZONE 'UTC'``。``AT TIME ZONE`` 是必需的:canonical
  列不带时区,直接把 timestamptz 赋给它是按会话时区折墙上时间,``Asia/Shanghai`` 会话下
  会静默偏移 8 小时。
* ``bucket_no``:``bucket_for(job_id)``(sha256(job_id) 前 8 字节大端 mod 256)。桶名不能
  交给 SQL 现算,否则同一规则出现第二份实现;这里逐行用 Python 的定义点算好再写入。
* ``due_at``:生产 ``coordination_jobs`` **没有** ``due_at_us`` 列(任务描述里的这个来源
  列不存在)。``migrations/canonical_source_map.json`` 已把 canonical ``due_at`` 的来源
  冻结为 ``speech_jobs.created_at_us``,所以按同一 epoch 换算从该列回填;没有对应
  ``speech_jobs`` 行的 Job 先留 ``NULL``——切换前必须补齐,而不是在这里编造一个时刻。
* ``last_event_seq``:先用 ``DEFAULT 0`` 占位。来源清单要求取
  ``MAX(speech_job_events.event_seq)``,但本段不改变事件写入路径,回填留给后续修订。

回填后立即对账两条不变量,失败即抛 ``RuntimeError`` 并给出行数,让迁移在提交前停下:
每一行都必须有 ``bucket_no``;``claim_expires_at`` 必须与 ``claim_expires_at_us`` 的
0/NULL 等价关系一致。这样一次「迁移成功但漏搬」不会留到切换后才以「任务永远不被认领」
的形式暴露。

``downgrade`` 只 DROP 这四列。它们是纯加法列,当前没有任何生产读取者,丢弃不改变既有
行为或证据;被保留的 epoch 列仍是唯一权威,所以回退是安全的。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Connection, text

from speechnexus_portable_sql.claims import bucket_for

revision = "0064_canonical_jobs_columns"
down_revision = "0063_deletion_targets_blocked"
branch_labels = None
depends_on = None

_ADDED_COLUMNS = ("bucket_no", "last_event_seq", "due_at", "claim_expires_at")

_CLAIM_EXPIRES_SQL = (
    "UPDATE coordination_jobs SET claim_expires_at = CASE "
    "WHEN claim_expires_at_us > 0 "
    "THEN to_timestamp(claim_expires_at_us / 1000000.0) AT TIME ZONE 'UTC' "
    "ELSE NULL END"
)

#: canonical_source_map.json 把 due_at 的来源固定为 speech_jobs.created_at_us;
#: 生产 coordination_jobs 没有独立的 due_at_us 列。
_DUE_AT_SQL = (
    "UPDATE coordination_jobs AS c "
    "SET due_at = to_timestamp(s.created_at_us / 1000000.0) AT TIME ZONE 'UTC' "
    "FROM speech_jobs AS s "
    "WHERE s.job_id = c.job_id AND s.created_at_us > 0"
)


def _backfill_buckets(connection: Connection) -> None:
    """bucket 由 Python 的唯一定义点算出;SQL 只负责写入。"""

    job_ids = [
        str(value)
        for value in connection.execute(
            text("SELECT job_id FROM coordination_jobs ORDER BY job_id")
        ).scalars()
    ]
    if not job_ids:
        return
    connection.execute(
        text("UPDATE coordination_jobs SET bucket_no = :bucket_no WHERE job_id = :job_id"),
        [{"bucket_no": int(bucket_for(job_id)), "job_id": job_id} for job_id in job_ids],
    )


def _require_reconciled(connection: Connection) -> None:
    missing_buckets = connection.execute(
        text("SELECT COUNT(*) FROM coordination_jobs WHERE bucket_no IS NULL")
    ).scalar_one()
    if missing_buckets:
        raise RuntimeError(
            f"0064 backfill left {missing_buckets} coordination_jobs row(s) without bucket_no"
        )
    inconsistent_claims = connection.execute(
        text(
            "SELECT COUNT(*) FROM coordination_jobs WHERE "
            "(claim_expires_at_us <= 0 AND claim_expires_at IS NOT NULL) "
            "OR (claim_expires_at_us > 0 AND claim_expires_at IS NULL)"
        )
    ).scalar_one()
    if inconsistent_claims:
        raise RuntimeError(
            "0064 backfill left "
            f"{inconsistent_claims} coordination_jobs row(s) whose claim_expires_at "
            "does not match claim_expires_at_us"
        )


def upgrade() -> None:
    # ``IF NOT EXISTS`` 不是保险,是这套迁移树的实际需要:若干集成测试用
    # ``tests.support.migrations.step_below_*`` 直接回退 ``alembic_version`` 表(不走
    # downgrade),再 ``upgrade head``。用裸 ``ADD COLUMN`` 时这些测试会以
    # ``DuplicateColumn`` 失败,而它们验证的是别的修订,不该被本修订拖红。
    # 回填语句本身幂等(全表重算),所以重复执行不会累积偏差。
    op.execute("ALTER TABLE coordination_jobs ADD COLUMN IF NOT EXISTS bucket_no SMALLINT")
    op.execute(
        "ALTER TABLE coordination_jobs ADD COLUMN IF NOT EXISTS "
        "last_event_seq BIGINT NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE coordination_jobs ADD COLUMN IF NOT EXISTS due_at TIMESTAMP(6)")
    op.execute(
        "ALTER TABLE coordination_jobs ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMP(6)"
    )

    connection = op.get_bind()
    connection.execute(text(_CLAIM_EXPIRES_SQL))
    connection.execute(text(_DUE_AT_SQL))
    _backfill_buckets(connection)
    _require_reconciled(connection)


def downgrade() -> None:
    # 纯加法列,尚无生产读取者;epoch 列仍在,回退不会丢证据。
    for column in _ADDED_COLUMNS:
        op.execute(f"ALTER TABLE coordination_jobs DROP COLUMN IF EXISTS {column}")
