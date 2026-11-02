"""Real Job HTTP boundary with synthetic facts/store, without services or credentials."""

from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.interfaces.http.jobs import create_job_router
from tests.unit.backend.test_public_job_replay_race import KEY, InterleavedStore
from tests.unit.backend.test_public_jobs import BINDING, _pin_facts, _pinned_body, _service


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"current": False}, "model_binding_not_current"),
        ({"retired": True}, "model_binding_retired"),
        (
            {"definition": {"logical_model": "other-tts", "tasks": ["tts_file"]}},
            "model_binding_mismatch",
        ),
        (
            {"definition": {"logical_model": "narrator-tts", "tasks": ["asr_file"]}},
            "model_binding_task_not_supported",
        ),
    ],
)
async def test_http_returns_stable_selection_error_without_write(
    change: dict[str, Any], code: str
) -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    facts.binding = replace(facts.binding, **change)
    app = _app(service)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        reply = await client.post("/v1/nexus/jobs", json=_pinned_body())
    assert reply.status_code == 422
    assert reply.json()["error"]["code"] == code
    assert reply.json()["error"]["retryable"] is False
    assert not store.requests


def _app(service: Any) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = "req-http-pin"
        return await call_next(request)

    app.include_router(create_job_router(lambda: service))
    return app


async def test_http_pin_replay_after_retirement_and_changed_pin_conflict() -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    async with AsyncClient(
        transport=ASGITransport(_app(service)), base_url="http://test"
    ) as client:
        headers = {"Idempotency-Key": "idem-http-pin-0001"}
        original = await client.post("/v1/nexus/jobs", json=_pinned_body(), headers=headers)
        assert original.status_code == 202
        facts.binding = replace(facts.binding, retired=True)
        replay = await client.post("/v1/nexus/jobs", json=_pinned_body(), headers=headers)
        assert replay.status_code == 202 and replay.json() == original.json()
        assert replay.json()["effective_configuration"]["models"]["tts"] == BINDING.reference
        changed = {**_pinned_body(), "model_binding": {**BINDING.reference, "revision": "3"}}
        conflict = await client.post("/v1/nexus/jobs", json=changed, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        missing = await client.post("/v1/nexus/jobs", json=changed)
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "model_binding_not_published"
    assert len(store.jobs) == 1


@pytest.mark.parametrize("different_intent", [True, False])
async def test_http_reconciles_commit_between_empty_replay_and_retirement(
    different_intent: bool,
) -> None:
    service, facts, _ = _service()
    _pin_facts(facts)
    store = InterleavedStore()
    service = type(service)(facts, store)
    original: list[dict[str, Any]] = []

    def commit_then_retire() -> None:
        job = service.submit(_pinned_body(), request_id="a", idempotency_key=KEY)
        original.append({"id": job.job_id, "submission": job.submission})
        facts.binding = replace(facts.binding, retired=True)

    store.after_empty = commit_then_retire
    body = _pinned_body()
    if different_intent:
        body["model_binding"] = {**BINDING.reference, "revision": "3"}
    async with AsyncClient(
        transport=ASGITransport(_app(service)), base_url="http://test"
    ) as client:
        reply = await client.post("/v1/nexus/jobs", json=body, headers={"Idempotency-Key": KEY})
    if different_intent:
        assert reply.status_code == 409
        assert reply.json()["error"]["code"] == "idempotency_conflict"
    else:
        assert reply.status_code == 202
        assert reply.json()["id"] == original[0]["id"]
        assert reply.json()["effective_configuration"]["models"]["tts"] == BINDING.reference
        assert reply.headers["location"].endswith(original[0]["id"])
    assert len(store.jobs) == len(store.requests) == 1


async def test_http_failed_rejection_coordination_returns_recoverable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    facts.binding = replace(facts.binding, retired=True)
    calls = 0

    def lookup(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublicJobUnavailableError

    monkeypatch.setattr(store, "find_by_key", lookup)
    async with AsyncClient(
        transport=ASGITransport(_app(service)), base_url="http://test"
    ) as client:
        reply = await client.post(
            "/v1/nexus/jobs", json=_pinned_body(), headers={"Idempotency-Key": KEY}
        )
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "job_outcome_unknown"
    assert reply.json()["error"]["retryable"] is True
    assert calls == 2 and not store.jobs
