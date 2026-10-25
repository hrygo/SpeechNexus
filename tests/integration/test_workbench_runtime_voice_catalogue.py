"""Native public-source HTTP through the actual C lifespan, ingress and projection.

The source is synthetic, contract-valid JSON served over a real loopback socket; no
voice producer, model, object-storage or physical-topology qualification is claimed.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from speechnexus_contracts.voice_catalogue_http import validate_response
from tests.integration.test_workbench_runtime import Database, serve
from tests.integration.test_workbench_runtime import database as database

from speechnexus_backend.workbench.runtime import create_runtime_app
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = {
    example["id"]: example["value"]["body"]
    for example in json.loads(
        (ROOT / "tests/fixtures/contracts/voice-catalogue-http.json").read_text()
    )["examples"]
}


def catalogue_source(payload: dict[str, Any]) -> tuple[FastAPI, list[dict[str, Any]]]:
    app = FastAPI()
    reads: list[dict[str, Any]] = []

    @app.get("/v1/nexus/voices")
    async def listing(request: Request) -> JSONResponse:
        reads.append({"path": request.url.path, "query": dict(request.query_params)})
        page = deepcopy(EXAMPLES["list"])
        page["data"][0]["status"] = payload["status"]
        return JSONResponse(page)

    @app.get("/v1/nexus/voices/{voice_id}/revisions/{revision}")
    async def revision(voice_id: str, revision: str, request: Request) -> JSONResponse:
        reads.append({"path": request.url.path, "query": dict(request.query_params)})
        return JSONResponse(payload)

    return app, reads


def catalogue_settings(origin: str) -> dict[str, Any]:
    # The runtime currently requires speech and object-zone settings together. These
    # synthetically unreachable object settings must remain unused by catalogue reads.
    return {
        "speech_base_url": origin,
        "object_endpoint": "http://127.0.0.1:1",
        "object_bucket": "synthetic-unused-voice-catalogue",
        "object_access_key": "synthetic-unused-access",
        "object_secret_key": "synthetic-unused-secret",
    }


@pytest.mark.parametrize("example", ["published", "retired", "disabled", "disabled-reason-2000"])
async def test_canonical_catalogue_is_projected_by_the_real_runtime(
    database: Database, example: str
) -> None:
    payload = deepcopy(EXAMPLES[example])
    validate_response("getVoiceRevision", 200, {"body": payload})
    source, reads = catalogue_source(payload)
    async with serve(source) as upstream:
        settings = WorkbenchSettings.model_validate(
            database.settings().model_dump() | catalogue_settings(str(upstream.base_url))
        )
        async with serve(create_runtime_app(settings)) as client:
            page = await client.get(
                "/v1/workbench/voices",
                params={"language": "zh-CN", "limit": 10, "after": "base-cursor-1"},
            )
            assert page.status_code == 200, page.text
            assert page.json()["data"][0]["voice_id"] == payload["voice_id"]
            assert page.json()["next_cursor"] == "base-cursor-2"
            response = await client.get("/v2/workbench/voices/voice-demo-1/revisions/2")
            assert response.status_code == 200, response.text
            assert response.json() == {
                "voice_id": payload["voice_id"],
                "revision": payload["revision"],
                "display_name": payload["display_name"],
                "status": payload["status"],
                "languages": payload["capabilities"]["languages"],
                "audio_formats": payload["capabilities"]["audio_formats"],
                "model_binding": payload["model_binding"],
                "published_at": payload["published_at"],
                "retired_at": payload["retired_at"],
                "disabled_reason": payload["disabled_reason"],
            }
            legacy = await client.get("/v1/workbench/voices/voice-demo-1/revisions/2")
            if example == "disabled-reason-2000":
                assert legacy.status_code == 503, legacy.text
                assert legacy.json()["error"]["code"] == "voice_catalogue_unavailable"
            else:
                assert legacy.status_code == 200 and legacy.json() == response.json()
    assert reads == [
        {
            "path": "/v1/nexus/voices",
            "query": {
                "workspace_id": "workspace-runtime",
                "language": "zh-CN",
                "limit": "10",
                "cursor": "base-cursor-1",
            },
        },
        {"path": "/v1/nexus/voices/voice-demo-1/revisions/2", "query": {}},
        {"path": "/v1/nexus/voices/voice-demo-1/revisions/2", "query": {}},
    ]


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-voice",
        "wrong-revision",
        "legacy-flattened",
        "disabled-no-retirement",
        "private-endpoint",
    ],
)
async def test_source_fault_never_becomes_a_successful_workbench_revision(
    database: Database, fault: str
) -> None:
    payload = deepcopy(EXAMPLES.get(fault, EXAMPLES["published"]))
    if fault == "wrong-voice":
        payload["voice_id"] = "voice-other-1"
    if fault == "wrong-revision":
        payload["revision"] = "3"
    if fault.startswith("wrong-"):
        # A schema-valid reply still must match the user's requested immutable identity.
        validate_response("getVoiceRevision", 200, {"body": payload})
    source, reads = catalogue_source(payload)
    async with serve(source) as upstream:
        settings = WorkbenchSettings.model_validate(
            database.settings().model_dump() | catalogue_settings(str(upstream.base_url))
        )
        async with serve(create_runtime_app(settings)) as client:
            for version in ("v1", "v2"):
                response = await client.get(f"/{version}/workbench/voices/voice-demo-1/revisions/2")
                assert response.status_code == 503, response.text
                body = response.json()
                assert body["error"]["code"] == "voice_catalogue_unavailable"
                assert "synthetic-private-field" not in response.text
                assert "voice-other-1" not in response.text
    assert reads == [{"path": "/v1/nexus/voices/voice-demo-1/revisions/2", "query": {}}] * 2
