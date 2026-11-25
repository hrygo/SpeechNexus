"""canonical 事件侧的加法 DDL:``job_events`` 表与 outbox 的 ``delivered_at`` 列。

**这张表为什么现在才出现在 alembic 树里。** canonical 形状从
``migrations/0003_portable_claim_buckets.sql`` 起就声明了 ``job_events`` 与
``coordination_outbox.delivered_at``,但生产迁移树到 ``0067`` 为止**没有**这张表,而 outbox 上
只有草案的 ``delivered_at_us BIGINT``。事件读路径仍在草案 ``speech_job_events`` 上,所以缺口一直
没有以 42P01 显形;一旦按计划把事件写者/读者切到 canonical 对象,第一步就会打在不存在的关系上。
本模块只补**对象**,不补写者,也不补读者:这一刀是 S0 的纯加法,把「表不存在」与「切换事件
路径」拆成两次可分别验证的改动(计划 §5 S0)。

**列清单只有一个定义点。** ``JOB_EVENT_COLUMN_DEFINITIONS`` 是 ``job_events`` 在本仓唯一一处
逐列声明:建表文本与授权面都从它派生,所以给表加一列时只需要改这一处。
``coordination_jobs`` 的 INSERT 列曾漂成五份、加 ``bucket_no`` 与 ``due_at`` 各返工一次,漏一列
只在首次真实写入时以 ``42501`` 失败,而两个引擎都不点名缺哪列 —— 那是本模块拒绝复制列清单的理由。
``tests/`` 侧另有一份**独立**的期望值(类型、NULL 性、长度),它刻意不从本模块派生:判据与实现
同源时,把 ``delivered_at`` 改成 ``BIGINT`` 两边一起变,那个注入永远不会红。

**为什么 outbox 是加列而不是改列。** ``delivered_at_us`` 现在仍有读者(草案投递路径按它判
「未投递」),``delivered_at`` 在切换之前没有写者。两列并存是刻意的:删列是单向步骤,必须发生在
写者切换并经过观察窗之后(计划 D2/S5),不属于本次提交。``delivered_at`` 可空,``NULL`` 就是
「未投递」——与 ``0003`` 一致,也和 ``claim_expires_at`` 的取舍同源:不给它 ``NOT NULL DEFAULT``
凭空造一个「1970 年已投递」的事实。

**授权面按事实拆,不按表拆。** 未来这条路径只需要三件事,每件事只有一个定义点:

* ``SELECT (event_seq, event_digest, payload_ref)``:``job_event.read`` 的投影;
* ``INSERT (job_id, event_seq, event_digest, payload_ref)``:``job_event.insert`` 的列;
* outbox 上的 ``INSERT (delivered_at)`` 与 ``UPDATE (delivered_at)``:``outbox.insert`` 与
  ``outbox.mark_delivered`` 只碰这一列。

事件表**没有** ``UPDATE``/``DELETE``:事件是只追加的事实,行级改动会同时破坏
``(job_id, event_seq)`` 的连续性判据与摘要。两条导入期断言守着这份授权面:读者投影必须落在声明
之内(列改名而投影没跟上时立刻失败),权限面必须恰好是 ``SELECT``/``INSERT``(给事件表加
``UPDATE`` 得先改那句断言,不会悄悄出现在授权里)。

**本模块此刻没有被任何运行角色使用。** ``canonical_event_grants`` 是供给期声明,还没有调用点:
S1 把事件写者接过来时,由写者身份的授权函数调用它(与 ``coordination_keyed_lock_grants`` 同一形
状),启动闸门届时再核对它。现在把它接进 ``job_runtime_grants`` 会让每个身份多出一批「已授权、
无语句」的列,而 ``job_runtime`` 的逐列比对拒绝未声明的授权面 —— 那不是加法。
"""

from __future__ import annotations

