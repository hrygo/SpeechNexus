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

import ast
import re
from pathlib import Path
from typing import cast

#: guards job 在没有安装项目依赖的环境里跑 ``tests/ci``,所以这里不 import 任何项目包 ——
#: 两份文本都从源文件读。导入失败会把这条判据变成 ImportError,而 ImportError 看起来
#: 和「判据不存在」一样红,却什么都不检查。
ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "backend/src/speechnexus_backend/speech/infrastructure/canonical_event_schema.py"
CLAIMS = ROOT / "packages/portable-sql/src/speechnexus_portable_sql/claims.py"


def _probe_projection(node: ast.expr) -> tuple[str, int] | None:
    """识别 ``tuple(x for x, _ in SRC)`` 这种纯投影。

    只支持这一种形状:遇到别的推导式就返回 ``None``,由 ``ast.literal_eval`` 报错,而不是
    猜一个语义出来——猜错会让判据静默比对错东西。
    """

    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    if node.func.id != "tuple" or len(node.args) != 1 or node.keywords:
        return None
    generator = node.args[0]
    if not isinstance(generator, ast.GeneratorExp) or len(generator.generators) != 1:
        return None
    comprehension = generator.generators[0]
    if not isinstance(comprehension.iter, ast.Name) or not isinstance(
        comprehension.target, ast.Tuple
    ):
        return None
    names = comprehension.target.elts
    if not all(isinstance(item, ast.Name) for item in names):
        return None
    if not isinstance(generator.elt, ast.Name):
        return None
    for index, item in enumerate(names):
        assert isinstance(item, ast.Name)
        if item.id == generator.elt.id:
            return comprehension.iter.id, index
    return None


def _module_constants(path: Path, names: set[str]) -> dict[str, object]:
    """从模块源码里取字面量常量,不执行模块。

    允许 ``A = B`` 这种别名:同一模块里的名字按定义顺序解析,``B`` 本身也是字面量才算数。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                bound[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = node.value

    resolved: dict[str, object] = {}

    def resolve(name: str, seen: frozenset[str] = frozenset()) -> object:
        if name in seen:
            raise AssertionError(f"{path.name}: {name} resolves through itself")
        if name not in bound:
            raise AssertionError(f"{path.name} no longer defines {name}")
        node = bound[name]
        if isinstance(node, ast.Name):
            return resolve(node.id, seen | {name})
        projected = _probe_projection(node)
        if projected is not None:
            source, index = projected
            return tuple(
                pair[index]
                for pair in cast("tuple[tuple[str, ...], ...]", resolve(source, seen | {name}))
            )
        return ast.literal_eval(node)

    for name in names:
        resolved[name] = resolve(name)
    return resolved


_SCHEMA = _module_constants(
    SCHEMA,
    {
        "JOB_EVENT_INSERT_COLUMNS",
        "JOB_EVENT_PRIMARY_KEY",
        "JOB_EVENT_SELECT_COLUMNS",
        "JOB_EVENT_TABLE",
    },
)
JOB_EVENT_INSERT_COLUMNS: tuple[str, ...] = cast(
    "tuple[str, ...]", _SCHEMA["JOB_EVENT_INSERT_COLUMNS"]
)
JOB_EVENT_PRIMARY_KEY: tuple[str, ...] = cast("tuple[str, ...]", _SCHEMA["JOB_EVENT_PRIMARY_KEY"])
JOB_EVENT_SELECT_COLUMNS: tuple[str, ...] = cast(
    "tuple[str, ...]", _SCHEMA["JOB_EVENT_SELECT_COLUMNS"]
)
JOB_EVENT_TABLE: str = cast("str", _SCHEMA["JOB_EVENT_TABLE"])


def _statement(name: str) -> str:
    tree = ast.parse(CLAIMS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == name
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                return value.value
    raise AssertionError(f"claims.STATEMENTS has no {name!r}")


class _Statements:
    def __getitem__(self, name: str) -> str:
        return _statement(name)


STATEMENTS: dict[str, str] = cast("dict[str, str]", _Statements())

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
