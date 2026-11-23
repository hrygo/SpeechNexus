"""coordination_jobs 二次回填 last_event_seq 与 0064 窗口行(0065),约束收紧另立修订。

0064 只给存量行做了一次加法回填,之后新建的 ``coordination_jobs`` 行仍只有草案列:
``bucket_no`` / ``claim_expires_at`` / ``due_at`` 为 ``NULL``,``last_event_seq`` 停在
``DEFAULT 0``。这在一张随时会写入的表上等于「回填只对迁移那一刻的行成立」,所以本修订先把
这段窗口补上,否则任何依赖 canonical 列的后续动作都会只对历史行成立。

1. **重跑 0064 的三条回填,但租约与 due_at 只填 ``NULL``**。canonical 列一旦被新的
   canonical 路径写过就是权威(§8.77 前置 3 的决定 (b):只写 canonical,迁移以 canonical
   为准),所以这两条回填是 **fill-if-null**:

   * ``claim_expires_at``:只在它 ``IS NULL`` 且 ``claim_expires_at_us > 0`` 时按
     ``to_timestamp(us / 1e6) AT TIME ZONE 'UTC'`` 写入。旧写法无条件从草案 epoch 列重算,
     会把新认领路径刚写下的租约擦成 ``NULL`` —— 下一次重跑迁移就等于让在飞租约失权。
   * ``due_at``:只在它 ``IS NULL`` 时从已冻结的来源 ``speech_jobs.created_at_us`` 换算;
     没有对应 ``speech_jobs`` 行的 Job 保持 ``NULL``。
   * ``bucket_no`` 仍由 Python 的唯一定义点 ``bucket_for(job_id)`` 算好再写入,不在 SQL 里
     重写一遍哈希。``AT TIME ZONE`` 不是修辞:canonical 列是不带时区的 ``TIMESTAMP(6)``,
     直接把 timestamptz 赋给它按会话时区折墙上时间,``Asia/Shanghai`` 会话下静默偏移 8 小时。

2. **回填 ``last_event_seq``** 为每 Job 的 ``MAX(speech_job_events.event_seq)``,无事件为
   ``0``。这里是全表一次聚合(``UPDATE ... FROM (SELECT ... GROUP BY)`` 的形状),不是
   逐行往返:草案事件表的 ``event_seq`` 由 ``COALESCE(MAX(event_seq), -1) + 1`` 生成,所以
   ``MAX`` 是唯一可信的事件头,``COUNT`` 会把 ``0,1,2`` 读成 ``3``。

3. **没有收紧 ``bucket_no`` 的 NOT NULL。**这一步在本修订里会把约束和写入者拆开落地:
   当时 ``jobs_sql.create_in_transaction`` 与 ``public_jobs_postgres`` 的 fallback 只写
   ``COORDINATION_JOB_INSERT_COLUMNS`` 的六个草案列,``bucket_no`` 既不在授权里也不在
   INSERT 里,一旦 NOT NULL,每次新建 Job 都会以 ``23502`` 失败并报成
   ``503 job_unavailable``。让数据库自己算桶号等于把 ``bucket_for`` 的 sha256 规则重写一份
   SQL 实现,而这条规则的唯一定义点在 Python;所以收紧必须晚于「写入者开始提供
   ``bucket_for(job_id)``」的写入切换(#1176)。该切换落地后两个写入者都在 INSERT 时提供
   ``bucket_no``,本修订仍保留可空列,把 ``NOT NULL`` 留给后续修订;第 4 步的第一条对账
   继续保证 **每一行都有桶号**。``tests/integration/test_canonical_jobs_0065_pg.py``
   现在两个方向都钉住:``"bucket_no"`` 在 ``COORDINATION_JOB_INSERT_COLUMNS`` 里,而列本身
   仍可空——后者只有收紧修订会翻。

4. **提交前对账四条不变量**,任一不满足就抛 ``RuntimeError`` 并报出行数:没有漏 ``bucket_no``
   的行;没有「草案说已领取、canonical 从未收到」的行(``claim_expires_at_us > 0`` 而
   ``claim_expires_at IS NULL``);``last_event_seq`` 与事件表的 ``MAX`` 逐行一致(这条让第 2
   步可证伪,否则「回填了 0」和「回填跑了但什么都没读」看起来一样);``due_at`` 不为 ``NULL``
   而对应 ``speech_jobs`` 行的 ``created_at_us > 0``。

   第二条只保留 fill-if-null **自己那一半**。反方向(``claim_expires_at_us <= 0`` 而
   ``claim_expires_at IS NOT NULL``)在 canonical 权威下是常态而不是损坏:新认领路径只写
   canonical,草案 epoch 列停在新建时的 ``0``。同理 ``draft > 0 -> canonical NOT NULL`` 能
   一直成立,前提是 canonical 租约一旦写下就不再被清空——派发终止只把 ``job_state`` 推成
   终态并保留 ``claim_expires_at``(``job.reclaim`` 也只重领 ``claimed`` 行),所以这条断言
   在重跑时仍然为真。

``downgrade`` 把 ``last_event_seq`` 归零,并防御性清掉 ``bucket_no`` 上可能存在的 NOT NULL
(本修订自己不设它,但别的部署或手工 DDL 可能设过),列本身不删——列由 0064 拥有。回到 0064
语义是安全的:``last_event_seq`` 目前**没有任何读取者**,``claim_expires_at`` 的读取者只认
已经写下的值(回退不擦它),归零只影响一个尚未被消费的占位列。这也让 ``step_below_*`` 那类
「直接回退 alembic_version 再 upgrade head」的测试可以重复进入本修订:回填的写入只会落在
``NULL`` 上,``DROP NOT NULL`` 对已可空的列是空操作,重复执行不会累积偏差。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Connection, text

from speechnexus_portable_sql.claims import bucket_for

revision = "0065_canonical_jobs_reconcile"
down_revision = "0064_canonical_jobs_columns"
branch_labels = None
depends_on = None

#: 与 0064 同一条换算,但**只填 NULL**:canonical 一旦写下就是权威,重跑不得覆盖。
#: 「0/负数是未领取」这条等价关系仍只在 0064 的换算里定义;这里只是把它的结果当成候选值,
#: 而 ``claim_expires_at_us > 0`` 已经把 «写 NULL» 这一支排除掉了。
_CLAIM_EXPIRES_SQL = (
    "UPDATE coordination_jobs SET claim_expires_at = "
    "to_timestamp(claim_expires_at_us / 1000000.0) AT TIME ZONE 'UTC' "
    "WHERE claim_expires_at IS NULL AND claim_expires_at_us > 0"
)

#: canonical_source_map.json 把 due_at 的来源固定为 speech_jobs.created_at_us。
#: 同样只填 NULL:写入者在 INSERT 时已经按同一来源写过它,重跑不覆盖。
_DUE_AT_SQL = (
    "UPDATE coordination_jobs AS c "
    "SET due_at = to_timestamp(s.created_at_us / 1000000.0) AT TIME ZONE 'UTC' "
    "FROM speech_jobs AS s "
    "WHERE s.job_id = c.job_id AND s.created_at_us > 0 AND c.due_at IS NULL"
)

#: 一次聚合而不是逐行往返;``LEFT JOIN`` 让没有事件的 Job 也落进分组,取 ``COALESCE`` 为 0。
_LAST_EVENT_SEQ_SQL = (
    "UPDATE coordination_jobs AS c "
    "SET last_event_seq = COALESCE(agg.last_seq, 0) "
    "FROM ("
    "SELECT c2.job_id AS job_id, MAX(e.event_seq) AS last_seq "
    "FROM coordination_jobs AS c2 "
    "LEFT JOIN speech_job_events AS e ON e.job_id = c2.job_id "
    "GROUP BY c2.job_id"
    ") AS agg "
    "WHERE agg.job_id = c.job_id"
)

_MISSING_BUCKETS_SQL = "SELECT COUNT(*) FROM coordination_jobs WHERE bucket_no IS NULL"

#: fill-if-null 只保证一个方向:草案说「已领取」的行必须已经在 canonical 里有租约。
#: 反方向(草案仍为 0、canonical 有值)是 canonical 权威后的正常状态,不再是对账目标。
_UNPROMOTED_CLAIM_SQL = (
    "SELECT COUNT(*) FROM coordination_jobs WHERE "
    "claim_expires_at_us > 0 AND claim_expires_at IS NULL"
)

_STALE_EVENT_HEAD_SQL = (
    "SELECT COUNT(*) FROM coordination_jobs AS c WHERE c.last_event_seq <> COALESCE("
    "(SELECT MAX(e.event_seq) FROM speech_job_events AS e WHERE e.job_id = c.job_id), 0)"
)

_MISSING_DUE_AT_SQL = (
    "SELECT COUNT(*) FROM coordination_jobs AS c WHERE c.due_at IS NULL AND EXISTS ("
    "SELECT 1 FROM speech_jobs AS s WHERE s.job_id = c.job_id AND s.created_at_us > 0)"
)


def _backfill_buckets(connection: Connection) -> None:
    """bucket 由 Python 的唯一定义点算出;SQL 只负责写入.

    全表重算而不是只补 ``NULL`` 行:与 0064 同一段语句,这样「0064 之后新建的行」和
    「0064 之前算错的行」都会被同一条规则收敛到同一个值。
    """

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


def _require_zero(connection: Connection, statement: str, message: str) -> None:
    offending = connection.execute(text(statement)).scalar_one()
    if offending:
        raise RuntimeError(f"0065 reconcile left {offending} {message}")


def upgrade() -> None:
    connection = op.get_bind()
    # 第 1 步:重跑 0064 的回填,覆盖 0064 之后写入、只有草案列的行。
    connection.execute(text(_CLAIM_EXPIRES_SQL))
    connection.execute(text(_DUE_AT_SQL))
    _backfill_buckets(connection)
    # 第 2 步:事件头。草案事件表的 event_seq 由 MAX+1 生成,所以 MAX 才是头,COUNT 不是。
    connection.execute(text(_LAST_EVENT_SEQ_SQL))
    # 第 3 步(有意不做):bucket_no 的 NOT NULL 属于写入切换,不能先于写入者落地。
    # 详见模块 docstring;强制力由第 4 步的对账与测试的写入者形状断言分两层守。
    # 第 4 步:提交前对账,任一条不为 0 就抛错并随迁移事务回滚。
    _require_zero(connection, _MISSING_BUCKETS_SQL, "coordination_jobs row(s) without bucket_no")
    _require_zero(
        connection,
        _UNPROMOTED_CLAIM_SQL,
        "coordination_jobs row(s) whose draft lease never reached claim_expires_at",
    )
    _require_zero(
        connection,
        _STALE_EVENT_HEAD_SQL,
        "coordination_jobs row(s) whose last_event_seq differs from the event MAX",
    )
    _require_zero(
        connection,
        _MISSING_DUE_AT_SQL,
        "coordination_jobs row(s) with NULL due_at but a due public speech_jobs row",
    )


def downgrade() -> None:
    # 回到 0064 语义:列还在(0064 拥有它们),事件头归零,桶号不再被强制。``last_event_seq``
    # 目前没有任何读取者;``claim_expires_at`` 不回退——它可能已经是新的认领路径写下的在飞
    # 租约,擦掉它等于回退一次让 Job 失权,downgrade 不做这种破坏性动作。
    # 本修订的 upgrade 不设 NOT NULL;这里仍然显式清一次,让「别处设过」的库也能回到
    # 0064 语义,而不是把不可空的约束留在一个已经声明回退的版本上。
    op.execute("ALTER TABLE coordination_jobs ALTER COLUMN bucket_no DROP NOT NULL")
    op.execute("UPDATE coordination_jobs SET last_event_seq = 0")
