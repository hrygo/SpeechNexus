"""协调命名空间的键锁行表:DDL 与授权面的唯一定义点。

**为什么这张表在 `speech`,而不是在使用方的命名空间。** 要串行化的那个键
(`speechnexus:catalog:…:binding:<id>`)两侧分别是 backend 与执行网关,它们是 `speech`
schema 里的两个不同角色。锁行建在其中一侧够不到的命名空间里,另一侧就摸不到同一行 ——
互斥会**静默裂成两把**,而两侧依赖的正是「谁先谁后」这一份排序。所以表落在两侧都已有关系的
`speech`,授权也从两侧各自的定义点派生:backend 是
``job_schema.job_dispatch_grants``,网关是
``gateway_query_grants.gateway_keyed_lock_grants``。两份都只调用本模块的
``coordination_keyed_lock_grants``:列清单、语句文本、DDL 都不在别处重写。

**形状。** 与 workbench 的 `keyed_locks` 同源。桶号与列名来自
``speechnexus_portable_sql.keyed_lock`` 的唯一清单(``KEYED_LOCK_COLUMNS``),本模块只补三方
面命名空间自己的东西:物理类型(``DECIMAL(38,0)``,与 ``coordination_claim_buckets`` 一致,
桶号是 Python 侧算出的 ``Decimal``)与主键。表里没有时间戳列:它没有写者也没有读者,
而带小数的 ``TIMESTAMP(6)`` 上 ``DEFAULT CURRENT_TIMESTAMP`` 在 MySQL 是非法默认值,
两引擎在这一处拼写不同,少一列就少一处差异。

**授权面按事实拆,不按表拆。** 键锁对运行角色需要三件事,每件事只有一个定义点:

* ``SELECT (bucket_no)``:加锁语句按桶号读回自己锁住的那一行;
* ``INSERT (bucket_no)``:桶行懒建,第一次用到该桶的事务把它插进来;
* ``UPDATE (version)``:``FOR UPDATE`` 的行锁许可。PostgreSQL 要求行上某列有 ``UPDATE``
  权限才肯锁行;``version`` 就是那份许可,当前**没有写入者**,也不是 CAS 版本。

三个元组与 ``KEYED_LOCK_COLUMNS`` 的并集必须相等(下面的断言):给
``speechnexus_portable_sql.keyed_lock`` 加一列,这里立刻导入失败,而不是新增一列「谁都没
授权、也没人发现」的列。这正是 ``coordination_jobs`` 的 INSERT 列曾经漂成五份、加一列要
返工两次的那种失效。
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.job_schema_preflight import (
    require_migrated_schema,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection
from speechnexus_portable_sql.keyed_lock import (
    COORDINATION_KEYED_LOCK_TABLE,
    KEYED_LOCK_BUCKET_COLUMN,
    KEYED_LOCK_COLUMNS,
    KEYED_LOCK_ROW_LOCK_COLUMN,
)

#: 协调命名空间里的表名。名字的唯一字面量在 ``speechnexus_portable_sql.keyed_lock`` ——
#: 执行网关是独立部署、导入不了本模块,而两侧必须锁同一张表,所以物理名字留在两者共享的
#: 底层,这里只再导出(DDL 与授权仍只由本模块声明)。
__all__ = [
    "COORDINATION_KEYED_LOCK_DDL",
    "COORDINATION_KEYED_LOCK_INSERT_COLUMNS",
    "COORDINATION_KEYED_LOCK_SELECT_COLUMNS",
    "COORDINATION_KEYED_LOCK_TABLE",
    "COORDINATION_KEYED_LOCK_UPDATE_COLUMNS",
    "coordination_keyed_lock_grants",
    "require_coordination_keyed_lock_grants",
]

#: 加锁语句读的列。与 ``keyed_lock.STATEMENTS['keyed_lock.lock']`` 的投影逐字一致。
COORDINATION_KEYED_LOCK_SELECT_COLUMNS: tuple[str, ...] = (KEYED_LOCK_BUCKET_COLUMN,)

#: 懒建桶行时写入的列。与 ``keyed_lock.STATEMENTS['keyed_lock.insert']`` 的列清单一致。
COORDINATION_KEYED_LOCK_INSERT_COLUMNS: tuple[str, ...] = (KEYED_LOCK_BUCKET_COLUMN,)

#: 行锁许可列(不是 CAS 版本,也没有写入者)。
COORDINATION_KEYED_LOCK_UPDATE_COLUMNS: tuple[str, ...] = (KEYED_LOCK_ROW_LOCK_COLUMN,)

assert (
    set(COORDINATION_KEYED_LOCK_SELECT_COLUMNS)
    | set(COORDINATION_KEYED_LOCK_INSERT_COLUMNS)
    | set(COORDINATION_KEYED_LOCK_UPDATE_COLUMNS)
) == set(KEYED_LOCK_COLUMNS), (
    "coordination_keyed_locks must cover every column of KEYED_LOCK_COLUMNS with a "
    "privilege decision; a new column in the portable keyed lock needs one here"
)

#: 逐列的物理类型。顺序与 ``KEYED_LOCK_COLUMNS`` 一致。
#:
#: 表里**没有** ``created_at``:它没有写者也没有读者,而带小数的 ``TIMESTAMP(6)`` 上
#: ``DEFAULT CURRENT_TIMESTAMP`` 在 MySQL 是非法默认值(必须写 ``CURRENT_TIMESTAMP(6)``),
#: 两引擎在这一处拼写不同。少一列就少一处引擎差异。
_COLUMN_DEFINITIONS: tuple[tuple[str, str], ...] = (
    (KEYED_LOCK_BUCKET_COLUMN, "DECIMAL(38,0) NOT NULL"),
    (KEYED_LOCK_ROW_LOCK_COLUMN, "DECIMAL(38,0) NOT NULL DEFAULT 0"),
)

assert tuple(name for name, _ in _COLUMN_DEFINITIONS) == KEYED_LOCK_COLUMNS, (
    "the DDL columns must be exactly the portable declaration: a column outside it has no "
    "writer and no reader, and an extra defaulted timestamp is the one thing PostgreSQL and "
    "MySQL spell differently (CURRENT_TIMESTAMP vs CURRENT_TIMESTAMP(6))"
)

#: 建表语句的唯一副本:生产由 alembic ``0067`` 执行,集成用例按同一份装配。
#: ``IF NOT EXISTS`` 与迁移树其余部分一致:若干集成用例把 ``alembic_version`` 回退再
#: ``upgrade head``,那时对象可能已经在。
COORDINATION_KEYED_LOCK_DDL: tuple[str, ...] = (
    f"CREATE TABLE IF NOT EXISTS {COORDINATION_KEYED_LOCK_TABLE} (\n    "
    + ",\n    ".join(f"{name} {definition}" for name, definition in _COLUMN_DEFINITIONS)
    + f",\n    PRIMARY KEY ({KEYED_LOCK_BUCKET_COLUMN})\n)",
)


def coordination_keyed_lock_grants(schema: str, role: str) -> tuple[str, ...]:
    """键锁行的完整运行授权面:`SELECT` + 懒建 `INSERT` + 行锁 `UPDATE`。

    两个角色拿到的授权**逐字相同**,所以这里只有一份函数:backend 与网关都调它,谁都不
    重写列清单。列级授权(不是表级)是刻意的:``GRANT UPDATE (version)`` 让角色不能顺手改
    桶号(行身份),``GRANT INSERT (bucket_no)`` 让懒建语句只能写身份列。
    """

    # 校验器的唯一实现在 `job_schema`:这里延迟导入,免得 `job_schema` 反过来导入本模块时成环。
    from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

    schema, role = checked_sql_name(schema), checked_sql_name(role)
    target = f'"{schema}".{COORDINATION_KEYED_LOCK_TABLE}'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT SELECT ({', '.join(COORDINATION_KEYED_LOCK_SELECT_COLUMNS)}) "
        f'ON {target} TO "{role}"',
        f"GRANT INSERT ({', '.join(COORDINATION_KEYED_LOCK_INSERT_COLUMNS)}) "
        f'ON {target} TO "{role}"',
        f"GRANT UPDATE ({', '.join(COORDINATION_KEYED_LOCK_UPDATE_COLUMNS)}) "
        f'ON {target} TO "{role}"',
    )


def require_coordination_keyed_lock_grants(
    connection: SpeechConnection, schema: str, *, subject: str
) -> None:
    """启动闸门:这个身份此刻就能取协调键锁,缺表或缺列在启动时点名.

    这张表原先登记为「允许但不要求」,因为还没有调用点用它;绑定禁用栅栏迁移到行锁之后,
    取不到它的角色不再只是少一条授权,而是**第一条锁语句就 42501**。所以判据从「允许」
    改成「必须有」:关系不存在或列级许可缺一列,都在启动核对时按名字失败。

    三件事分别判,不合成一句 ``has_table_privilege``:列级授权下按表判恒为假,会把一份
    正确的列级授权判成缺授权。存在性先判 —— ``has_column_privilege`` 对不存在的关系是
    报错而不是返回空,先查再判才能给出「没跑迁移」这个可读结论(#920 的同一条理由)。
    """

    # 存在性复用唯一的「关系是否已迁移」判定点,不在这里另写一份 pg_class 查询。
    require_migrated_schema(connection, schema, (COORDINATION_KEYED_LOCK_TABLE,))
    qualified = f"{schema}.{COORDINATION_KEYED_LOCK_TABLE}"
    for privilege, columns in (
        ("SELECT", COORDINATION_KEYED_LOCK_SELECT_COLUMNS),
        ("INSERT", COORDINATION_KEYED_LOCK_INSERT_COLUMNS),
        ("UPDATE", COORDINATION_KEYED_LOCK_UPDATE_COLUMNS),
    ):
        for column in columns:
            row = connection.execute(
                "SELECT pg_catalog.has_column_privilege(CURRENT_USER, ?, ?, ?) AS held",
                (qualified, column, privilege),
            ).fetchone()
            if row is None or not row["held"]:
                raise ValueError(
                    f"{subject} cannot take the {COORDINATION_KEYED_LOCK_TABLE} row lock: "
                    f"missing {privilege} on {COORDINATION_KEYED_LOCK_TABLE}.{column}; "
                    "re-provision the role from its grant function"
                )
