"""Pure internal execution request normalization shared by protocol consumers.

The caller owns RFC 8785 serialization and hashing; no private request is logged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def effective_execution_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Reproduce the gateway's effective submission document without changing wire data."""
    execution = dict(request)
    deadline = datetime.fromisoformat(execution["deadline_at"])
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("execution deadline must be timezone aware")
    execution["deadline_at"] = (
        deadline.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    stream = None
    if execution["sample_rate_hz"] is not None:
        operation_id, attempt_id = execution["operation_id"], execution["attempt_id"]
        candidate = f"{operation_id}.{attempt_id}"
        if len(candidate) <= 128:
            stream = candidate
        else:
            suffix = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:32]
            stream = f"{operation_id[:80]}.{suffix}"
    return {"execution_request": execution, "output_stream_id": stream}
