"""The network entry rejects untrusted authorities before any business/DB access."""

import httpx
import pytest

from speechnexus_backend.workbench.runtime import create_runtime_app
from tests.unit.backend.test_workbench_runtime_settings import configuration


@pytest.mark.parametrize(
    "headers",
    [
        [("Host", "evil.example")],
        [("Host", "evil.example@localhost")],
        [("Host", "localhost/path")],
        [("Host", "localhost:invalid")],
        [("Host", "localhost"), ("Host", "localhost")],
        [("Host", "localhost"), ("Origin", "https://evil.example")],
        [("Host", "localhost"), ("Origin", "null")],
        [
            ("Host", "localhost"),
            ("Origin", "http://localhost:8899"),
            ("Origin", "http://localhost:8899"),
        ],
    ],
)
async def test_runtime_ingress_rejects_untrusted_or_duplicate_headers(
    headers: list[tuple[str, str]],
) -> None:
    # No lifespan here: this unit tests ingress only; real lifespan is tested against PG.
    app = create_runtime_app(configuration())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as c:
        response = await c.get("/v1/workbench/status", headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.headers["Cache-Control"] == "no-store"


async def test_runtime_ingress_allows_explicit_local_origin_and_headless_requests() -> None:
    app = create_runtime_app(configuration(allowed_origins=("http://localhost:8899",)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as c:
        for headers in (
            {"Host": "localhost:8899"},
            {"Host": "localhost:8899", "Origin": "http://localhost:8899"},
        ):
            response = await c.get("/v1/workbench/status", headers=headers)
            assert response.status_code == 200
            assert response.json()["llm_enabled"] is False
