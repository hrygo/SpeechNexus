"""收紧 ``coordination_jobs.bucket_no`` 为 ``NOT NULL``(S4)。

计划 §5 的 S4 只做一件写入者**已经能保证**的事:把 ``0065`` 有意留成可空的列收紧。
``0065`` 的第 3 步写着「``bucket_no`` 的 ``NOT NULL`` 属于写入切换,不能先于写入者落地」;
写入切换(#1176)已经落地,所以这一列的前提成立:

* 生产 writer 的 INSERT 列声明
  (``job_writer_columns.COORDINATION_JOB_INSERT_COLUMNS``)含 ``bucket_no``,值是
  ``bucket_for(job_id)``;
* 换算的唯一 Python 定义点仍是 ``portable_sql.claims.bucket_for``,本修订不新增第二处。

**为什么必须先回填。** 只要表里还有一行 NULL,``ALTER COLUMN ... SET NOT NULL`` 就以
``23502`` 失败并回滚整笔迁移。回填与 ``0064``/``0065`` 同一份:逐行用 Python 的定义点算
(全表重算,幂等),再断言 0 行 NULL。

**head 上能有几行 NULL —— 由迁移史推导,不是估计。** ``0064`` 与 ``0065`` 都对**全表**重算过
``bucket_for(job_id)``,且都在提交前断言 0 行 NULL(``0064:_require_reconciled`` 的
``missing_buckets``、``0065`` 的 ``_MISSING_BUCKETS_SQL``)。两修订之后新增的行只能来自 Job
writer,而它的 INSERT 列声明含 ``bucket_no``。所以 head 上不存在 NULL 行,这一列可以收紧。

**``due_at`` 不收紧,而且这是判据而不是遗漏。** 计划 §5 把两列并列,但迁移史给出的是**非零**
的合法 NULL 总体,不是 0:

* ``0064``/``0065`` 把 ``due_at`` 的来源冻结为 ``speech_jobs.created_at_us``,而
  ``0065`` 的对账 ``_MISSING_DUE_AT_SQL`` 只覆盖「有对应公开行且 ``created_at_us > 0``」的
  Job —— 它**有意**允许一行 ``coordination_jobs`` 保留 NULL。
* 这种形状真实存在,不是测试造出来的:``0001_jobs`` 创建 ``coordination_jobs`` 时
  ``speech_jobs`` 还不存在(``0016_public_jobs`` 才有),那批行上**没有任何时间列**可换算
  (``coordination_jobs`` 自己没有时间戳,outbox 的 ``delivered_at_us`` 也不是创建时刻)。
  ``tests/support/legacy_jobs.seed_legacy_job`` 就是这条形状,多个迁移用例靠它起步。
* 回填没有第二个来源,给这些行写一个算出来的时刻等于记录一个没人观测过的事实
  (``0054`` 已立过这条规则);而且 ``due_at`` 是认领候选谓词 ``due_at <= now`` 的一侧,
  把一个创建时刻未知的历史 Job 改成「立刻到期」不是收紧约束,是改变它能否被派发。

所以 ``NULL due_at`` 继续表示「创建时刻未知、不参与派发」,这一列的可空性是契约的一部分。
写入侧仍然每次都提供它:两个生产 INSERT 都从 ``speech_jobs.created_at_us`` 换算。

本修订仍然对 ``due_at`` 做一次 fill-if-null 回填(有来源的行补齐,幂等),只是不加约束;
``downgrade`` 只放开 ``bucket_no`` 的 ``NOT NULL``。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Connection, text

from speechnexus_portable_sql.claims import bucket_for

revision = "0069_tighten_job_bucket_due"
down_revision = "0068_canonical_event_tables"
branch_labels = None
depends_on = None

#: 与 ``0065`` 的 ``_DUE_AT_SQL`` 同一条换算:只填 NULL,canonical 一旦写下就是权威。
#: 没有对应公开行的 Job 保持 NULL(见模块 docstring:那不是一个错误,而是「时刻未知」)。
_DUE_AT_SQL = (
    "UPDATE coordination_jobs AS c "
    "SET due_at = to_timestamp(s.created_at_us / 1000000.0) AT TIME ZONE 'UTC' "
    "FROM speech_jobs AS s "
    "WHERE s.job_id = c.job_id AND s.created_at_us > 0 AND c.due_at IS NULL"
)

_MISSING_BUCKETS_SQL = "SELECT COUNT(*) FROM coordination_jobs WHERE bucket_no IS NULL"


def _backfill_buckets(connection: Connection) -> None:
    """bucket 由 Python 的唯一定义点算出;SQL 只负责写入."""

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


def _require_backfilled(connection: Connection) -> None:
    missing_buckets = connection.execute(text(_MISSING_BUCKETS_SQL)).scalar_one()
    if missing_buckets:
        raise RuntimeError(
            f"0069 backfill left {missing_buckets} coordination_jobs row(s) without bucket_no"
        )


def upgrade() -> None:
    connection = op.get_bind()
    _backfill_buckets(connection)
    connection.execute(text(_DUE_AT_SQL))
    _require_backfilled(connection)
    op.execute("ALTER TABLE coordination_jobs ALTER COLUMN bucket_no SET NOT NULL")


def downgrade() -> None:
    # 只回到 0065 声明的可空形状;列与值都保留,回退不丢事实。
    op.execute("ALTER TABLE coordination_jobs ALTER COLUMN bucket_no DROP NOT NULL")
