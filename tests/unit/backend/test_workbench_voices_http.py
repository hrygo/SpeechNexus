"""The frozen voice operations answer as declared, with and without a speech base."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.application.voices import (
    VoiceCatalogueUnavailableError,
    WorkbenchVoices,
)
from speechnexus_backend.workbench.domain.voices import (
    VoiceCataloguePage,
    VoiceRevision,
    VoiceSummary,
)
from speechnexus_backend.workbench.interfaces.http_voices import create_voice_router

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
DEFAULT_WORKSPACE = "workspace-default"
VOICE = "voice-demo-1"
BINDING = "binding-demo-1"


def summary() -> VoiceSummary:
    return VoiceSummary(
        voice_id=VOICE,
        revision="2",
        display_name="合成旁白",
        source_type="designed",
        status="published",
        languages=("zh-CN",),
        audio_formats=("wav",),
    )


def revision() -> VoiceRevision:
    return VoiceRevision(
        voice_id=VOICE,
        revision="2",
        display_name="合成旁白",
        status="published",
        languages=("zh-CN",),
        audio_formats=("wav",),
        model_binding_id=BINDING,
        model_binding_revision="1",
        published_at=NOW,
    )


def revision_value() -> VoiceRevision:
    return revision()


class Port:
    """The port the router sees, with the two reads the frozen operations need."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.failure: Exception | None = None

    async def list_summaries(
        self, *, workspace_id: str, language: str | None, limit: int, after: str | None
    ) -> VoiceCataloguePage:
        self.calls.append(("list", workspace_id, language, limit, after))
        if self.failure is not None:
            raise self.failure
        return VoiceCataloguePage(items=(summary(),), has_more=True, next_cursor="base-cursor-2")

    async def get_revision(self, *, voice_id: str, revision: str) -> VoiceRevision:
        self.calls.append(("get", voice_id, revision))
        if self.failure is not None:
            raise self.failure
        return revision_value()


def build(service: WorkbenchVoices | None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_voice_router(
            service,
            default_workspace_id=DEFAULT_WORKSPACE,
            max_json_bytes=64 * 1024,
            body_timeout_seconds=2,
        )
    )
    return app


async def call(app: FastAPI, path: str, **query: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://workbench.test") as client:
        return await client.get(path, params=query)


async def test_a_listing_is_declared_and_defaults_to_the_configured_workspace() -> None:
    port = Port()
    response = await call(build(WorkbenchVoices(port)), "/v1/workbench/voices", limit=10)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "nexus.list"
    assert body["has_more"] is True and body["next_cursor"] == "base-cursor-2"
    assert body["data"][0]["voice_id"] == VOICE
    assert body["data"][0]["revision"] == "2"
    assert port.calls == [("list", DEFAULT_WORKSPACE, None, 10, None)]


async def test_a_listing_forwards_language_workspace_and_the_bases_cursor() -> None:
    port = Port()
    response = await call(
        build(WorkbenchVoices(port)),
        "/v1/workbench/voices",
        workspace_id="workspace-other",
        language="zh-CN",
        after="base-cursor-1",
    )
    assert response.status_code == 200
    assert port.calls == [("list", "workspace-other", "zh-CN", 50, "base-cursor-1")]


async def test_a_pinned_revision_is_declared() -> None:
    port = Port()
    response = await call(build(WorkbenchVoices(port)), f"/v1/workbench/voices/{VOICE}/revisions/2")
    assert response.status_code == 200
    body = response.json()
    assert body["voice_id"] == VOICE
    assert body["model_binding"] == {"id": BINDING, "revision": "1"}
    assert body["published_at"].endswith("Z") and body["retired_at"] is None
    assert port.calls == [("get", VOICE, "2")]


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("length", [128, 129, 2000])
async def test_complete_disabled_reason_uses_v2_without_breaking_v1(
    version: str, length: int
) -> None:
    reason = "停" * length

    class DisabledPort(Port):
        async def get_revision(self, *, voice_id: str, revision: str) -> VoiceRevision:
            return replace(
                revision_value(), status="disabled", retired_at=NOW, disabled_reason=reason
            )

    response = await call(
        build(WorkbenchVoices(DisabledPort())), f"/{version}/workbench/voices/{VOICE}/revisions/2"
    )
    if version == "v1" and length > 128:
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "voice_catalogue_unavailable"
    else:
        assert response.status_code == 200, response.text
        assert response.json()["disabled_reason"] == reason
        assert response.json()["retired_at"] == "2026-10-24T12:00:00Z"


async def test_v2_oversized_get_body_is_a_declared_error_without_calling_the_base() -> None:
    port = Port()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(WorkbenchVoices(port))),
        base_url="http://workbench.test",
    ) as client:
        response = await client.request(
            "GET", f"/v2/workbench/voices/{VOICE}/revisions/2", content=b"x" * (64 * 1024 + 1)
        )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "request_too_large"
    assert port.calls == []


async def test_a_missing_revision_is_a_declared_not_found() -> None:
    port = Port()
    port.failure = ResourceNotFoundError()
    response = await call(build(WorkbenchVoices(port)), f"/v1/workbench/voices/{VOICE}/revisions/9")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


@pytest.mark.parametrize("operation", ["list", "get"])
async def test_without_a_speech_base_the_operations_say_unavailable(operation: str) -> None:
    path = (
        "/v1/workbench/voices"
        if operation == "list"
        else f"/v1/workbench/voices/{VOICE}/revisions/2"
    )
    response = await call(build(None), path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "voice_catalogue_unavailable"


@pytest.mark.parametrize("operation", ["list", "get"])
async def test_an_unreachable_base_is_reported_as_unavailable(operation: str) -> None:
    port = Port()
    port.failure = VoiceCatalogueUnavailableError()
    path = (
        "/v1/workbench/voices"
        if operation == "list"
        else f"/v1/workbench/voices/{VOICE}/revisions/2"
    )
    response = await call(build(WorkbenchVoices(port)), path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "voice_catalogue_unavailable"


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/v1/workbench/voices", {"limit": 0}),
        ("/v1/workbench/voices", {"limit": 101}),
        (f"/v1/workbench/voices/{VOICE}/revisions/02", {}),
        ("/v1/workbench/voices/Bad/revisions/2", {}),
        ("/v1/workbench/voices", {"after": ""}),
    ],
    ids=[
        "limit-zero",
        "limit-too-large",
        "leading-zero-revision",
        "invalid-voice-id",
        "empty-cursor",
    ],
)
async def test_a_request_outside_the_frozen_scope_is_refused_before_any_call(
    path: str, query: dict[str, Any]
) -> None:
    port = Port()
    response = await call(build(WorkbenchVoices(port)), path, **query)
    assert response.status_code == 400
    assert port.calls == []
