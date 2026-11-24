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

**跨部署的身份。** 同一个键可能由两个独立部署各取一次(backend 的绑定禁用栅栏与执行网关
的提交屏障),所以表名(`COORDINATION_KEYED_LOCK_TABLE`)与键文本(`catalog_lock_key`)都定义
在本包:两侧都调同一个函数,「同键 → 同一行」才是构造上的结论,而不是两处字符串碰巧相同。
行锁落在这笔事务的第一把锁的位置上 —— 它之前不许有任何行锁或 advisory lock。

时间与身份都由调用方给:本模块不读时钟、不生成身份、不改任何业务行。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

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
    "COORDINATION_KEYED_LOCK_TABLE",
    "KEYED_LOCK_BUCKETS",
    "KEYED_LOCK_BUCKET_COLUMN",
    "KEYED_LOCK_COLUMNS",
    "KEYED_LOCK_ROW_LOCK_COLUMN",
    "KEYED_LOCK_TABLE",
    "STATEMENTS",
    "KeyedLock",
    "acquire",
    "acquire_async",
    "acquire_in",
    "binding_lock_key",
    "bucket_for_key",
    "catalog_lock_key",
    "key_digest",
    "lock_statements",
    "voice_lock_key",
]

#: 桶数。`KEYED_LOCK_TABLE` 的行数上界就是它;扩大只改变竞争分布,不改变任何判定。
KEYED_LOCK_BUCKETS = 1024

#: 锁行表的唯一名字定义点:各命名空间的 DDL 与授权都从这里取,不各写一份。
KEYED_LOCK_TABLE = "keyed_locks"

#: 协调命名空间的锁行表名。backend 与执行网关是 import-linter 明令独立的两个部署,
#: 谁都不能导入对方,而它们必须锁**同一张物理表** —— 本包是两者唯一共享的底层,所以
#: 这个名字定义在这里;``coordination_keyed_lock_schema``(DDL 与授权面)从本模块再导出,
#: 全仓不出现第二份字面量。
COORDINATION_KEYED_LOCK_TABLE = "coordination_keyed_locks"

#: 键文本前缀。catalog 互斥的键文法是仓库级约定(``speechnexus:catalog:<段>…``),
#: 只在这里拼,免得两侧或后续迁移各自写一份前缀。
_KEY_PREFIX = "speechnexus:catalog"

#: 绑定禁用栅栏的键种类段。
_BINDING_KIND = "binding"

#: 声音发布/撤销/准入的键种类段。
_VOICE_KIND = "voice"

#: 键种类段的字符集。种类段与业务身份之间用 ``:`` 分隔,所以种类里再出现 ``:`` 就会把
#: 一条键折进另一族;这里按「一个短小写词」收窄,与 ``PublicationKind`` 的四个取值
#: (``profile`` / ``binding`` / ``voice`` / ``pronunciation``)相容,但不复制那份枚举 ——
#: 枚举属于领域,键文法属于本包,两者一旦各写一份就会有一份先过期。
_KEY_SEGMENT = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")

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
#: 不做任何改写(写错了要在发出前失败,而不是被悄悄折成合法名字)。表名与 schema 同一判据:
#: 两者都是调用方给的、要插值进语句的标识符,规则写两遍就是两份实现。
_IDENTIFIER = re.compile(r"\A[a-z][a-z0-9_]{0,62}\Z")


@dataclass(frozen=True, slots=True)
class KeyedLock:
    """一次键锁的结果:锁住的行、完整键的摘要、以及这一行是否由本事务创建.

    `digest` 只用于日志与证据:它是完整键(域 + 业务范围 + 幂等键摘要)的摘要,不参与行的
    身份,也不参与重放判定。`created` 说明这次调用是不是第一个用到这个桶的事务。
    """

    bucket_no: Decimal
    digest: str
    created: bool


def _target(schema: str | None, table: str = KEYED_LOCK_TABLE) -> str:
    """把命名空间与表名折成语句里的表引用;`schema=None` 按会话 `search_path` 解析.

    标识符不能被参数化,所以这里是唯一的插值点:名字先过字符集,再拼进已登记的模板。
    `public` / `information_schema` / `pg_` 前缀直接拒绝 —— 锁行不是公共对象,也不该落进
    系统命名空间。

    表名是参数而不是常量:同一个原语要服务多个命名空间,而各命名空间对这张表有自己的命名
    (workbench 的 `keyed_locks`、协调命名空间的 `coordination_keyed_locks`)。语句模板仍只有
    `STATEMENTS` 一份,派生方式不变。
    """

    if not _IDENTIFIER.match(table):
        raise ContractError("table must be a plain lowercase SQL identifier")
    if schema is None:
        return table
    if not _IDENTIFIER.match(schema) or schema in {"public", "information_schema"}:
        raise ContractError("schema must be a plain lowercase SQL identifier")
    if schema.startswith("pg_"):
        raise ContractError("schema must not name a system namespace")
    return f"{schema}.{table}"


