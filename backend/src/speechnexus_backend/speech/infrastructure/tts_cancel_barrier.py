"""TTS 取消/准入屏障:在 ``coordination_keyed_locks`` 上按操作身份取行锁.

两侧要串行化的是同一个 ``operation_id`` 的**准入**与**取消**,而它们插入的是两张不同的表:
准入写 ``speech_tts_settlement``,取消写 ``speech_tts_cancellation``。所以这里没有可以共用
的**天然行**——取消行在「还没有人取消」时根本不存在,而 ``SELECT … FOR UPDATE`` 在行不存在
时两引擎都不锁任何东西(PG 返回空集,InnoDB 在 ``READ COMMITTED`` 下不取间隙锁)。换出来的
不是「弱一点的锁」,是**静默失效**:``due()`` 只从结算行读 ``abort_reason``,从不回读取消表,
原因没在插入结算根时快照进去,后面没有任何补救路径。

**这里是唯一的取锁点。** 键文本只在 ``tts_cancel_lock_key`` 拼一次,两侧(准入的
``tts_settlement_sql`` 与取消的 ``tts_settlement_postgres``)都调 ``lock_tts_cancel``;语句
只由 ``speechnexus_portable_sql.keyed_lock`` 生成,本模块不写第二份 SQL,也不写第二份键前缀。

锁序。取消侧这笔事务**第一把**就是这把行锁,一直持到提交。准入侧它在已经排好序的准入事务
内部取得,排在 binding 键锁与容量/租约行之后、结算根插入之前:

    coordination_keyed_locks(binding 键) -> 准入容量/租约行 -> 键锁(TTS 取消键)
      -> speech_tts_settlement 插入

取消侧不持任何准入侧会等的对象(它插取消行、回填未结算根),所以这个次序不产生新的等待环;
它只让「读取消原因 → 把原因快照进结算根」与「插取消行 → 回填未结算根」互相等待——正是旧
advisory lock 达到的效果。

事务边界由调用方拥有:本模块不 ``commit``、不 ``rollback``、不自己开事务。两处调用方本来
就在事务块里(准入与结算 worker 都用 ``with pool.connection()``,psycopg 在退出时提交),
所以这里不额外包一层;没有事务时 ``acquire`` 直接 ``ContractError``——静默退化成「无锁」是
最坏的结果,不如在第一条语句上失败。
"""

from __future__ import annotations

from typing import Any

from speechnexus_portable_sql.contract import ContractError
from speechnexus_portable_sql.keyed_lock import (
    COORDINATION_KEYED_LOCK_TABLE,
    acquire_in,
)
from speechnexus_portable_sql.types import require_text

__all__ = ["lock_tts_cancel", "tts_cancel_lock_key"]

#: 键文本的域段。与旧 advisory lock 的 ``'speechnexus:tts-cancel:'`` 逐字相同。
_KEY_PREFIX = "speechnexus:tts-cancel"

#: 读会话解析出的 schema。键文本里有它,所以它必须来自这一笔事务真正在用的那个
#: ``search_path``,不能由调用方另给一份——两份值在 ``search_path`` 首个条目不同时会分叉。
_SCHEMA_STATEMENT = "SELECT current_schema() AS schema"

#: ``current_schema()`` 的结果上限:PG 标识符是 63 字节。
_MAX_SCHEMA_BYTES = 63


def tts_cancel_lock_key(schema: str, operation_id: str) -> str:
    """``speechnexus:tts-cancel:<schema>:<operation_id>``:键文本的唯一拼写点.

    为什么保留 ``schema`` 段。行锁的身份是(锁行表, 桶号),schema 那一段本来由表承载,所以
    这一段在键锁里不是必需的。保留它的理由是**迁移窗口**:旧键文本是
    ``'speechnexus:tts-cancel:' || current_schema() || ':' || ?``,两侧凡是按旧文本拼键的
    参与者都必须算出同一个桶号,删掉一段就会让两种拼法各算一个桶。真正跨机制的那一半
    (advisory lock 与行锁)本来就不能互等,所以这次把两侧放进同一个提交——见 ``lock_tts_cancel``。
    """

    checked_schema = require_text(schema, field="schema", max_bytes=_MAX_SCHEMA_BYTES)
    checked_operation = require_text(operation_id, field="operation_id", max_bytes=512)
    return f"{_KEY_PREFIX}:{checked_schema}:{checked_operation}"


def _current_schema(executor: Any) -> str:
    """在调用方的会话里读 ``current_schema()``,只读一次.

    ``executor`` 可能是连接(开一条游标、用完关掉),也可能是调用方手上的游标,包括
    ``_DeadlineCursor`` 那种包装过的游标——它们自带 ``execute``/``fetchall``,就用它自己,
    与 ``keyed_lock.acquire_in`` 对执行者的判定一致。这一条是 ``SELECT``,不是锁,所以不会
    破坏「键锁是这笔事务的第一把锁」。
    """

    open_cursor = getattr(executor, "cursor", None)
    cursor = executor if open_cursor is None else open_cursor()
    try:
        cursor.execute(_SCHEMA_STATEMENT)
        rows = cursor.fetchall()
    finally:
        if open_cursor is not None:
            cursor.close()
    if len(rows) != 1:
        raise ContractError("current_schema() must return exactly one row")
    return require_text(rows[0]["schema"], field="schema", max_bytes=_MAX_SCHEMA_BYTES)


def lock_tts_cancel(executor: Any, operation_id: str) -> None:
    """在 ``executor`` 所属的那笔事务里锁住这个操作的取消/准入屏障.

    同一事务里重复取同一个键不自阻塞(第二次插入走唯一冲突分支,行锁已在手)。
    """

    acquire_in(
        executor,
        key=tts_cancel_lock_key(_current_schema(executor), operation_id),
        table=COORDINATION_KEYED_LOCK_TABLE,
    )
