"""Shared identity of the durable public Job cancellation fence.

This module is not a wire contract. The backend acceptance transaction writes the
fence and the execution gateway reads it; both name the same table. No lock
namespace lives here: the two sides order themselves on the canonical Job row
(``coordination_jobs``), the same row the dispatch claim and the terminal
transition lock, so the fixed order stays bucket -> Job -> fence/outbox. That row
lock is portable SQL; a PostgreSQL advisory key would be outside the subset and
would have to be derived identically by two processes.
"""

from __future__ import annotations

#: Durable fact shared by the backend (writer) and gateway (reader).
JOB_CANCELLATION_FENCE_TABLE = "speech_job_cancellation_fences"

__all__ = ["JOB_CANCELLATION_FENCE_TABLE"]
