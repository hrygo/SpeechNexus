"""The shared JSON formatter keeps ``extra`` fields and never fights a host."""

from __future__ import annotations

import json
import logging

from speechnexus_backend.json_logging import (
    STANDARD_RECORD_ATTRIBUTES,
    JsonLogFormatter,
    use_json_logging,
)


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="speechnexus_backend.synthetic",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="admission readiness changed",
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_every_extra_field_reaches_the_document() -> None:
    document = json.loads(
        JsonLogFormatter().format(
            _record(
                event="admission_watch_changed",
                workspace_id="workspace-test",
                **{
                    "from": {"status": "admitting", "cause": None},
                    "to": {"status": "degraded", "cause": "capacity_exhausted"},
                },
            )
        )
    )
    assert document["message"] == "admission readiness changed"
    assert document["level"] == "INFO"
    assert document["logger"] == "speechnexus_backend.synthetic"
    assert isinstance(document["time"], str) and document["time"]
    assert document["event"] == "admission_watch_changed"
    assert document["workspace_id"] == "workspace-test"
    assert document["from"] == {"status": "admitting", "cause": None}
    assert document["to"] == {"status": "degraded", "cause": "capacity_exhausted"}
    # Standard attributes stay as the record's own keys, never duplicated as extras.
    assert STANDARD_RECORD_ATTRIBUTES.isdisjoint({"event", "workspace_id", "from", "to"})


def test_a_value_json_cannot_encode_is_rendered_as_text() -> None:
    document = json.loads(JsonLogFormatter().format(_record(when=object())))
    assert isinstance(document["when"], str) and document["when"]


def test_configuration_is_installed_once_and_only_when_absent() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        root.handlers.clear()
        use_json_logging()
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler.formatter, JsonLogFormatter)
        assert root.level == logging.INFO
        # A second call is a no-op: the entrypoint never stacks duplicate handlers.
        use_json_logging()
        assert len(root.handlers) == 1
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
