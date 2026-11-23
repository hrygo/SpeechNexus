"""目录读:表名写死,隐藏已退休修订的谓词不能被简化掉."""

from __future__ import annotations

import inspect

from speechnexus_backend.speech.infrastructure import model_catalog_sql as module


def _source() -> str:
    return inspect.getsource(module)


def test_the_catalog_reader_names_its_tables_literally() -> None:
    source = _source()
    assert "speech_publication_retirements" in source
    assert "speech_catalog_state" in source
    assert "RETIREMENT_TABLE" not in source
    assert "STATE_TABLE" not in source


def test_the_retired_revision_predicate_survives() -> None:
    source = _source()
    # 已退休的修订必须继续被排除:kind/object_id/revision/revision_id 四个键都要对上。
    flat = " ".join(source.split())
    assert "retired.kind = 'binding'" in flat
    assert "retired.object_id = p.object_id" in flat
    assert "retired.revision = p.revision " in flat
    assert "retired.revision_id = p.revision_id" in flat
    assert "NOT EXISTS (" in " ".join(source.split())


def test_the_state_read_stays_a_single_row_read() -> None:
    source = _source()
    assert "WHERE singleton IS TRUE" in source
    # 计数器是单行状态,不是行数:读语句里不能出现聚合。
    statement = source.split("cursor.execute(", 2)[-1]
    assert "COUNT(" not in statement.upper()
