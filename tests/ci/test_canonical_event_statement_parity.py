"""canonical 事件语句与列声明的唯一性:两份文本必须描述同一张表、同一组列。

``canonical_event_schema`` 声明 ``job_events`` 的物理形状(建表、主键、外键、授权面),而运行期
执行的却是 ``speechnexus_portable_sql.claims`` 里那条 ``job_event.insert``。两份文本各自可读、
各自正确,合起来却可能描述两张表:列改了一处、另一处没改,写入以 ``42703``/``42501`` 失败,
而两个引擎都只报表名不点名缺哪列(``coordination_jobs`` 的 INSERT 列曾因此漂成五份)。

这里不比「两个常量相等」——它们不在同一个包里,也不该互相导入 —— 而是**逐字解析语句文本**,
把表名、列清单、列顺序与声明比对。解析器本身也要能被证伪:``_drift`` 对每条注入都返回差异,
否则这条判据在语句被改写时会静默通过。
"""

from __future__ import annotations

import re

from speechnexus_backend.speech.infrastructure.canonical_event_schema import (
    JOB_EVENT_INSERT_COLUMNS,
    JOB_EVENT_PRIMARY_KEY,
    JOB_EVENT_SELECT_COLUMNS,
    JOB_EVENT_TABLE,
)
from speechnexus_portable_sql.claims import STATEMENTS

INSERT = re.compile(r"^INSERT INTO (\w+) \(([^)]*)\) VALUES", re.IGNORECASE)
READ = re.compile(r"^SELECT (.+?) FROM (\w+) WHERE", re.IGNORECASE)


def _columns(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(","))


def _insert_shape(statement: str) -> tuple[str, tuple[str, ...]]:
    match = INSERT.match(statement.strip())
    assert match is not None, f"job_event.insert is not an INSERT: {statement!r}"
    return match.group(1), _columns(match.group(2))


def _read_shape(statement: str) -> tuple[str, tuple[str, ...]]:
    match = READ.match(statement.strip())
    assert match is not None, f"job_event.read is not a SELECT: {statement!r}"
    return match.group(2), _columns(match.group(1))


def _drift(statement: str) -> list[str]:
    """逐条比对语句形状与声明,返回人能读的差异(空表示一致)。"""

    problems: list[str] = []
    table, columns = _insert_shape(statement)
    if table != JOB_EVENT_TABLE:
        problems.append(f"insert targets {table!r}, the declaration is {JOB_EVENT_TABLE!r}")
    if columns != JOB_EVENT_INSERT_COLUMNS:
        problems.append(f"insert columns {columns} != declared {JOB_EVENT_INSERT_COLUMNS}")
    read_table, read_columns = _read_shape(STATEMENTS["job_event.read"])
    if read_table != JOB_EVENT_TABLE:
        problems.append(f"read targets {read_table!r}, the declaration is {JOB_EVENT_TABLE!r}")
    if read_columns != JOB_EVENT_SELECT_COLUMNS:
        problems.append(f"read projection {read_columns} != declared {JOB_EVENT_SELECT_COLUMNS}")
    # 序号是本表主键的一部分:insert 的列序不能把 event_seq 挪出主键声明之外。
    if not set(JOB_EVENT_PRIMARY_KEY) <= set(columns):
        problems.append(f"insert drops a primary key column of {JOB_EVENT_PRIMARY_KEY}")
    return problems


def test_the_executed_insert_describes_the_declared_table() -> None:
    assert _drift(STATEMENTS["job_event.insert"]) == []


def test_a_renamed_column_or_table_is_reported() -> None:
    """注入:列改名、表改名、少一列都必须被点名,否则这条判据什么都没检查。"""

    renamed = STATEMENTS["job_event.insert"].replace("event_digest", "digest")
    assert any("insert columns" in problem for problem in _drift(renamed))

    retargeted = STATEMENTS["job_event.insert"].replace(JOB_EVENT_TABLE, "job_event_shadow")
    assert any("insert targets" in problem for problem in _drift(retargeted))

    dropped = STATEMENTS["job_event.insert"].replace("payload_ref", "")
    assert any("insert columns" in problem for problem in _drift(dropped))
