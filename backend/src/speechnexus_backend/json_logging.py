"""One JSON object per log record, for console entrypoints that need machine output.

The backend's structured lines are written with ``extra={...}`` on a plain
``logging`` record, so the fields exist only as record attributes; a text
formatter drops them and ``logging``'s default configuration drops the record
entirely (root defaults to ``WARNING`` with no handler). An entrypoint whose
product *is* its log output -- the admission watch writes one line per verdict
change and nothing else -- would therefore appear to work in tests and print
nothing in a container.

``use_json_logging`` is the small, shared configuration those entrypoints call:
JSON on stderr, ``INFO`` included, no per-module formatter copies. It is
deliberately additive: ``basicConfig`` is a no-op once the root logger has a
handler, so a host process, a test harness or an earlier entrypoint keeps its own
configuration and this function never fights it.

Which keys become top-level JSON members is fixed by the record, not by the
caller: the standard attributes keep their conventional names (``level``,
``logger``, ``message``, ``time``) and every ``extra`` key is copied verbatim, so
``event``, ``workspace_id``, ``from`` and ``to`` stay queryable.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

STANDARD_RECORD_ATTRIBUTES: frozenset[str] = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
)
"""The attribute names every ``LogRecord`` carries; anything else is ``extra``."""


class JsonLogFormatter(logging.Formatter):
    """Render one record as one JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            key: value
            for key, value in record.__dict__.items()
            if key not in STANDARD_RECORD_ATTRIBUTES
        }
        document["time"] = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        document["level"] = record.levelname
        document["logger"] = record.name
        document["message"] = record.getMessage()
        if record.exc_info:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(document, ensure_ascii=False, default=str)


def use_json_logging(level: int = logging.INFO) -> None:
    """Send root-logger records to stderr as JSON, unless somebody already did."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=level, handlers=[handler])


__all__ = ["STANDARD_RECORD_ATTRIBUTES", "JsonLogFormatter", "use_json_logging"]
