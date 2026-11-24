"""Fake-cursor answers for the coordination keyed lock's three statements.

A unit fake models the SQL it answers. The keyed lock emits a savepoint pair (its bucket
row is created lazily), that bucket ``INSERT``, and ``SELECT … FOR UPDATE``; every fake
driving a code path that takes the binding mutex has to answer all three. Keeping the
answer here means the statement shape is declared once instead of drifting across a dozen
independent fakes -- and a fake can state, in one line, "the lock's statements are
modelled" without restating the primitive's SQL.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from speechnexus_portable_sql.keyed_lock import COORDINATION_KEYED_LOCK_TABLE

_INSERT = f"INSERT INTO {COORDINATION_KEYED_LOCK_TABLE}"
_LOCK = f"SELECT bucket_no FROM {COORDINATION_KEYED_LOCK_TABLE}"
_SAVEPOINT_PREFIXES = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")


def answer_keyed_lock(query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]] | None:
    """替身该为这条语句摆出的行;``None`` 表示「这不是键锁的语句」。

    锁语句必须回一行:原语会核对「行在插入与加锁之间消失」,回空会让替身把一条真实的
    契约违反伪装成通过。
    """

    if query.startswith(_SAVEPOINT_PREFIXES) or query.startswith(_INSERT):
        return []
    if query.startswith(_LOCK):
        return [{"bucket_no": parameters[0]}]
    return None


def is_keyed_lock_insert(query: str) -> bool:
    """这条 INSERT 是键锁懒建桶行,不是业务写入."""

    return query.startswith(_INSERT)


def business_inserts(calls: Iterable[tuple[str, tuple[object, ...]]]) -> list[str]:
    """写业务行的 INSERT;键锁懒建的那一行不算业务写入.

    键锁的桶行是锁本身(行在第一次被用到时才插入),既不是 Job 也不是重放证据。把它算进
    「这笔事务没有写任何行」会让断言变成一句空话;把它排除,断言仍然要求业务行零写入。
    """

    return [
        query
        for query, _ in calls
        if query.startswith("INSERT") and not is_keyed_lock_insert(query)
    ]
