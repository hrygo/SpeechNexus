"""键锁行的授权面与形状只有一个定义点,两个角色拿到逐字相同的语句。

被替代的那把 catalog 互斥两侧是 `speech` schema 里的**两个不同角色**(backend 与执行网关)。
互斥成立的前提是两侧锁到**同一行**,而两侧锁到同一行的前提是两侧拿到**同一份**授权面:
只要网关那份少列一列或多了表级 `UPDATE`,失败形态就是 `FOR UPDATE` 的 `42501`(PostgreSQL
只报表名,不点名缺哪列),或者更糟 —— 网关拿不到锁却照样继续,互斥**静默裂成两把**。

所以这里的判据不是「两边各自看起来合理」,而是:

* 两个授权入口返回**逐字相同**的元组(网关那份是委托,不是副本);
* 三个列元组的并集**恰好**是 ``KEYED_LOCK_COLUMNS``:原语加一列而不在这里给授权决定时,
  导入期断言先红,而不是等首次真实加锁;
* DDL 的列顺序是「原语清单 + created_at」,`created_at` 不进任何授权元组(它不产生权限事实);
* 授权是列级而不是表级 —— 表级 `UPDATE` 会让角色顺手改桶号,也就是改行身份。

形状与并发行为本身由 `tests/integration/test_coordination_keyed_lock_pg.py` 在真库、真角色
上验证;那里是本文件断言的终点。
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.coordination_keyed_lock_schema import (
    COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN,
    COORDINATION_KEYED_LOCK_DDL,
    COORDINATION_KEYED_LOCK_INSERT_COLUMNS,
    COORDINATION_KEYED_LOCK_SELECT_COLUMNS,
    COORDINATION_KEYED_LOCK_TABLE,
    COORDINATION_KEYED_LOCK_UPDATE_COLUMNS,
    coordination_keyed_lock_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_keyed_lock_grants,
    gateway_query_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import job_dispatch_grants
from speechnexus_portable_sql.keyed_lock import KEYED_LOCK_BUCKET_COLUMN, KEYED_LOCK_COLUMNS

SCHEMA = "sn_synthetic"
BACKEND_ROLE = "sn_backend_synthetic"
GATEWAY_ROLE = "sn_gateway_synthetic"


def test_both_grants_answer_the_same_statements_for_the_same_role() -> None:
    """两个入口是同一个函数:换角色、换 schema 都不会分叉。"""

    assert gateway_keyed_lock_grants(SCHEMA, BACKEND_ROLE) == coordination_keyed_lock_grants(
        SCHEMA, BACKEND_ROLE
    )
    assert gateway_keyed_lock_grants(SCHEMA, GATEWAY_ROLE) == coordination_keyed_lock_grants(
        SCHEMA, GATEWAY_ROLE
    )


def test_the_backend_writer_surface_contains_that_exact_surface() -> None:
    """backend 侧的语句必须**逐字**出现在 ``job_dispatch_grants`` 里,不是等价物。

    集合比较会在顺序或列清单被重写时仍然通过;这里比的是元组包含关系,所以 `job_dispatch_grants`
    里若出现手写的第二份键锁授权(列序不同、少一列),这条红。
    """

    backend = coordination_keyed_lock_grants(SCHEMA, BACKEND_ROLE)
    dispatch = job_dispatch_grants(SCHEMA, BACKEND_ROLE)

    assert backend, "the lock surface must not be empty"
    assert all(statement in dispatch for statement in backend)
    named = [statement for statement in backend if COORDINATION_KEYED_LOCK_TABLE in statement]
    assert len(named) == 3, "a role gets read, lazy-create and the row-lock allowance"
    assert [
        statement for statement in dispatch if COORDINATION_KEYED_LOCK_TABLE in statement
    ] == named, "the writer's copy must be those three statements, in that order"


def test_the_gateway_reader_role_is_not_widened() -> None:
    """只读读者的闭包拒绝任何 INSERT/UPDATE,所以它绝不能拿到锁行。"""

    reader = gateway_query_grants(SCHEMA, GATEWAY_ROLE)

    assert not any(COORDINATION_KEYED_LOCK_TABLE in statement for statement in reader)


def test_every_ported_column_has_a_privilege_decision() -> None:
    """三个元组的并集 == 原语清单:新列必须在这里就有归属,哪怕只是「读」这一种。

    同一列可以同时出现在 `SELECT` 与 `INSERT` 里(`bucket_no` 既被读回也被插入),所以判据是
    **并集**而不是「每个名字只出现一次」;判据的反面是漏一列 —— 那会在首次真实加锁时以
    `42501` 失败,而报错不带列名。
    """

    decisions = {
        *COORDINATION_KEYED_LOCK_SELECT_COLUMNS,
        *COORDINATION_KEYED_LOCK_INSERT_COLUMNS,
        *COORDINATION_KEYED_LOCK_UPDATE_COLUMNS,
    }

    assert decisions == set(KEYED_LOCK_COLUMNS)
    for column in KEYED_LOCK_COLUMNS:
        assert column in decisions, f"{column} has no privilege decision"


def test_the_row_lock_column_is_the_only_update_and_is_not_a_cas_version() -> None:
    """`version` 只是 `FOR UPDATE` 的许可列:没有写入者,所以 UPDATE 面就是它一列。"""

    assert COORDINATION_KEYED_LOCK_UPDATE_COLUMNS == ("version",)
    assert COORDINATION_KEYED_LOCK_SELECT_COLUMNS == (KEYED_LOCK_BUCKET_COLUMN,)
    assert COORDINATION_KEYED_LOCK_INSERT_COLUMNS == (KEYED_LOCK_BUCKET_COLUMN,)


def test_the_ddl_is_the_ported_columns_plus_created_at_in_that_order() -> None:
    """DDL 的列顺序是判据:换序会让既有行的列位置对不上迁移树里的说明。"""

    assert len(COORDINATION_KEYED_LOCK_DDL) == 1
    ddl = COORDINATION_KEYED_LOCK_DDL[0]
    names = [
        line.strip().split(" ", 1)[0]
        for line in ddl.splitlines()
        if line.startswith("    ") and not line.strip().startswith("PRIMARY KEY")
    ]

    assert names == [*KEYED_LOCK_COLUMNS, COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN]
    assert COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN not in {
        *COORDINATION_KEYED_LOCK_SELECT_COLUMNS,
        *COORDINATION_KEYED_LOCK_INSERT_COLUMNS,
        *COORDINATION_KEYED_LOCK_UPDATE_COLUMNS,
    }
    assert f"PRIMARY KEY ({KEYED_LOCK_BUCKET_COLUMN})" in ddl


def test_the_grants_are_column_level_and_quote_the_identifiers() -> None:
    """列级而非表级:表级 `UPDATE` 会让角色能改桶号,也就是改行身份。"""

    statements = coordination_keyed_lock_grants(SCHEMA, BACKEND_ROLE)

    assert any("GRANT SELECT (bucket_no)" in statement for statement in statements)
    assert any("GRANT INSERT (bucket_no)" in statement for statement in statements)
    assert any("GRANT UPDATE (version)" in statement for statement in statements)
    assert not any(
        "GRANT UPDATE ON" in statement or "GRANT INSERT ON" in statement for statement in statements
    ), "a table-level INSERT/UPDATE would cover columns the runtime never writes"
    target = f'"{SCHEMA}".{COORDINATION_KEYED_LOCK_TABLE}'
    assert all(target in statement for statement in statements[1:])
    assert all(f'TO "{BACKEND_ROLE}"' in statement for statement in statements)
    assert statements[0] == f'GRANT USAGE ON SCHEMA "{SCHEMA}" TO "{BACKEND_ROLE}"'