#: canonical 事件表的物理名字。``0003`` 与 ``speechnexus_portable_sql.claims`` 的
#: ``job_event.insert``/``job_event.read`` 用的是同一个表,这里是它在产品侧的唯一字面量,
#: 迁移与授权都引用它。
JOB_EVENT_TABLE = "job_events"

#: outbox 表名与本次新增的列名,同样只有一份字面量。
COORDINATION_OUTBOX_TABLE = "coordination_outbox"
OUTBOX_DELIVERED_AT_COLUMN = "delivered_at"

#: 逐列物理类型,顺序即建表顺序。与 ``migrations/0003_portable_claim_buckets.sql`` 逐列一致:
#: ``VARCHAR(128)``/``DECIMAL(38,0)``/``VARCHAR(71)``/``VARCHAR(512)``。计数器用
#: ``DECIMAL(38,0)`` 而不是 ``BIGINT`` 的理由与 ``coordination_jobs`` 相同 —— 逻辑 schema 把每个
#: 计数器都定义成 ``decimal(38,0)``,而 ``claims`` 绑定的是 Python ``Decimal``。
JOB_EVENT_COLUMN_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("job_id", "VARCHAR(128) NOT NULL"),
    ("event_seq", "DECIMAL(38,0) NOT NULL"),
    ("event_digest", "VARCHAR(71) NOT NULL"),
    ("payload_ref", "VARCHAR(512)"),
)

#: 列名投影。只从上面那份声明派生,不在这里重写。
JOB_EVENT_COLUMNS: tuple[str, ...] = tuple(name for name, _ in JOB_EVENT_COLUMN_DEFINITIONS)

#: 事件顺序的唯一公开定义:``(job_id, event_seq)``。``0003`` 同样把它作主键。
JOB_EVENT_PRIMARY_KEY: tuple[str, ...] = ("job_id", "event_seq")

#: 事件行的所有者是 Job:外键 ``job_id`` 指向 ``coordination_jobs(job_id)``。没有这条外键,删掉
#: Job 之后仍会有「属于某个不存在 Job」的事件行,而续读按 ``job_id`` 过滤永远读不到它们。
#: 元组顺序是 (本表列, 被引用表);两侧列名同叫 ``job_id``。
JOB_EVENT_FOREIGN_KEY = ("job_id", "coordination_jobs")

#: 事件续读的列:``job_event.read`` 的投影逐字如此(``job_id`` 只出现在 ``WHERE``,不需要授权)。
JOB_EVENT_SELECT_COLUMNS: tuple[str, ...] = ("event_seq", "event_digest", "payload_ref")

#: 事件追加的列:``job_event.insert`` 逐字如此。
JOB_EVENT_INSERT_COLUMNS: tuple[str, ...] = JOB_EVENT_COLUMNS

#: 事件表的权限面。事件是只追加的事实,所以这里**没有** ``UPDATE`` 与 ``DELETE``。
JOB_EVENT_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "SELECT": JOB_EVENT_SELECT_COLUMNS,
    "INSERT": JOB_EVENT_INSERT_COLUMNS,
}

# ``INSERT`` 就是整行(``job_event.insert`` 写四个声明列),所以它按构造覆盖声明面,不可能缺列;
# 会漂的只有读者投影 —— 列改名而投影没跟上时,第一条续读以 ``42703`` 失败。这条断言把那个
# 漂移挪到导入期,而不是等到事件读路径上线之后。
assert set(JOB_EVENT_SELECT_COLUMNS) <= set(JOB_EVENT_COLUMNS), (
    "the reader projects a column the declaration does not have; renaming a column here "
    "without updating the projection fails the first event read with an undefined column"
)

assert set(JOB_EVENT_PRIVILEGES) == {"SELECT", "INSERT"}, (
    "the canonical event table is append-only: an UPDATE or DELETE privilege here would let "
    "a writer rewrite an event row and break the (job_id, event_seq) continuity the SSE "
    "order and the digest both rely on"
)

