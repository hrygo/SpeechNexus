"""零行/存在性探针:不得再用 `LIMIT`,判定仍必须能翻面.

方言没有 `LIMIT`,所以资产表与语音视图的探针改成了标准 `EXISTS`。这些用例盯两件事:
语句里不再出现 `LIMIT`,以及「有没有行」这个判定在真/假两侧都能翻过来。
"""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.assets_postgres import _any_asset
from speechnexus_backend.speech.infrastructure.voice_catalogue_postgres import (
    _require_publications,
)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.statements: list[str] = []

    def execute(self, statement: str, parameters: object = None) -> None:
        self.statements.append(statement)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def rows(self, statement: str, parameters: object = None) -> list[dict[str, Any]]:
        self.statements.append(statement)
        return self._rows


@pytest.mark.parametrize(
    ("rows", "expected"),
    [([{"present": True}], True), ([{"present": False}], False), ([], False)],
)
def test_any_asset_reads_the_existence_flag(rows: list[dict[str, Any]], expected: bool) -> None:
    cursor = _Cursor(rows)
    assert _any_asset(cursor) is expected  # type: ignore[arg-type]
    statement = cursor.statements[0]
    assert "EXISTS (SELECT 1 FROM speech_assets)" in statement
    # 注入 `LIMIT 1` 会让这条失败:探针已经不该再带 LIMIT。
    assert "LIMIT" not in statement


def test_publications_are_required_from_the_existence_flag() -> None:
    cursor = _Cursor([{"present": True}])
    _require_publications(cursor)  # type: ignore[arg-type]
    statement = cursor.statements[0]
    assert "EXISTS (SELECT 1 FROM" in statement
    assert "LIMIT" not in statement


@pytest.mark.parametrize("rows", [[{"present": False}], [], [{"present": True}, {"present": True}]])
def test_a_missing_or_ambiguous_presence_refuses_the_read(rows: list[dict[str, Any]]) -> None:
    """没有可读视图,或判定不是恰好一行,都必须拒绝,而不是当成「有」。"""

    cursor = _Cursor(rows)
    with pytest.raises(PublicationUnavailableError):
        _require_publications(cursor)  # type: ignore[arg-type]