def lock_statements(schema: str | None = None, *, table: str = KEYED_LOCK_TABLE) -> tuple[str, str]:
    """`(插入, 加锁)` 两条语句,表引用按 `schema` / `table` 解析。

    返回文本而不是执行:调用方可能要把语句喂给带 `row_factory` 的游标,或者在测试里逐字
    比对。两条语句的顺序不能交换 —— 先保证行存在,再锁它。
    """

    target = _target(schema, table)
    return (
        STATEMENTS["keyed_lock.insert"].format(target=target),
        STATEMENTS["keyed_lock.lock"].format(target=target),
    )


def key_digest(key: str) -> str:
    """完整键的摘要(`sha256:` + 十六进制),只作证据,不是行身份。"""

    text = require_text(key, field="key", max_bytes=1024)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def catalog_lock_key(kind: str, object_id: str) -> str:
    """目录身份互斥的完整键:``speechnexus:catalog:<kind>:<object_id>``,全仓唯一的拼法.

    一条目录事实(绑定、声音、发音、档案)在发布、退役、撤销、准入、探针授权、操作员认证里
    被**同一把**逻辑锁排序,所以键文法是**一种**,不是每种发布形态一种。``kind`` 因此是
    参数而不是本模块的几个常量:``publications_sql`` 的通用站点拿到的就是 ``PublicationKind``
    本身,它必须和绑定/声音专用站点算出**逐字相同**的键,否则同一个身份的发布与退役会各锁
    各的,而且不报错。

    键里为什么**没有** schema —— 与 ``binding_lock_key`` 同一条理由(那里写了全文):advisory
    lock 只能靠键文本区分实例,行锁的行身份是(``search_path`` 解析出的锁行表, 桶号),schema
    由表承载。把 ``current_schema()`` 留在键里只会多出一条会分叉的推导:两侧 ``search_path``
    首个条目不同而表解析到同一张时,同一个逻辑键会算出两个桶,互斥静默裂成两把。
    """

    if not isinstance(kind, str) or not _KEY_SEGMENT.match(kind):
        raise ContractError("kind must be one lowercase key segment")
    text = require_text(object_id, field="object_id", max_bytes=512)
    return f"{_KEY_PREFIX}:{kind}:{text}"


def binding_lock_key(binding_id: str) -> str:
    """绑定禁用栅栏那把互斥的完整键:``speechnexus:catalog:binding:<binding_id>``.

    为什么这个函数在本包:该键的两侧分别是 backend 与执行网关,它们是两个独立部署,
    谁都不能导入对方的模块,而「同一个绑定 → 同一行」只有在两侧**逐字相同**地拼这个文本
    时成立。定义点放在两者唯一共享的底层,任何一侧都不可能悄悄换一种拼法。

    拼法本身不再写在这里:它就是 ``catalog_lock_key(kind, object_id)``,绑定只是 ``kind``
    取 ``binding`` 的那一支。绑定/声音/通用发布站点因此共用一条键文法,专用函数只固定种类段。

    为什么键里不再有 schema:旧键把会话的 ``current_schema()`` 也拼进文本
    (``'speechnexus:catalog:' || current_schema() || ':binding:' || ?``),那是 advisory
    lock 唯一的命名空间手段 —— 哈希值没有表,只能靠键文本区分实例。行锁不需要它:行的
    身份是(`search_path` 解析出的锁行表, 桶号),schema 那一段由表本身承载,而两侧在同一
    部署里用同一个 ``search_path`` 取同一张表,所以「同键 → 同一行」由表保证。反过来,
    把 ``current_schema()`` 留在键里会多出一条**会分叉**的推导:两侧 search_path 首个条目
    不同(例如一侧 ``a, speech``、另一侧 ``speech``)而表都解析到 ``speech`` 时,桶号会算成
    两个 —— 互斥静默裂成两把,正是这次迁移要防的失效。
    """

    return catalog_lock_key(_BINDING_KIND, binding_id)


def voice_lock_key(voice_id: str) -> str:
    """声音身份互斥的完整键:``speechnexus:catalog:voice:<voice_id>``.

    声音族的全部参与者(发布、撤销、来源批准、TTS 准入、探针授权/批准)都在 ``backend``
    这一个部署里,但它与绑定族共用 ``coordination_keyed_locks`` 的同一张表:锁序要求
    「绑定行 → 声音行」,而两族共表是这条顺序能靠表本身成立的前提。所以拼法仍只走
    ``catalog_lock_key``,本函数只固定种类段。
    """

    return catalog_lock_key(_VOICE_KIND, voice_id)


def bucket_for_key(key: str, *, buckets: int = KEYED_LOCK_BUCKETS) -> Decimal:
    """完整键 → 桶号:`sha256(键)` 前 8 字节对 `buckets` 取模。

    不取 `hash()`、不用进程内随机盐、不读时钟:同一个键在任何进程里都必须落到同一行。
    """

    text = require_text(key, field="key", max_bytes=1024)
    if buckets < 1:
        raise ContractError("bucket count must be at least 1")
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return Decimal(int.from_bytes(digest[:8], "big") % buckets)


