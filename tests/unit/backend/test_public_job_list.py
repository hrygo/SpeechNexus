"""List Jobs through the real HTTP boundary and application cursor handling."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import parse

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.public_jobs import PublicJob, PublicJobUnavailableError
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import NOW, Facts, Store, _body

PREFIX = "/v1/nexus/jobs"


class ListStore(Store):
    failure: Exception | None = None

    def list_jobs(
        self,
        *,
        workspace_id: str,
        state: str | None,
        kind: str | None,
        after: tuple[int, str] | None,
        limit: int,
    ) -> tuple[PublicJob, ...]:
        if self.failure is not None:
            raise self.failure
        rows = sorted(self.jobs.values(), key=lambda job: (job.created_at_us, job.job_id))
        return tuple(
            job
            for job in rows
            if job.workspace_id == workspace_id
            and (state is None or job.state == state)
            and (kind is None or job.kind == kind)
            and (after is None or (job.created_at_us, job.job_id) > after)
        )[:limit]


def _service() -> tuple[PublicJobs, ListStore]:
    store = ListStore()
    service = PublicJobs(Facts(), store)
    sample = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-list", idempotency_key=None
    )
    store.jobs.clear()
    # Insert out of order; both the timestamp and tie-breaking id matter.
    for n in (4, 2, 3, 0, 1):
        job = replace(
            sample, job_id=f"job-{n:032d}", created_at_us=NOW + n // 3, updated_at_us=NOW + n // 3
        )
        store.jobs[job.job_id] = job
    other = replace(sample, job_id="job-other", workspace_id="workspace-other")
    store.jobs[other.job_id] = other
    return service, store


async def test_pages_are_complete_repeatable_and_workspace_scoped() -> None:
    service, _ = _service()
    app = create_app(settings=_settings(), jobs=service)
    seen: list[str] = []
    query = {"limit": "2"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        while True:
            page = await client.get(PREFIX, params=query)
            assert page.status_code == 200, page.text
            assert parse("JobList", page.json()) == page.json()
            assert page.headers["cache-control"] == "no-store"
            assert (await client.get(PREFIX, params=query)).json() == page.json()
            seen.extend(job["id"] for job in page.json()["data"])
            if not page.json()["has_more"]:
                assert page.json()["next_cursor"] is None
                break
            query["cursor"] = page.json()["next_cursor"]
        assert seen == [f"job-{n:032d}" for n in range(5)]
        other = await client.get(PREFIX, params={"workspace_id": "workspace-other"})
        assert [job["id"] for job in other.json()["data"]] == ["job-other"]
        assert (await client.get(PREFIX, params={"workspace_id": "workspace-missing"})).json()[
            "data"
        ] == []


@pytest.mark.parametrize(
    "query",
    [
        {"status": "pending"},
        {"status": "unknown"},
        {"status": ""},
        {"kind": "speech"},
        {"kind": ""},
        {"limit": "0"},
        {"limit": "-1"},
        {"limit": "101"},
        {"limit": "1.5"},
        {"limit": "true"},
        {"limit": ""},
        {"limit": "9" * 1000},
        {"workspace_id": "NOT_VALID"},
        {"workspace_id": ""},
        {"cursor": ""},
        {"cursor": "garbage"},
        {"cursor": "x" * 513},
        [("status", "queued"), ("status", "running")],
    ],
)
async def test_invalid_queries_return_400(query: Any) -> None:
    service, _ = _service()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings=_settings(), jobs=service)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PREFIX, params=query, headers={"X-Request-ID": "req-list-invalid"}
        )
    assert response.status_code == 400, response.text
    assert parse("ErrorResponse", response.json())["error"]["code"] == "invalid_job_request"
    assert response.json()["error"]["request_id"] == "req-list-invalid"


async def test_filters_and_limits_accept_the_complete_contract_enum() -> None:
    service, store = _service()
    job = store.jobs[f"job-{0:032d}"]
    store.jobs[job.job_id] = replace(job, state="running")
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings=_settings(), jobs=service)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PREFIX, params={"status": "running", "kind": "tts", "limit": "1"}
        )
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["data"]] == [job.job_id]
        for query in (
            {"kind": "asr"},
            {"kind": "voice_design"},
            {"kind": "voice_clone"},
            {"kind": "voice_preview"},
            {"kind": "voice_validation"},
            {"status": "retry_wait"},
        ):
            response = await client.get(PREFIX, params=query)
            assert response.status_code == 200, response.text
            assert response.json()["data"] == []
        for limit in (None, "100"):
            response = await client.get(PREFIX, params={} if limit is None else {"limit": limit})
            assert response.status_code == 200
            assert len(response.json()["data"]) == 5


async def test_default_limit_and_upper_limit_bound_the_page() -> None:
    service, store = _service()
    sample = next(iter(store.jobs.values()))
    store.jobs.clear()
    for n in range(115):
        job = replace(sample, job_id=f"job-{n:032d}")
        store.jobs[job.job_id] = job
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings=_settings(), jobs=service)),
        base_url="http://test",
    ) as client:
        first = await client.get(PREFIX)
        assert first.status_code == 200
        assert len(first.json()["data"]) == 50
        remainder = await client.get(
            PREFIX, params={"limit": "100", "cursor": first.json()["next_cursor"]}
        )
        assert remainder.status_code == 200
        assert len(remainder.json()["data"]) == 65
        assert remainder.json()["has_more"] is False
        assert remainder.json()["data"][0]["id"] == f"job-{50:032d}"
        upper = await client.get(PREFIX, params={"limit": "100"})
        assert len(upper.json()["data"]) == 100
        assert upper.json()["has_more"] is True


async def test_cursor_is_bound_to_filters_and_storage_failure_is_503() -> None:
    service, store = _service()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings=_settings(), jobs=service)),
        base_url="http://test",
    ) as client:
        page = await client.get(PREFIX, params={"limit": "1"})
        assert page.status_code == 200, page.text
        cursor = page.json()["next_cursor"]
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        malformed_cursors: list[Any] = [
            [True, *raw[1:]],
            [2, *raw[1:]],
            [1, -1, *raw[2:]],
            [1, True, *raw[2:]],
            [1, 253402300800000000, *raw[2:]],
            [1, raw[1], "NOT_VALID", raw[3]],
            {},
            [1],
        ]
        for malformed in malformed_cursors:
            token = base64.urlsafe_b64encode(json.dumps(malformed).encode()).decode().rstrip("=")
            response = await client.get(PREFIX, params={"cursor": token})
            assert response.status_code == 400
        for changed in ({"workspace_id": "workspace-other"}, {"status": "queued"}, {"kind": "tts"}):
            response = await client.get(PREFIX, params={"cursor": cursor, **changed})
            assert response.status_code == 400
        store.failure = PublicJobUnavailableError()
        response = await client.get(PREFIX)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "job_unavailable"
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings=_settings())), base_url="http://test"
    ) as client:
        assert (await client.get(PREFIX)).status_code == 503
