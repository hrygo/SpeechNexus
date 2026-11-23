"""禁用目标模块:每条语句都是字面量,投影列序对着 `_FIELDS` 核对."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import binding_disable_targets_postgres as module

_FIELDS = module._FIELDS
_READS = (
    "_PAGE_TARGETS",
    "_PAGE_TARGETS_SCOPED",
    "_READ_TARGET",
    "_LIST_TARGETS",
    "_LIST_TARGETS_SCOPED",
)
_WRITES = ("_INSERT_TARGET", "_ABORT_INTENT")


def _projected(statement: str) -> list[str]:
    body = statement.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    columns: list[str] = []
    for raw in body.split(","):
        column = raw.strip()
        if column == "?":
            continue
        if column.startswith("? AS "):
            column = column.split(" AS ", 1)[1].strip()
        columns.append(column.split(".")[-1])
    return columns


def test_every_read_projects_the_whole_target_in_field_order() -> None:
    for name in _READS:
        assert _projected(getattr(module, name)) == list(_FIELDS), name


def test_no_statement_is_interpolated_and_all_use_the_canonical_marker() -> None:
    for name in (*_READS, *_WRITES):
        statement = getattr(module, name)
        assert "{" not in statement, name
        assert "%s" not in statement, name
    for name in _WRITES:
        assert "ON CONFLICT" not in getattr(module, name).upper(), name


def test_the_insert_binds_the_event_plus_every_field() -> None:
    statement = module._INSERT_TARGET
    body = statement.split("(", 1)[1].split(")", 1)[0]
    assert [column.strip() for column in body.split(",")] == ["event_id", *_FIELDS]
    assert statement.count("?") == len(_FIELDS) + 1
