"""生命周期写入用的表名是字面量,必须出现在 schema 的 DDL 里.

这些语句原来用 `{TABLE}` 插值。改成字面量才让语句可静态登记(#1101 的 statement manifest),
代价是可能与 schema 常量漂移 —— 所以拿 DDL 作为独立事实源来核对,而不是再抄一遍常量名。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from speechnexus_backend.speech.infrastructure import (
    binding_lifecycle_postgres,
    public_job_admission,
    public_jobs_postgres,
    voice_publications_postgres,
)

_INFRA = Path(binding_lifecycle_postgres.__file__).parent
_MODULES = {
    "binding_lifecycle_postgres": Path(binding_lifecycle_postgres.__file__),
    "voice_publications_postgres": Path(voice_publications_postgres.__file__),
}

#: The readers that ask the catalogue tombstone/state tables by name. They used to
#: interpolate ``RETIREMENT_TABLE`` / ``STATE_TABLE``; a table name is an identifier, so no
#: dialect can bind it, and the interpolation was the only reason these statements could
#: not be read statically.
_CATALOGUE_READERS = {
    "public_job_admission": Path(public_job_admission.__file__),
    "public_jobs_postgres": Path(public_jobs_postgres.__file__),
}


def _schema_table_names() -> set[str]:
    """schema 模块导出的表名常量是独立事实源(``*_TABLE`` / ``TABLE``)。"""

    names: set[str] = set()
    for path in _INFRA.glob("*schema*.py"):
        module = importlib.import_module(f"speechnexus_backend.speech.infrastructure.{path.stem}")
        for attribute, value in vars(module).items():
            exported = attribute in {"TABLE", "JOBS", "SUBMISSIONS"} or attribute.endswith("_TABLE")
            if exported and isinstance(value, str):
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


def _referenced(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {
        name
        for name in re.findall(r"(?:INSERT INTO|DELETE FROM|UPDATE|FROM|JOIN) \"?(\w+)\"?", text)
        if name not in _KEYWORDS
    }


def test_the_schema_modules_name_tables() -> None:
    assert _SCHEMA_TABLES, "no schema table constant was found; the check would be vacuous"


@pytest.mark.parametrize("module", sorted(_MODULES))
def test_every_table_a_literalised_module_names_is_a_schema_constant(module: str) -> None:
    unknown = sorted(_referenced(_MODULES[module]) - _SCHEMA_TABLES)
    assert unknown == [], (
        f"{module} names tables no schema module exports: {unknown}. A misspelt "
        "literal survives the type checker and only fails at runtime, so the "
        "schema constants are the fact source"
    )


def test_the_startup_read_probes_name_only_schema_tables() -> None:
    """启动探针是一条静态语句:它列的每个关系都必须来自 schema 常量或 DDL。"""

    from speechnexus_backend import admission_watch_runtime, job_cancellation_runtime

    for statement in (
        admission_watch_runtime.READ_PROBE,
        job_cancellation_runtime.CANCELLATION_READ_PROBE,
    ):
        assert statement.endswith("WHERE 1=0")
        names = {
            name.strip()
            for name in statement.split(" FROM ", 1)[1].split(" WHERE ", 1)[0].split(",")
        }
        assert names <= _SCHEMA_TABLES, sorted(names - _SCHEMA_TABLES)


#: A table name in these modules' statements, when it belongs to the publication family.
_CATALOGUE_TABLE_NAME = re.compile(
    r"\b(?:FROM|JOIN) (speech_publication\w*|speech_catalog\w*)", re.IGNORECASE
)


def test_the_catalogue_readers_name_only_the_schema_tables() -> None:
    """The literalised table names are the schema constants, not a second naming.

    A table name cannot be a parameter, so the closed set of names has to appear in the
    statement text. This test is what keeps that from becoming a second definition: it
    reads the names from the schema modules, so renaming a table -- or misspelling one
    literal -- fails here. A wrong relation name is not caught by the type checker and
    only surfaces as "a required relation is missing" from the Job store, which is why the
    fact source has to be independent of the statements.
    """
    from speechnexus_backend.speech.infrastructure.catalog_revision_schema import (
        RETIREMENT_TABLE,
        STATE_TABLE,
    )
    from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
        HEAD_TABLE,
    )
    from speechnexus_backend.speech.infrastructure.publication_schema import (
        TABLE as PUBLICATION_TABLE,
    )

    known = {PUBLICATION_TABLE, HEAD_TABLE, RETIREMENT_TABLE, STATE_TABLE}
    texts = {name: path.read_text(encoding="utf-8") for name, path in _CATALOGUE_READERS.items()}
    for name, text in texts.items():
        named = set(_CATALOGUE_TABLE_NAME.findall(text))
        assert named, f"{name} names no catalogue table; the check would be vacuous"
        assert named <= known, f"{name} names tables no schema module owns: {sorted(named - known)}"
        assert RETIREMENT_TABLE in named, f"{name} does not read the tombstone table"
        # The interpolation that made the statement unstatable may not come back; the
        # portable-SQL guard also rejects it, so this is the site-level half of that.
        assert "{RETIREMENT_TABLE}" not in text, name
        assert "{STATE_TABLE}" not in text, name
    assert STATE_TABLE in _CATALOGUE_TABLE_NAME.findall(texts["public_jobs_postgres"]), (
        "the catalogue revision read no longer names the state table literally"
    )