def _own_cursor(connection: object) -> tuple[Any, bool]:
    """`(要用的游标, 是否由这里关闭)`;连接才开新游标,自带游标的执行者就用自己的.

    生产路径传进来的是连接(开一条游标、用完关掉)或游标(`acquire_in` 已经换成它的连接)。
    单元用例的替身有些本身就既是连接又是游标 —— 只有 `execute`/`fetchall`,没有 `cursor()`;
    对它们来说「用它自己」就是同一笔事务里的同一份语句,所以这里不另造一层适配。
    """

    open_cursor = getattr(connection, "cursor", None)
    if open_cursor is None:
        return connection, False
    return open_cursor(), True


def _require_transaction(connection: object) -> None:
    """没有事务块就拿不住键锁:语句一结束锁就释放,等于什么都没锁.

    静默退化成「无锁」是最坏的结果,所以这里明确失败。判定与 `insert_if_absent` 共用
    `transaction.in_transaction`:它区分「`autocommit` 模式下没有事务」和「`autocommit` 模式
    下调用方显式开了一个事务块」—— 后者能开保存点,也能持锁到提交。
    """

    if not in_transaction(connection):
        raise ContractError("a keyed lock needs an open transaction, not a bare autocommit call")


def acquire(
    connection: Connection,
    *,
    key: str,
    schema: str | None = None,
    table: str = KEYED_LOCK_TABLE,
) -> KeyedLock:
    """在调用方的事务里锁住 `key` 对应的行,返回锁住的桶号.

    顺序固定:插入(已存在则只回滚到保存点)→ `SELECT … FOR UPDATE`。插入那一步只为了让行
    存在,行的内容与业务无关;唯一冲突在这里是**正常路径**(并发写者先到),由
    `insert_if_absent` 翻成 `False`,事务保持可用。

    事务边界由调用方拥有:本函数不 `commit` 也不 `rollback`。同一事务内重复取同一个键不
    自阻塞(第二次插入走冲突分支,行锁已在手)。

    `table` 默认是 workbench 的 `keyed_locks`;协调命名空间传 `coordination_keyed_locks`,
    语句本身不变 —— 派生表的仍是同一个 `lock_statements`,没有第二份实现。
    """

    _require_transaction(connection)
    insert_statement, lock_statement = lock_statements(schema, table=table)
    bucket = bucket_for_key(key)
    digest = key_digest(key)
    cursor, opened = _own_cursor(connection)
    try:
        created = insert_if_absent(cursor, insert_statement, (bucket,))
        cursor.execute(lock_statement, (bucket,))
        if len(cursor.fetchall()) != 1:
            raise ContractError(f"{table} row {bucket} disappeared between insert and lock")
    finally:
        if opened:
            cursor.close()
    return KeyedLock(bucket_no=bucket, digest=digest, created=created)


async def acquire_async(
    connection: AsyncConnection,
    *,
    key: str,
    schema: str | None = None,
    table: str = KEYED_LOCK_TABLE,
) -> KeyedLock:
    """`acquire` 的异步形态:同一组语句、同一个桶号、同一份碰撞契约.

    异步连接上的游标是 `await` 的,所以插入那一步走 `insert_if_absent_async`;两条语句与
    同步形态逐字相同(都由 `lock_statements` 生成),不存在第二份 SQL。
    """

    _require_transaction(connection)
    insert_statement, lock_statement = lock_statements(schema, table=table)
    bucket = bucket_for_key(key)
    digest = key_digest(key)
    cursor = connection.cursor()
    try:
        created = await insert_if_absent_async(cursor, insert_statement, (bucket,))
        await cursor.execute(lock_statement, (bucket,))
        if len(await cursor.fetchall()) != 1:
            raise ContractError(f"{table} row {bucket} disappeared between insert and lock")
    finally:
        await cursor.close()
    return KeyedLock(bucket_no=bucket, digest=digest, created=created)


def acquire_in(
    executor: Any,
    *,
    key: str,
    schema: str | None = None,
    table: str = KEYED_LOCK_TABLE,
) -> KeyedLock:
    """在 `executor` 所属的那笔事务里取键锁(游标或连接都可以).

    调用点手上常常只有游标(`with connection.cursor() as cursor:`),而 `acquire` 需要连接
    来读驱动的事务状态并开自己的游标。所以「哪个连接拥有这个执行者」只在这里判定一次:
    游标的 `connection` 是持有它的连接,而 psycopg 的连接对它自己返回自身。用 `getattr`
    而不是断言属性存在,是因为集成用例的替身只实现 `execute`/`cursor`。

    这不是第二种取锁做法:桶号、语句与提交边界都还是 `acquire` 的那一套。
    """

    owner = getattr(executor, "connection", None)
    return acquire(
        cast(Connection, owner if owner is not None else executor),
        key=key,
        schema=schema,
        table=table,
    )
