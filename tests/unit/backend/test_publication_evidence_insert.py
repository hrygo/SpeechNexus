"""发布证据的插入走 `insert_if_absent`,不依赖 `ON CONFLICT`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    insert_evidence,
)


class DuplicateKeyError(Exception):
    sqlstate = "23505"


class Cursor:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))
        if self.duplicate and query.lstrip().startswith("INSERT INTO speech_binding"):
            raise DuplicateKeyError(query)


def evidence() -> BindingPublicationEvidence:
    return BindingPublicationEvidence(
        "binding-tts",
        1,
        "binding-rev-1",
        "manifest-1",
        "report-1",
        "operator_cli",
        1_700_000_000_000_000,
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )


def test_insert_runs_inside_a_savepoint_and_never_uses_on_conflict() -> None:
    cursor = Cursor()

    assert insert_evidence(cast(Any, cursor), evidence()) is True
    savepoint, _ = cursor.calls[0]
    insert_query, insert_parameters = cursor.calls[1]
    assert savepoint.startswith("SAVEPOINT")
    assert "ON CONFLICT" not in insert_query
    assert insert_query.count("INSERT INTO speech_binding_publication_evidence") == 1
    assert insert_parameters[2] == "1"


def test_a_duplicate_key_is_reported_as_no_insert_without_raising() -> None:
    cursor = Cursor(duplicate=True)

    assert insert_evidence(cast(Any, cursor), evidence()) is False
    # 回滚到保存点之后事务仍然可用 —— 这正是 `ON CONFLICT` 之前免费提供、现在必须显式保住的性质。
    assert any(query.startswith("ROLLBACK TO SAVEPOINT") for query, _ in cursor.calls)


@pytest.mark.parametrize("sqlstate", ["23514"])
def test_the_duplicate_predicate_is_the_unique_violation_only(sqlstate: str) -> None:
    """只有唯一冲突(23505)算「已存在」;检查约束失败必须原样抛出。"""

    class OtherError(Exception):
        pass

    OtherError.sqlstate = sqlstate  # type: ignore[attr-defined]
    cursor = Cursor()

    def raise_other(query: str, parameters: tuple[Any, ...] = ()) -> None:
        cursor.calls.append((query, parameters))
        if query.lstrip().startswith("INSERT INTO speech_binding"):
            raise OtherError(sqlstate)

    cursor.execute = raise_other  # type: ignore[method-assign]
    with pytest.raises(OtherError):
        insert_evidence(cast(Any, cursor), evidence())
