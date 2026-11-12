"""Closed private wire vocabulary from `contracts/internal/asset-inspection.openapi.yaml`.

The failure envelope carries no message, path, command, token or input fragment:
the stable ``status`` value is the only discriminator.
"""

from __future__ import annotations

import json

SCHEMA_VERSION = 1
CONTRACT_VERSION = 1
RECIPE_VERSION = "pcm-decode-1"
WAV_MEDIA_TYPE = "audio/wav"

# Stable status tokens, one per private response class. This mapping is the
# only place a status becomes an HTTP code.
HTTP_STATUS: dict[str, int] = {
    "bad_request": 400,
    "unauthorized": 401,
    "encoded_too_large": 413,
    "unsupported_media": 415,
    "invalid_media": 422,
    "media_limit_exceeded": 422,
    "busy": 429,
    "unavailable": 503,
    "deadline_exceeded": 504,
}

FAILURE_STATUSES = frozenset(HTTP_STATUS)


def failure_body(status: str) -> bytes:
    """Encode the exact failure envelope; reject an unknown status before writing."""
    if status not in FAILURE_STATUSES:
        raise ValueError("Unknown inspection failure status")
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "status": status},
        separators=(",", ":"),
    ).encode("ascii")
