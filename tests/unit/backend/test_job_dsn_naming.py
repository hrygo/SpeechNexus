"""The unassembled Job surface names the blank DSN an operator must fill.

#572 AC2 (remaining half): a missing prerequisite fails before a claim and names
the condition. The Job surface is assembled only when the writer DSN is set, and
a create is admitted only when the admission DSN is set. Both absences used to
answer the same ``503 job_unavailable`` / "Job storage is unavailable.", so an
operator could not tell which key to set (#877 item 1).

#920 split the two answers apart and gave each one condition: the two absences no
longer share a message, and the surface-level answer names the one key that
assembles the surface instead of listing a second key it does not depend on.

These tests manufacture each absence and assert the answer names the key. No DSN
value is ever echoed: a deployment's credentials never cross this boundary.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import parse

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_prerequisites import ADMISSION_DSN, WRITER_DSN
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    PostgresPublicJobs,
)
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import NOW, Facts, _body

WRITER = "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
ADMISSION = "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"


async def test_blank_job_dsns_are_named_before_any_admission() -> None:
    """No Job store composed: the 503 names the writer key, not "storage".

    The writer DSN is the one key that assembles the surface, so it is the only key
    this answer names. A blank admission DSN is named by the create answer below,
    which is the operation it blocks: listing it here told an operator to set a key
    that could not change this refusal (#920).
    """
    app = create_app(settings=_settings())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/nexus/jobs", json=_body())

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    # The public code and its retryability are unchanged; only the message names
    # the configuration item (#877 red line: no contract change).
    assert error["code"] == "job_unavailable"
    assert error["type"] == "service_unavailable_error"
    assert error["retryable"] is False
    assert WRITER in error["message"]
    assert ADMISSION not in error["message"]
    # The answer is the shared table's row, not a second copy of the sentence.
    assert error["message"] == WRITER_DSN.detail
    assert "postgresql://" not in error["message"]


async def test_a_blank_admission_only_is_named_by_the_creation_path() -> None:
    """Writer set, admission blank: the refusal names the one missing key.

    This is the second configuration miss the old answer folded together: the
    store is assembled (reads still work) but a create cannot reserve.
    """

    class Refusing:
        def submit(self, *args: Any, **kwargs: Any) -> Any:
            raise PublicJobUnavailableError(ADMISSION_DSN.detail)

    app = create_app(settings=_settings(), jobs=cast(Any, Refusing()))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/nexus/jobs", json=_body())

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "job_unavailable"
    assert ADMISSION in error["message"]
    # The two absences answer differently: the admission row never borrows the
    # writer row's sentence (#920).
    assert error["message"] == ADMISSION_DSN.detail
    assert error["message"] != WRITER_DSN.detail
    assert error["message"] != "Job storage is unavailable."


def test_the_store_names_the_blank_admission_key_it_cannot_create_with() -> None:
    """The store-level refusal carries the key name instead of a bare code."""
    store = PostgresPublicJobs(
        cast(Any, None),
        require_admission=True,
        admission_unavailable_detail=ADMISSION_DSN.detail,
    )
    service = PublicJobs(Facts(), store, now_us=lambda: NOW)

    with pytest.raises(PublicJobUnavailableError) as raised:
        service.submit(
            parse("JobCreateRequest", _body()),
            request_id="req-admission-blank",
            idempotency_key=None,
        )

    assert ADMISSION in str(raised.value)
