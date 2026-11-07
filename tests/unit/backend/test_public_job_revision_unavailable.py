"""A missing admission role is a retryable 503, not a contract violation."""

import pytest

from speechnexus_backend.speech.application.public_jobs import _revision_result
from speechnexus_backend.speech.domain.public_jobs import (
    PublicJobUnavailableError,
    RevisionCreateResult,
)


def test_revision_without_admission_role_is_unavailable() -> None:
    with pytest.raises(PublicJobUnavailableError):
        _revision_result(RevisionCreateResult("rejected", error_code="job_unavailable"))
