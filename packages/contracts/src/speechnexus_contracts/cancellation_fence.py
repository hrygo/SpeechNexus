"""Shared identity and lock namespace for public Job cancellation fencing.

This module is not a wire contract. The backend acceptance transaction and the
execution gateway must derive the same PostgreSQL transaction-scoped advisory
lock key: acquiring it before writing or reading the durable cancellation fence
orders the two transactions, so a submit either observes the committed fence or
commits before acceptance. The digest is stable across PostgreSQL versions and
across both processes.
"""

from __future__ import annotations

import hashlib

#: Durable fact shared by the backend (writer) and gateway (reader).
JOB_CANCELLATION_FENCE_TABLE = "speech_job_cancellation_fences"

_LOCK_DOMAIN = b"speechnexus:job-cancellation-fence:"


def job_cancellation_fence_lock_key(job_id: str) -> int:
    """Return the signed 64-bit advisory-lock key for one Job's fence."""
    if not isinstance(job_id, str) or not job_id.isascii() or not 1 <= len(job_id) <= 128:
        raise ValueError("cancellation fencing requires a bounded ASCII Job identity")
    digest = hashlib.sha256(_LOCK_DOMAIN + job_id.encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


__all__ = ["JOB_CANCELLATION_FENCE_TABLE", "job_cancellation_fence_lock_key"]
