"""canonical 事件侧的加法 DDL:``job_events`` 表与 outbox 的 ``delivered_at`` 列。

**这张表为什么现在才出现在 alembic 树里。** canonical 形状从
``migrations/0003_portable_claim_buckets.sql`` 起就声明了 ``job_events`` 与
``coordination_outbox.delivered_at``,但生产迁移树到 ``0067`` 为止**没有**这张表,而 outbox 上
只有草案的 ``delivered_at_us BIGINT``。S0 之前事件读路径仍在草案 ``speech_job_events`` 上,所以
缺口一直没有以 42P01 显形;一旦按计划把事件写者/读者切到 canonical 对象,第一步就会打在不存在
的关系上。本模块补**对象**(S0)、追加**写面**(S1/S1b),S2 起同时是**读面**的唯一声明点。

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

* ``SELECT (event_seq, event_digest, payload_ref, job_id)``:``job_event.read`` 的**读面**,
  即投影加谓词列。投影与谓词分开声明,是因为投影必须逐字等于语句的 ``SELECT`` 列表(那条
  判据由 ``tests/ci/test_canonical_event_statement_parity.py`` 钉住),而 ``job_id`` 只出现在
  ``WHERE`` 里。列级 ``SELECT`` 少授 ``job_id`` 时整条续读以 ``42501`` 失败 —— S1b 实测到的
  那个缺口(S2 补上,见下);
* ``INSERT (job_id, event_seq, event_digest, payload_ref)``:``job_event.insert`` 的列;
* outbox 上的 ``INSERT (delivered_at)`` 与 ``UPDATE (delivered_at)``:``outbox.insert`` 与
  ``outbox.mark_delivered`` 只碰这一列。

事件表**没有** ``UPDATE``/``DELETE``:事件是只追加的事实,行级改动会同时破坏
``(job_id, event_seq)`` 的连续性判据与摘要。两条导入期断言守着这份授权面:读者投影必须落在声明
之内(列改名而投影没跟上时立刻失败),权限面必须恰好是 ``SELECT``/``INSERT``(给事件表加
``UPDATE`` 得先改那句断言,不会悄悄出现在授权里)。

**授权面按阶段拆成两个函数,不是一个带开关的函数。** 事件表自己的那一份
(``job_event_grants``)在 S1 有了调用点:派发身份在 ``job_dispatch_grants`` 里拿到它的列级
``SELECT``/``INSERT``,事件双写才有权限落库。outbox 的 ``delivered_at`` 仍没有写者,所以它留在
``canonical_event_grants`` 里,等 S2 的投递路径接过来 —— 现在把两半一起发给每个身份,等于给每个
登录一批「已授权、无语句」的列,而 ``job_runtime`` 的逐列比对拒绝未声明的授权面,那不是加法。
`job_event_grants` 与 `canonical_event_grants` 共用同一份 ``JOB_EVENT_PRIVILEGES``,列清单不复制。
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

#: 事件续读的列:``job_event.read`` 的投影逐字如此(``job_id`` 只出现在 ``WHERE``,不在投影里)。
JOB_EVENT_SELECT_COLUMNS: tuple[str, ...] = ("event_seq", "event_digest", "payload_ref")

#: 续读语句在**投影之外**引用的列:``job_event.read`` 的 ``WHERE job_id = ? AND event_seq > ?``
#: 中不属于投影的那些(``event_seq`` 已在投影里,所以这里只剩 ``job_id``)。
#:
#: 为什么投影不够:PostgreSQL 的列级 ``SELECT`` 只授投影列时,``WHERE`` 里的列同样要求可读,
#: 否则整条语句以 ``42501 permission denied for table job_events`` 失败 —— 而报错不点名缺哪列。
#: S1b 实测过这个形状(2026-11-25,非属主身份):授予面是
#: ``SELECT (event_seq, event_digest, payload_ref)``,语句一跑就 42501。S2 是第一次真正执行
#: 那条语句的阶段,所以补授 ``job_id`` 与读翻转必须同一提交,否则新读路径一上线就红。
#:
#: 这一份与投影分开声明,而不是把 ``job_id`` 塞进 ``JOB_EVENT_SELECT_COLUMNS``:投影要逐字
#: 等于语句的 ``SELECT`` 列表(``tests/ci/test_canonical_event_statement_parity.py`` 逐字解析),
#: 塞进去会让那条判据变成假的。谓词列本身也由同一份判据对着语句文本核对,所以这里仍然只有
#: 一个定义点。
JOB_EVENT_READ_FILTER_COLUMNS: tuple[str, ...] = ("job_id",)

#: ``job_event.read`` 需要的完整读面:投影 + 谓词列。派发身份能跑那条语句,就是这份读面完整
#: 的定义;``JOB_EVENT_PRIVILEGES`` 从这里派生,不再手写第三份列清单。
JOB_EVENT_READ_COLUMNS: tuple[str, ...] = (
    *JOB_EVENT_SELECT_COLUMNS,
    *JOB_EVENT_READ_FILTER_COLUMNS,
)

#: 事件追加的列:``job_event.insert`` 逐字如此。
JOB_EVENT_INSERT_COLUMNS: tuple[str, ...] = JOB_EVENT_COLUMNS

#: 事件表的权限面。事件是只追加的事实,所以这里**没有** ``UPDATE`` 与 ``DELETE``。
JOB_EVENT_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "SELECT": JOB_EVENT_READ_COLUMNS,
    "INSERT": JOB_EVENT_INSERT_COLUMNS,
}

# ``INSERT`` 就是整行(``job_event.insert`` 写四个声明列),所以它按构造覆盖声明面,不可能缺列;
# 会漂的只有读面 —— 投影列改名而语句没跟上时,第一条续读以 ``42703`` 失败;谓词列没授时,
# 第一条续读以 ``42501`` 失败。这条断言把两种漂移都挪到导入期。
assert set(JOB_EVENT_READ_COLUMNS) <= set(JOB_EVENT_COLUMNS), (
    "the reader grants a column the declaration does not have; renaming a column here "
    "without updating the projection or the predicate fails the first event read"
)

assert set(JOB_EVENT_PRIVILEGES["SELECT"]) == set(JOB_EVENT_READ_COLUMNS), (
    "the SELECT grant must be exactly the read surface: a projection-only grant fails the "
    "first resume with 42501 because job_event.read filters on job_id"
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
#:
#: **InnoDB 验证 schema 没有这张表,这不是遗漏。** 那份 schema 由 ``JOB_DDL`` /
#: ``CANONICAL_JOB_DDL`` / ``CLAIM_BUCKET_DDL`` / ``COORDINATION_KEYED_LOCK_DDL`` 建出来,而
#: 事件双写的两条迁移只存在于 ``PostgresDispatchStateStore``:``DispatchStateStore`` 目前只有
#: 这一个实现,``_record_progress`` / ``_finish`` 在 InnoDB 上没有写入者,建表就等于造一个没有
#: 语句的镜像。等到派发状态机真的在 InnoDB 上跑,这张表**必须由 ``JOB_EVENT_DDL[0]`` 派生**
#: (逐字转换 PG 专属语法,如 ``VARCHAR``/``DECIMAL``/外键写法),不允许再抄一份列清单:
#: ``job_dispatch_grants`` 与运行期写入读的是本模块,两份清单漂一列只在首次真实写入时以
#: ``42501`` 显形,而两个引擎都不点名缺哪列。
JOB_EVENT_DDL: tuple[str, ...] = (
    f"CREATE TABLE IF NOT EXISTS {JOB_EVENT_TABLE} (\n    "
    + ",\n    ".join(f"{name} {definition}" for name, definition in JOB_EVENT_COLUMN_DEFINITIONS)
    + f",\n    PRIMARY KEY ({', '.join(JOB_EVENT_PRIMARY_KEY)})"
    + f",\n    FOREIGN KEY ({JOB_EVENT_FOREIGN_KEY[0]}) "
    + f"REFERENCES {JOB_EVENT_FOREIGN_KEY[1]} ({JOB_EVENT_FOREIGN_KEY[0]})\n)",
    f"ALTER TABLE {COORDINATION_OUTBOX_TABLE} ADD COLUMN IF NOT EXISTS "
    f"{OUTBOX_DELIVERED_AT_COLUMN} TIMESTAMP(6)",
)


def _checked(schema: str, role: str) -> tuple[str, str]:
    """标识符校验的唯一实现在 ``job_schema``:延迟导入,免得两个模块互相导入时成环。"""

    from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

    return checked_sql_name(schema), checked_sql_name(role)


def job_event_grants(schema: str, role: str) -> tuple[str, ...]:
    """``job_events`` 自己的运行授权面:事件追加与续读,只有这两件事。

    S1 的事件写者只需要 ``INSERT`` 四列(整行就是声明本身)。续读(S2 起)要的是
    ``JOB_EVENT_READ_COLUMNS``:投影三列**加** ``job_id``。少了 ``job_id`` 这条读面是不完整的
    —— 列级 ``SELECT`` 只授投影时,``job_event.read`` 的 ``WHERE job_id = ?`` 会让整条语句以
    ``42501`` 失败,而报错不点名缺哪列。``job_id`` 属于读面而不是投影,所以它单独声明、
    由同一份判据对着语句文本核对。

    列级授权(不是表级)是刻意的:表级 ``SELECT`` 会随新列悄悄变宽,而读者能读哪几列是这条路径
    的公开面。事件表上**没有** ``UPDATE``/``DELETE`` —— 事件是只追加的事实。

    这里不碰 outbox:``delivered_at`` 到 S2 才有写者,它的授权面在
    ``canonical_event_grants``,两个函数共用同一份 ``JOB_EVENT_PRIVILEGES``。
    """

    schema, role = _checked(schema, role)
    statements = [f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"']
    for privilege, columns in JOB_EVENT_PRIVILEGES.items():
        statements.append(
            f'GRANT {privilege} ({", ".join(columns)}) ON "{schema}".{JOB_EVENT_TABLE} TO "{role}"'
        )
    return tuple(statements)


def canonical_event_grants(schema: str, role: str) -> tuple[str, ...]:
    """canonical 事件侧的完整授权面:``job_event_grants`` 加 outbox 的那一列。

    事件追加/续读那一半来自 ``job_event_grants``,不在这里重写列清单。outbox 上只授
    ``delivered_at``:``delivered_at_us`` 仍是草案列,它的授权面归 ``job_runtime_grants``,
    本函数不重复声明别人的列。
    """

    schema, role = _checked(schema, role)
    return (
        *job_event_grants(schema, role),
        f"GRANT INSERT ({OUTBOX_DELIVERED_AT_COLUMN}) "
        f'ON "{schema}".{COORDINATION_OUTBOX_TABLE} TO "{role}"',
        f"GRANT UPDATE ({OUTBOX_DELIVERED_AT_COLUMN}) "
        f'ON "{schema}".{COORDINATION_OUTBOX_TABLE} TO "{role}"',
    )


__all__ = [
    "COORDINATION_OUTBOX_TABLE",
    "JOB_EVENT_COLUMNS",
    "JOB_EVENT_COLUMN_DEFINITIONS",
    "JOB_EVENT_DDL",
    "JOB_EVENT_FOREIGN_KEY",
    "JOB_EVENT_INSERT_COLUMNS",
    "JOB_EVENT_PRIMARY_KEY",
    "JOB_EVENT_PRIVILEGES",
    "JOB_EVENT_READ_COLUMNS",
    "JOB_EVENT_READ_FILTER_COLUMNS",
    "JOB_EVENT_SELECT_COLUMNS",
    "JOB_EVENT_TABLE",
    "OUTBOX_DELIVERED_AT_COLUMN",
    "canonical_event_grants",
    "job_event_grants",
]
