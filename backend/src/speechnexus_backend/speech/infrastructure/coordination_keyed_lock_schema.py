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
桶号是 Python 侧算出的 ``Decimal``)、``created_at``(行是懒建的,记下它第一次被用到的时间
供排查锁竞争用;没有任何运行角色读写这一列,所以它不产生授权事实)、以及主键。

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

from speechnexus_portable_sql.keyed_lock import (
    KEYED_LOCK_BUCKET_COLUMN,
    KEYED_LOCK_COLUMNS,
    KEYED_LOCK_ROW_LOCK_COLUMN,
)

#: 协调命名空间里的表名。workbench 那张叫 `keyed_locks`;这里沿用 ``coordination_`` 前缀,
#: 与 `coordination_jobs` / `coordination_outbox` / `coordination_claim_buckets` 一致。
COORDINATION_KEYED_LOCK_TABLE = "coordination_keyed_locks"

#: 行首次被用到的时间。**唯一**一个不出现在 ``KEYED_LOCK_COLUMNS`` 里的列:它不参与加锁、
#: 不被任何运行角色读写,所以它不引入第四条授权事实。
COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN = "created_at"

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

#: 逐列的物理类型。顺序与 ``KEYED_LOCK_COLUMNS`` 一致,`created_at` 排在最后。
_COLUMN_DEFINITIONS: tuple[tuple[str, str], ...] = (
    (KEYED_LOCK_BUCKET_COLUMN, "DECIMAL(38,0) NOT NULL"),
    (KEYED_LOCK_ROW_LOCK_COLUMN, "DECIMAL(38,0) NOT NULL DEFAULT 0"),
    (
        COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN,
        "TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP",
    ),
)

assert tuple(name for name, _ in _COLUMN_DEFINITIONS) == (
    *KEYED_LOCK_COLUMNS,
    COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN,
), "the DDL column order must be the portable declaration plus created_at"

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


__all__ = [
    "COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN",
    "COORDINATION_KEYED_LOCK_DDL",
    "COORDINATION_KEYED_LOCK_INSERT_COLUMNS",
    "COORDINATION_KEYED_LOCK_SELECT_COLUMNS",
    "COORDINATION_KEYED_LOCK_TABLE",
    "COORDINATION_KEYED_LOCK_UPDATE_COLUMNS",
    "coordination_keyed_lock_grants",
]
