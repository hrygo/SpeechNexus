"""A missing engine is named as an engine, never as a published model.

#877 item 4: with no registered engine instance, a create answered
``422 model_not_published`` (binding resolution) or the generic
``429 job_capacity_exhausted`` "retry after the indicated delay", while the
operator's real missing prerequisite was engine registration. The read-only
self-check already named ``instance-asr-1 generation 1 not registered``.
These tests manufacture "binding exists, no ready instance" and "nothing is
published or registered" and assert the public answer names the engine/instance.
The codes stay the ones the contract declares.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import parse

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import (
    JobAdmissionRejectedError,
    ModelNotPublishedError,
    PublicJobs,
)
from speechnexus_backend.speech.domain.public_jobs import JobCreateResult
from speechnexus_backend.speech.infrastructure.public_job_admission import (
    PublicJobAdmissionRejectedError,
    _instance,
)
from speechnexus_backend.speech.interfaces.http import jobs as http
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import NOW, Facts, Store, _body


class Cursor:
    """The two cursor methods ``_instance`` uses, answering from a queue."""

    def __init__(self, replies: list[list[dict[str, Any]]]) -> None:
        self.replies = list(replies)
        self.queries: list[str] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.queries.append(query)

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.replies.pop(0)) if self.replies else []


def test_no_registered_instance_names_the_engine_registration_step() -> None:
    cursor = Cursor([[], []])

    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _instance(cursor, "binding-rev-1", "model-asr-1")

    assert raised.value.code == "job_capacity_exhausted"
    # The refusal names the missing deployment input and the step that creates it;
    # it never claims the caller should simply retry.
    detail = raised.value.detail
    assert detail is not None
    assert "engine" in detail.lower()
    assert "register" in detail.lower()
    assert "model-asr-1" in detail


def test_registered_but_unready_instances_name_their_states() -> None:
    cursor = Cursor([[], [{"instance_state": "draining"}, {"instance_state": "stopped"}]])

    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _instance(cursor, "binding-rev-1", "model-asr-1")

    assert raised.value.code == "job_capacity_exhausted"
    detail = raised.value.detail
    assert detail is not None
    assert "draining" in detail
    assert "stopped" in detail


def test_an_unpublished_binding_without_an_instance_names_the_engine() -> None:
    """From zero, the 422 must not send the operator to the model catalogue."""
    service = PublicJobs(Facts(), Store(), now_us=lambda: NOW)

    with pytest.raises(ModelNotPublishedError) as raised:
        service.submit(
            parse("JobCreateRequest", _body(model="model-asr-1")),
            request_id="req-no-engine",
            idempotency_key=None,
        )

    message = str(raised.value)
    assert "engine" in message.lower()
    assert "register" in message.lower()


def test_registered_instances_without_a_publication_name_the_publication_step() -> None:
    """The two repairs behind one 422 are distinguishable, so the answer picks one.

    "No published binding and no instance" is one state; "instances exist but no
    binding revision is published" is another, and only the first is repaired by
    registering the engine. A single sentence for both sent the operator of the
    second state back to engine registration, which changes nothing. This pins the
    registration-fact arm: the answer names the states and the publication step,
    and it must not claim that no instance is registered (#877 item 4).
    """

    class RegisteredButUnpublished(Facts):
        def latest_binding(self, model_id: str) -> Any:
            return None

        def engine_instance_states(self, model_id: str) -> tuple[str, ...]:
            return ("draining", "stopped")

    service = PublicJobs(RegisteredButUnpublished(), Store(), now_us=lambda: NOW)

    with pytest.raises(ModelNotPublishedError) as raised:
        service.submit(
            parse("JobCreateRequest", _body(model="model-asr-1")),
            request_id="req-registered-unpublished",
            idempotency_key=None,
        )

    message = str(raised.value)
    assert "draining" in message and "stopped" in message
    assert "publish the binding" in message
    # Registration is not what is missing here, so the other arm's sentence is wrong.
    assert "no engine instance is registered" not in message


async def test_the_http_answer_carries_the_named_precondition() -> None:
    class Unpublished:
        def submit(self, *args: Any, **kwargs: Any) -> Any:
            raise ModelNotPublishedError(
                "The model 'model-asr-1' has no published binding revision and no "
                "engine instance is registered for it: publish the binding and "
                "register the engine (engine-registration profile)"
            )

    app = FastAPI()

    @app.middleware("http")
    async def request_id(request: Any, call_next: Any) -> Any:
        request.state.request_id = "req-unpublished"
        return await call_next(request)

    app.include_router(http.create_job_router(lambda: cast(Any, Unpublished())))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/nexus/jobs", json=_body())

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "model_not_published"
    assert "engine" in error["message"].lower()
    assert error["message"] != "The model is not published."


def test_a_published_binding_without_a_ready_instance_names_the_instance() -> None:
    """The constructed "binding exists, no ready instance" arm (#877 item 4).

    The binding is published (the fake facts resolve it), so the failure is the
    admission read that finds no ready instance. The public code stays the
    contract's ``429 job_capacity_exhausted``; the message names the engine rather
    than telling the caller to retry.
    """

    class NoReadyInstance(Store):
        def create(self, job: Any) -> JobCreateResult:
            return JobCreateResult(
                "rejected",
                error_code="job_capacity_exhausted",
                detail=(
                    "no engine instance is registered for model 'tts-quality': register "
                    "the engine (engine-registration profile / speechnexus-register-engine) "
                    "so admission has a ready instance; retrying this request cannot create one"
                ),
            )

    service = PublicJobs(Facts(), NoReadyInstance(), now_us=lambda: NOW)

    with pytest.raises(JobAdmissionRejectedError) as raised:
        service.submit(
            parse("JobCreateRequest", _body()),
            request_id="req-no-ready-instance",
            idempotency_key=None,
        )

    assert raised.value.code == "job_capacity_exhausted"
    detail = raised.value.detail
    assert detail is not None
    assert "engine" in detail.lower()
    assert "register" in detail.lower()


async def test_the_http_capacity_answer_carries_the_named_instance() -> None:
    class NoReadyInstance(Store):
        def create(self, job: Any) -> JobCreateResult:
            return JobCreateResult(
                "rejected",
                error_code="job_capacity_exhausted",
                detail=(
                    "no engine instance is registered for model 'tts-quality': register "
                    "the engine (engine-registration profile) so admission has a ready "
                    "instance; retrying this request cannot create one"
                ),
            )

    service = PublicJobs(Facts(), NoReadyInstance(), now_us=lambda: NOW)
    app = create_app(settings=_settings(), jobs=service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/nexus/jobs", json=_body())

    assert response.status_code == 429, response.text
    error = response.json()["error"]
    assert error["code"] == "job_capacity_exhausted"
    assert "engine" in error["message"].lower()
    assert "register" in error["message"].lower()
    assert response.headers["retry-after"] == "1"
