"""参数标记的唯一翻译定义:契约用 `?`,驱动用各自的风格.

为什么单独一层:psycopg 与 pymysql 都需要同一条规则(含「字面量 `%` 先转义」),把规则写两遍
就是两份实现的开始。驱动模块只负责把连接的游标接到这里。

`?` 在 PostgreSQL 里同时是 JSONB 的存在运算符(`?`、`?|`、`?&`)。翻译层因此必须先做词法扫描:
只翻**占位符位置**的 `?`,字符串字面量、标识符、注释里的问号一律不动;遇到运算符形态直接拒绝,
而不是把一条合法语句悄悄改坏。运算符本来就在受限方言的禁止清单里(见 `subset.find_forbidden`),
所以这里拒绝是收紧,不是新增限制。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

__all__ = ["PYFORMAT", "AmbiguousMarkerError", "placeholder_positions", "qmark_to_pyformat"]

PYFORMAT = re.compile(r"%\([a-z_][a-z0-9_]*\)s", re.IGNORECASE)


class AmbiguousMarkerError(ValueError):
    """语句里的 `?` 是运算符而不是占位符:拒绝翻译,不猜测。"""


_VALUE_END = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_'\")]")
_VALUE_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_'\"$")
#: 这些词之后出现的 `?` 是占位符,不是 JSONB 运算符。
_KEYWORDS = frozenset(
    {
        "SELECT",
        "WHERE",
        "AND",
        "OR",
        "NOT",
        "VALUES",
        "SET",
        "BY",
        "ON",
        "USING",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "IS",
        "IN",
        "LIKE",
        "RETURN",
        "LIMIT",
        "OFFSET",
        "BETWEEN",
        "COALESCE",
        "AS",
    }
)


def _is_operator(sql: str, index: int) -> bool:
    """`?` 处在运算符位置:左边是一个值结束,右边是一个值开始(JSONB 存在运算)."""

    left = index - 1
    while left >= 0 and sql[left] in " \t\r\n":
        left -= 1
    right = index + 1
    while right < len(sql) and sql[right] in " \t\r\n":
        right += 1
    if left < 0 or right >= len(sql):
        return False
    if sql[left] not in _VALUE_END:
        return False
    word_end = left + 1
    word_start = left
    while word_start >= 0 and (sql[word_start].isalnum() or sql[word_start] == "_"):
        word_start -= 1
    if sql[word_start + 1 : word_end].upper() in _KEYWORDS:
        return False
    return sql[right] in _VALUE_START or sql.startswith("%s", right)


def placeholder_positions(sql: str, *, legacy: bool = False) -> list[int]:
    """返回占位符 `?` 的下标.

    运算符形态在 qmark 语句里直接拒绝(受限方言不允许);在旧 `%s` 语句里则跳过,因为那是
    已有的 JSONB 用法,由静态 guard 的基线负责收敛,不能靠运行时改写。
    """

    positions: list[int] = []
    index = 0
    size = len(sql)
    while index < size:
        char = sql[index]
        if char == "'":
            index += 1
            while index < size:
                if sql[index] == "'":
                    if index + 1 < size and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == '"':
            index += 1
            while index < size and sql[index] != '"':
                index += 1
            index += 1
            continue
        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = size if newline < 0 else newline + 1
            continue
        if char == "/" and sql.startswith("/*", index):
            closing = sql.find("*/", index + 2)
            index = size if closing < 0 else closing + 2
            continue
        if char == "?":
            following = sql[index + 1 : index + 2]
            if following in ("|", "&"):
                if legacy:
                    index += 1
                    continue
                raise AmbiguousMarkerError("jsonb `?|` / `?&` operator is not a placeholder")
            probe = index + 1
            while probe < size and sql[probe] in " \t\r\n":
                probe += 1
            if _is_operator(sql, index):
                if legacy:
                    index += 1
                    continue
                raise AmbiguousMarkerError("jsonb `?` operator is not a placeholder")
            positions.append(index)
        index += 1
    return positions


def qmark_to_pyformat(sql: str, params: Sequence[Any] | None) -> str:
    """把 qmark 语句翻成 `%s`;已有 `%` 按字面量转义,旧语句原样放过。"""

    if params is None or "?" not in sql:
        return sql
    legacy = "%s" in sql or bool(PYFORMAT.search(sql))
    positions = placeholder_positions(sql, legacy=legacy)
    if legacy:
        if positions:
            raise AmbiguousMarkerError("statement mixes `?` and `%s` placeholders")
        return sql
    pieces: list[str] = []
    cursor = 0
    for position in positions:
        pieces.append(sql[cursor:position].replace("%", "%%"))
        pieces.append("%s")
        cursor = position + 1
    pieces.append(sql[cursor:].replace("%", "%%"))
    return "".join(pieces)
