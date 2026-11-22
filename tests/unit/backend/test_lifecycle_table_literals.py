"""生命周期写入用的表名是字面量,必须出现在 schema 的 DDL 里.

这些语句原来用 `{TABLE}` 插值。改成字面量才让语句可静态登记(#1101 的 statement manifest),
代价是可能与 schema 常量漂移 —— 所以拿 DDL 作为独立事实源来核对,而不是再抄一遍常量名。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from speechnexus_backend.speech.infrastructure import binding_lifecycle_postgres

_INFRA = Path(binding_lifecycle_postgres.__file__).parent
_MODULE = Path(binding_lifecycle_postgres.__file__).read_text(encoding="utf-8")


def _schema_table_names() -> set[str]:
    """schema 模块导出的表名常量是独立事实源(``*_TABLE`` / ``TABLE``)。"""

    names: set[str] = set()
    for path in _INFRA.glob("*schema*.py"):
        module = importlib.import_module(f"speechnexus_backend.speech.infrastructure.{path.stem}")
        for attribute, value in vars(module).items():
            if (attribute == "TABLE" or attribute.endswith("_TABLE")) and isinstance(value, str):
                names.add(value)
    return names


def _ddl_table_names() -> set[str]:
    """DDL 模板里以字面量出现的表名(这些表没有导出常量)。"""

    names: set[str] = set()
    for path in _INFRA.glob("*schema*.py"):
        names |= set(
            re.findall(
                r"CREATE TABLE (?:IF NOT EXISTS )?\"?([a-z_]\w*)\"?",
                path.read_text(encoding="utf-8"),
            )
        )
    return names


_SCHEMA_TABLES = _schema_table_names() | _ddl_table_names()
_KEYWORDS = {"SET"}
_REFERENCED = {
    name
    for name in re.findall(r"(?:INSERT INTO|DELETE FROM|UPDATE|FROM|JOIN) \"?(\w+)\"?", _MODULE)
    if name not in _KEYWORDS
}


def test_the_schema_modules_name_tables() -> None:
    assert _SCHEMA_TABLES, "no schema table constant was found; the check would be vacuous"


def test_every_table_the_lifecycle_module_names_is_a_schema_constant() -> None:
    unknown = sorted(_REFERENCED - _SCHEMA_TABLES)
    assert unknown == [], (
        "these statements name tables no schema module exports: "
        f"{unknown}. A misspelt literal survives the type checker and only fails at "
        "runtime, so the schema constants are the fact source"
    )
