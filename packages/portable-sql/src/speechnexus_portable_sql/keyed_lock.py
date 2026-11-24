"""按业务身份串行化的可移植键锁:用一行真实的行锁替代 advisory lock。

为什么不再用 advisory lock:`pg_advisory_xact_lock` 只有 PostgreSQL 有,而它想表达的东西
(「同一个业务键的处理串行,不同键互不影响」)是领域规则,不是数据库特性。受限方言里没有
advisory lock,能表达的只有**行锁**:先让这个键对应的行存在(`INSERT …` 不存在才插),再
`SELECT … FOR UPDATE` 锁住它。两笔同键事务因此必然排队,锁随事务提交或回滚自然释放 ——
不需要解锁语句,也不会像 advisory lock 那样在连接复用时留下残留。

**行的身份是桶号,不是完整键。** 一行一个完整键会让这张表随所有幂等键无界增长,而锁行永远
不能回收(删掉就等于放开互斥)。所以完整键先折成 SHA-256,取前 8 字节对 `KEYED_LOCK_BUCKETS`
取模得到桶号;表里最多 `KEYED_LOCK_BUCKETS` 行,行在第一次被用到时才插入。碰撞契约:

- **同键必同桶。** 推导只依赖键文本,不含进程、连接、时钟或会话状态,所以任何进程、任何
  会话、任何时刻算出的桶号一致 —— 两笔同键事务一定等同一行。
- **异键可能同桶,后果只是竞争。** 碰撞让两笔本来无关的事务互等,吞吐下降,但**不会**让
  一笔事务看见另一笔的重放身份:重放判据始终是完整摘要本身(`mutation_receipts` 一类业务行
  的主键),键锁从不参与「这是不是同一次操作」的判定。这条与
  `speech/infrastructure/workspaces_postgres.py` 写下的口径逐字一致:锁只排序相等的键,
  完整摘要才是主键,碰撞只增加竞争,不能别名一次重放。
- **桶数只影响竞争分布,不影响正确性。** 扩大 `KEYED_LOCK_BUCKETS` 只减少碰撞,不改任何
  判定;它在这里是常量而不是参数,避免同一个键在两处被算出两个桶号。

锁序:这把锁必须在一笔事务里**最先**取,排在 `bucket → canonical Job → public Job/outbox`
之前;本模块不认识那些表,只钉住「键锁是这笔事务的第一把锁」这一条。

时间与身份都由调用方给:本模块不读时钟、不生成身份、不改任何业务行。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from speechnexus_portable_sql.contract import (
    AsyncConnection,
    Connection,
    ContractError,
)
from speechnexus_portable_sql.transaction import (
    in_transaction,
    insert_if_absent,
    insert_if_absent_async,
)
from speechnexus_portable_sql.types import require_text

__all__ = [
    "KEYED_LOCK_BUCKETS",
    "KEYED_LOCK_BUCKET_COLUMN",
    "KEYED_LOCK_COLUMNS",
    "KEYED_LOCK_ROW_LOCK_COLUMN",
    "KEYED_LOCK_TABLE",
    "STATEMENTS",
    "KeyedLock",
    "acquire",
    "acquire_async",
    "bucket_for_key",
    "key_digest",
    "lock_statements",
]

#: 桶数。`KEYED_LOCK_TABLE` 的行数上界就是它;扩大只改变竞争分布,不改变任何判定。
KEYED_LOCK_BUCKETS = 1024

#: 锁行表的唯一名字定义点:各命名空间的 DDL 与授权都从这里取,不各写一份。
KEYED_LOCK_TABLE = "keyed_locks"

#: 行的身份列。桶号是锁行的主键,也是唯一被语句读写的列。
KEYED_LOCK_BUCKET_COLUMN = "bucket_no"

#: 行锁许可列。PostgreSQL 的 `FOR UPDATE` 要求行上某列有 `UPDATE` 权限;这一列就是那份
#: 许可,当前**没有写入者**,也不是 CAS 版本 —— 锁定行本身不改变任何值。
KEYED_LOCK_ROW_LOCK_COLUMN = "version"

#: 列名的唯一清单。派生 DDL、列级授权与语句文本都按它取,免得加一列要改四处。
KEYED_LOCK_COLUMNS: tuple[str, ...] = (KEYED_LOCK_BUCKET_COLUMN, KEYED_LOCK_ROW_LOCK_COLUMN)

#: statement ID → SQL 模板。`{target}` 只能是表名或 `schema.表名`,标识符不能参数化,
#: 所以它在发出前由 `lock_statements()` 用一份校验过的名字替换。
STATEMENTS = {
    "keyed_lock.insert": f"INSERT INTO {{target}} ({KEYED_LOCK_BUCKET_COLUMN}) VALUES (?)",
    "keyed_lock.lock": (
        f"SELECT {KEYED_LOCK_BUCKET_COLUMN} FROM {{target}} "
        f"WHERE {KEYED_LOCK_BUCKET_COLUMN} = ? FOR UPDATE"
    ),
}

#: 命名空间标识符:与 `schema_identifier` / `checked_sql_name` 同一条字符集,这里只做收窄,
#: 不做任何改写(写错了要在发出前失败,而不是被悄悄折成合法名字)。
_NAMESPACE = re.compile(r"\A[a-z][a-z0-9_]{0,62}\Z")


@dataclass(frozen=True, slots=True)
class KeyedLock:
    """一次键锁的结果:锁住的行、完整键的摘要、以及这一行是否由本事务创建.

    `digest` 只用于日志与证据:它是完整键(域 + 业务范围 + 幂等键摘要)的摘要,不参与行的
    身份,也不参与重放判定。`created` 说明这次调用是不是第一个用到这个桶的事务。
    """

    bucket_no: Decimal
    digest: str
    created: bool


def _target(schema: str | None) -> str:
    """把命名空间折成语句里的表引用;`None` 表示按会话 `search_path` 解析.

    标识符不能被参数化,所以这里是唯一的插值点:名字先过字符集,再拼进已登记的模板。
    `public` / `information_schema` / `pg_` 前缀直接拒绝 —— 锁行不是公共对象,也不该落进
    系统命名空间。
    """

    if schema is None:
        return KEYED_LOCK_TABLE
    if not _NAMESPACE.match(schema) or schema in {"public", "information_schema"}:
        raise ContractError("schema must be a plain lowercase SQL identifier")
    if schema.startswith("pg_"):
        raise ContractError("schema must not name a system namespace")
    return f"{schema}.{KEYED_LOCK_TABLE}"


def lock_statements(schema: str | None = None) -> tuple[str, str]:
    """`(插入, 加锁)` 两条语句,表引用按 `schema` 解析。

    返回文本而不是执行:调用方可能要把语句喂给带 `row_factory` 的游标,或者在测试里逐字
    比对。两条语句的顺序不能交换 —— 先保证行存在,再锁它。
    """

    target = _target(schema)
    return (
        STATEMENTS["keyed_lock.insert"].format(target=target),
        STATEMENTS["keyed_lock.lock"].format(target=target),
    )


def key_digest(key: str) -> str:
    """完整键的摘要(`sha256:` + 十六进制),只作证据,不是行身份。"""

    text = require_text(key, field="key", max_bytes=1024)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def bucket_for_key(key: str, *, buckets: int = KEYED_LOCK_BUCKETS) -> Decimal:
    """完整键 → 桶号:`sha256(键)` 前 8 字节对 `buckets` 取模。

    不取 `hash()`、不用进程内随机盐、不读时钟:同一个键在任何进程里都必须落到同一行。
    """

    text = require_text(key, field="key", max_bytes=1024)
    if buckets < 1:
        raise ContractError("bucket count must be at least 1")
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return Decimal(int.from_bytes(digest[:8], "big") % buckets)


def _require_transaction(connection: object) -> None:
    """没有事务块就拿不住键锁:语句一结束锁就释放,等于什么都没锁.

    静默退化成「无锁」是最坏的结果,所以这里明确失败。判定与 `insert_if_absent` 共用
    `transaction.in_transaction`:它区分「`autocommit` 模式下没有事务」和「`autocommit` 模式
    下调用方显式开了一个事务块」—— 后者能开保存点,也能持锁到提交。
    """

    if not in_transaction(connection):
        raise ContractError("a keyed lock needs an open transaction, not a bare autocommit call")


def acquire(connection: Connection, *, key: str, schema: str | None = None) -> KeyedLock:
    """在调用方的事务里锁住 `key` 对应的行,返回锁住的桶号.

    顺序固定:插入(已存在则只回滚到保存点)→ `SELECT … FOR UPDATE`。插入那一步只为了让行
    存在,行的内容与业务无关;唯一冲突在这里是**正常路径**(并发写者先到),由
    `insert_if_absent` 翻成 `False`,事务保持可用。

    事务边界由调用方拥有:本函数不 `commit` 也不 `rollback`。同一事务内重复取同一个键不
    自阻塞(第二次插入走冲突分支,行锁已在手)。
    """

    _require_transaction(connection)
    insert_statement, lock_statement = lock_statements(schema)
    bucket = bucket_for_key(key)
    digest = key_digest(key)
    cursor = connection.cursor()
    try:
        created = insert_if_absent(cursor, insert_statement, (bucket,))
        cursor.execute(lock_statement, (bucket,))
        if len(cursor.fetchall()) != 1:
            raise ContractError(
                f"{KEYED_LOCK_TABLE} row {bucket} disappeared between insert and lock"
            )
    finally:
        cursor.close()
    return KeyedLock(bucket_no=bucket, digest=digest, created=created)


async def acquire_async(
    connection: AsyncConnection, *, key: str, schema: str | None = None
) -> KeyedLock:
    """`acquire` 的异步形态:同一组语句、同一个桶号、同一份碰撞契约.

    异步连接上的游标是 `await` 的,所以插入那一步走 `insert_if_absent_async`;两条语句与
    同步形态逐字相同(都由 `lock_statements` 生成),不存在第二份 SQL。
    """

    _require_transaction(connection)
    insert_statement, lock_statement = lock_statements(schema)
    bucket = bucket_for_key(key)
    digest = key_digest(key)
    cursor = connection.cursor()
    try:
        created = await insert_if_absent_async(cursor, insert_statement, (bucket,))
        await cursor.execute(lock_statement, (bucket,))
        if len(await cursor.fetchall()) != 1:
            raise ContractError(
                f"{KEYED_LOCK_TABLE} row {bucket} disappeared between insert and lock"
            )
    finally:
        await cursor.close()
    return KeyedLock(bucket_no=bucket, digest=digest, created=created)
