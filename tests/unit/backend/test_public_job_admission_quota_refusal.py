"""A missing quota row is a deterministic refusal; a failed read is still an outage.

#702: ``speech_workspaces`` is seeded with ``workspace-default`` by the asset
migration, while ``workspace_quotas`` is written only by engine registration. The
quota read therefore has to separate "the store answered with no row" from "the
read failed": only the first is a configuration fact a caller can act on, and it
must not borrow the 503 ``job_unavailable`` storage-outage code. The two storage
failures are also kept apart: a rejected query is ``storage_rejected`` -> 503
``job_unavailable`` (non-retryable), while a dead connection is ``outcome_unknown``
-> 503 ``job_outcome_unknown`` (retryable). These cases pin both directions at the
two places that decide the public answer.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from speechnexus_backend.speech.application.public_jobs import (
    JobAdmissionRejectedError,
    JobOutcomeUnknownError,
    JobRejectedError,
    _submission_result,
)
from speechnexus_backend.speech.domain.public_jobs import JobCreateResult
from speechnexus_backend.speech.infrastructure.public_job_admission import (
    PublicJobAdmissionRejectedError,
    _quota,
)

WORKSPACE = "workspace-default"


class Cursor:
    """The two cursor methods ``_quota`` uses, with a scripted answer."""

    def __init__(
        self, *, rows: list[dict[str, Any]] | None = None, failure: Exception | None = None
    ):
        self._rows = rows
        self._failure = failure
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.queries.append((query, parameters))
        if self._failure is not None:
            raise self._failure

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows or ())


def _row() -> dict[str, Any]:
    return {
        "policy_revision": "policy-test",
        "version": 1,
        "held_attempts": 0,
        "quarantined_attempts": 0,
        "workspace_policy_revision": 1,
    }


def test_a_workspace_without_a_quota_row_is_workspace_not_configured() -> None:
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _quota(Cursor(rows=[]), WORKSPACE)
    assert raised.value.code == "workspace_not_configured"


def test_an_ambiguous_quota_read_stays_the_storage_outage_code() -> None:
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _quota(Cursor(rows=[_row(), _row()]), WORKSPACE)
    assert raised.value.code == "job_unavailable"


def test_a_failed_quota_read_propagates_instead_of_becoming_the_4xx() -> None:
    # The adapter classifies a database failure, not this read: swallowing it into
    # workspace_not_configured would turn a real outage into a client-fixable 4xx.
    failure = psycopg.OperationalError("synthetic storage failure")
    with pytest.raises(psycopg.OperationalError) as raised:
        _quota(Cursor(failure=failure), WORKSPACE)
    assert raised.value is failure


def test_a_present_quota_row_is_returned_unchanged() -> None:
    row = _row()
    assert _quota(Cursor(rows=[row]), WORKSPACE) == row


def test_the_new_code_reaches_http_as_a_refusal_not_the_unavailable_fallback() -> None:
    # ``_ADMISSION_REJECTION_CODES`` decides whether the store's code becomes the
    # mapped 422 or falls through to ``JobRejectedError`` (503 ``job_unavailable``).
    # Omitting the new code here would silently restore the defect at the HTTP layer.
    with pytest.raises(JobAdmissionRejectedError) as raised:
        _submission_result(JobCreateResult("rejected", error_code="workspace_not_configured"))
    assert raised.value.code == "workspace_not_configured"


def test_a_storage_rejection_keeps_the_unavailable_fallback() -> None:
    with pytest.raises(JobRejectedError):
        _submission_result(JobCreateResult("rejected", error_code="storage_rejected"))


def test_a_connection_level_failure_stays_the_retryable_unknown_outcome() -> None:
    # The other storage failure: a dead connection is ``outcome_unknown`` (503
    # ``job_outcome_unknown``, retryable), not the non-retryable ``job_unavailable``
    # that a rejected query answers (#702 review M2).
    with pytest.raises(JobOutcomeUnknownError):
        _submission_result(JobCreateResult("unknown", error_code="outcome_unknown"))
