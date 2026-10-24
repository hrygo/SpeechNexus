"""Bounded immutable configuration values shared by independent resource families."""

import hashlib
import re

MAX_REVISION = 10**19 - 1
MAX_DOCUMENT_BYTES = 262144


def revision_number(value: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        raise ValueError("Invalid configuration revision")
    return int(value)


def document_digest(value: bytes) -> str:
    if not isinstance(value, bytes) or not 1 <= len(value) <= MAX_DOCUMENT_BYTES:
        raise ValueError("Invalid bounded configuration document")
    return "sha256:" + hashlib.sha256(value).hexdigest()
