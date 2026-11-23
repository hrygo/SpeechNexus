"""审计事件读:`statement()` 只能产出四条静态语句之一."""

from __future__ import annotations

from speechnexus_backend.speech.domain.audit_events import AuditEventPosition, AuditEventQuery
from speechnexus_backend.speech.infrastructure import audit_events_postgres as module

_PROJECTION = tuple(column.strip() for column in module._COLUMNS.split(","))
_STATEMENTS = (
    module._PAGE_FIRST,
    module._PAGE_AFTER_ORDER,
    module._PAGE_ACTION_ORDER,
    module._PAGE_AFTER_ACTION_ORDER,
)


def _query(**overrides: object) -> AuditEventQuery:
    fields: dict[str, object] = {"limit": 10}
    fields.update(overrides)
    return AuditEventQuery(**fields)  # type: ignore[arg-type]


def test_every_shape_projects_the_whole_row_and_ends_with_one_ordering() -> None:
    for statement in _STATEMENTS:
        body = statement.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
        assert tuple(column.strip() for column in body.split(",")) == _PROJECTION
        assert statement.count("ORDER BY occurred_at_us DESC, event_id DESC") == 1


def test_the_four_query_shapes_map_to_those_four_statements() -> None:
    assert module.statement(_query())[0] == module._PAGE_FIRST
    assert module.statement(_query(action="x"))[0] == module._PAGE_ACTION_ORDER
    after = module.statement(_query(after=_cursor()))[0]
    assert after == module._PAGE_AFTER_ORDER
    both = module.statement(_query(after=_cursor(), action="x"))[0]
    assert both == module._PAGE_AFTER_ACTION_ORDER
    assert both.endswith("ORDER BY occurred_at_us DESC, event_id DESC")


def test_the_reader_never_builds_a_statement_at_call_time() -> None:
    for statement in _STATEMENTS:
        assert statement.startswith("SELECT ")
        assert "{" not in statement and "%s" not in statement
        assert statement.count("speech_publication_lifecycle_events") == 1


def _cursor() -> AuditEventPosition:
    return AuditEventPosition(occurred_at_us=1, event_id="evt-1")