#: 本次修订执行的全部 DDL,唯一定义点:生产由 alembic ``0068`` 执行。
#: ``IF NOT EXISTS`` 与迁移树其余部分一致:若干集成用例把 ``alembic_version`` 回退再
#: ``upgrade head``,那时对象可能已经在;``ADD COLUMN IF NOT EXISTS`` 让「重入同一修订」不报错,
#: 而 ``delivered_at_us`` 原样保留,回退到 ``0067`` 时删掉的也只是本修订加的这一列。
JOB_EVENT_DDL: tuple[str, ...] = (
    f"CREATE TABLE IF NOT EXISTS {JOB_EVENT_TABLE} (\n    "
    + ",\n    ".join(f"{name} {definition}" for name, definition in JOB_EVENT_COLUMN_DEFINITIONS)
    + f",\n    PRIMARY KEY ({', '.join(JOB_EVENT_PRIMARY_KEY)})"
    + f",\n    FOREIGN KEY ({JOB_EVENT_FOREIGN_KEY[0]}) "
    + f"REFERENCES {JOB_EVENT_FOREIGN_KEY[1]} ({JOB_EVENT_FOREIGN_KEY[0]})\n)",
    f"ALTER TABLE {COORDINATION_OUTBOX_TABLE} ADD COLUMN IF NOT EXISTS "
    f"{OUTBOX_DELIVERED_AT_COLUMN} TIMESTAMP(6)",
)


def canonical_event_grants(schema: str, role: str) -> tuple[str, ...]:
    """canonical 事件侧的运行授权面:事件追加/续读,以及 outbox 的那一列。

    一个函数,两侧(写者与投递者)都调它,谁都不重写列清单。列级授权(不是表级)是刻意的:
    ``GRANT SELECT (event_seq, event_digest, payload_ref)`` 让读者读不到它不需要的列 ——
    表级 ``SELECT`` 会随新列悄悄变宽,而读者能读哪几列是这条路径的公开面。写者写的是整行,
    所以它的 ``INSERT`` 清单就是声明本身;能漂的只有读者投影,那句导入期断言守住它。

    事件表上**没有** ``UPDATE``/``DELETE``,outbox 上只授 ``delivered_at``:``delivered_at_us``
    仍是草案列,它的授权面归 ``job_runtime_grants``,本函数不重复声明别人的列。
    """

    # 校验器的唯一实现在 `job_schema`:这里延迟导入,免得 `job_schema` 反过来导入本模块时成环。
    from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

    schema, role = checked_sql_name(schema), checked_sql_name(role)
    statements = [f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"']
    for privilege, columns in JOB_EVENT_PRIVILEGES.items():
        statements.append(
            f'GRANT {privilege} ({", ".join(columns)}) ON "{schema}".{JOB_EVENT_TABLE} TO "{role}"'
        )
    statements.extend(
        (
            f"GRANT INSERT ({OUTBOX_DELIVERED_AT_COLUMN}) "
            f'ON "{schema}".{COORDINATION_OUTBOX_TABLE} TO "{role}"',
            f"GRANT UPDATE ({OUTBOX_DELIVERED_AT_COLUMN}) "
            f'ON "{schema}".{COORDINATION_OUTBOX_TABLE} TO "{role}"',
        )
    )
    return tuple(statements)


__all__ = [
    "COORDINATION_OUTBOX_TABLE",
    "JOB_EVENT_COLUMNS",
    "JOB_EVENT_COLUMN_DEFINITIONS",
    "JOB_EVENT_DDL",
    "JOB_EVENT_FOREIGN_KEY",
    "JOB_EVENT_INSERT_COLUMNS",
    "JOB_EVENT_PRIMARY_KEY",
    "JOB_EVENT_PRIVILEGES",
    "JOB_EVENT_SELECT_COLUMNS",
    "JOB_EVENT_TABLE",
    "OUTBOX_DELIVERED_AT_COLUMN",
    "canonical_event_grants",
]
