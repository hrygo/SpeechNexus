"""The voice catalogue adapter maps the base's answers without inventing any of them."""

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import httpx
import pytest

from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.application.voices import VoiceCatalogueUnavailableError
from speechnexus_backend.workbench.infrastructure.public_voice_catalogue import (
    PublicVoiceCatalogue,
)

BASE = "http://speech.internal:8080"
WORKSPACE = "workspace-synthetic"
SUMMARY = {
    "id": "voice-demo-1",
    "revision": "2",
    "display_name": "合成旁白",
    "source_type": "designed",
    "status": "published",
    "languages": ["zh-CN"],
    "audio_formats": ["wav"],
}
REVISION: dict[str, Any] = {
    "resource_type": "voice_revision",
    "voice_id": "voice-demo-1",
    "revision": "2",
    "object": "nexus.voice_revision",
    "status": "published",
    "display_name": "合成旁白",
    "source": {
        "type": "designed",
        "draft_id": "draft-demo-1",
        "draft_revision": "1",
        "source_asset_digest": None,
    },
    "capabilities": {
        "languages": ["zh-CN"],
        "audio_formats": ["wav"],
        "sample_rates_hz": [24000],
        "modes": ["complete"],
    },
    "validation": {
        "validation_id": "validation-demo-1",
        "status": "passed",
        "checked_at": "2026-10-24T12:00:00Z",
    },
    "model_binding": {"id": "binding-demo-1", "revision": "1"},
    "published_at": "2026-10-24T12:00:00Z",
    "retired_at": None,
    "disabled_reason": None,
}


class Stream(httpx.AsyncByteStream):
    """A body the adapter can stream once, like a real upstream socket."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._raw

    async def aclose(self) -> None:
        self.closed = True


def answer(
    payload: Any, *, status: int = 200, content_type: str = "application/json"
) -> httpx.Response:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return httpx.Response(status, headers={"content-type": content_type}, stream=Stream(raw))


def catalogue(handler: Any) -> PublicVoiceCatalogue:
    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    return PublicVoiceCatalogue(client)


async def test_a_listing_forwards_the_query_and_maps_the_bases_page() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(httpx.QueryParams(request.url.query))))
        return answer(
            {
                "object": "nexus.list",
                "data": [SUMMARY],
                "has_more": True,
                "next_cursor": "base-cursor-2",
            }
        )

    page = await catalogue(handler).list_summaries(
        workspace_id=WORKSPACE, language="zh-CN", limit=10, after="base-cursor-1"
    )
    assert seen == [
        (
            "/v1/nexus/voices",
            {
                "workspace_id": WORKSPACE,
                "limit": "10",
                "language": "zh-CN",
                "cursor": "base-cursor-1",
            },
        )
    ]
    assert page.has_more is True and page.next_cursor == "base-cursor-2"
    assert page.items[0].voice_id == "voice-demo-1"
    assert page.items[0].revision == "2"
    assert page.items[0].audio_formats == ("wav",)


async def test_a_listing_without_optional_filters_omits_them() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(httpx.QueryParams(request.url.query)))
        return answer({"object": "nexus.list", "data": [], "has_more": False, "next_cursor": None})

    page = await catalogue(handler).list_summaries(
        workspace_id=WORKSPACE, language=None, limit=50, after=None
    )
    assert seen == [{"workspace_id": WORKSPACE, "limit": "50"}]
    assert page.items == () and page.has_more is False and page.next_cursor is None


async def test_a_revision_carries_the_bases_pinned_binding_and_moments() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return answer(REVISION)

    value = await catalogue(handler).get_revision(voice_id="voice-demo-1", revision="2")
    assert seen == ["/v1/nexus/voices/voice-demo-1/revisions/2"]
    assert value.model_binding_id == "binding-demo-1"
    assert value.model_binding_revision == "1"
    assert value.published_at.year == 2026 and value.retired_at is None
    assert value.disabled_reason is None


async def test_a_retired_revision_keeps_its_retirement_moment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer({**REVISION, "status": "retired", "retired_at": "2026-10-25T08:00:00Z"})

    value = await catalogue(handler).get_revision(voice_id="voice-demo-1", revision="2")
    assert value.status == "retired" and value.retired_at is not None


async def test_a_disabled_revision_keeps_its_retirement_and_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(
            {
                **REVISION,
                "status": "disabled",
                "retired_at": "2026-10-25T08:00:00Z",
                "disabled_reason": "合成绑定已停用",
            }
        )

    value = await catalogue(handler).get_revision(voice_id="voice-demo-1", revision="2")
    assert value.status == "disabled" and value.retired_at is not None
    assert value.disabled_reason == "合成绑定已停用"


@pytest.mark.parametrize("length", [129, 2000])
async def test_a_canonical_disabled_reason_is_preserved_without_truncation(length: int) -> None:
    reason = "停" * length
    value = await catalogue(
        lambda _: answer(
            REVISION
            | {
                "status": "disabled",
                "retired_at": "2026-10-25T08:00:00Z",
                "disabled_reason": reason,
            }
        )
    ).get_revision(voice_id="voice-demo-1", revision="2")
    assert value.disabled_reason == reason


@pytest.mark.parametrize(
    "raw",
    [
        b'{"voice_id":"voice-other-1",' + json.dumps(REVISION).encode()[1:],
        b" " * (256 * 1024 + 1) + json.dumps(REVISION).encode(),
    ],
)
async def test_duplicate_keys_and_oversized_answers_are_closed(raw: bytes) -> None:
    stream = Stream(raw)
    async with httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    ) as client:
        with pytest.raises(VoiceCatalogueUnavailableError):
            await PublicVoiceCatalogue(client).get_revision(voice_id="voice-demo-1", revision="2")
    assert stream.closed


async def test_caller_cancellation_propagates_and_closes_the_response() -> None:
    entered = asyncio.Event()

    class Blocked(Stream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b""

    stream = Blocked(b"")
    async with httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    ) as client:
        task = asyncio.create_task(
            PublicVoiceCatalogue(client).get_revision(voice_id="voice-demo-1", revision="2")
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


@pytest.mark.parametrize(("key", "value"), [("voice_id", "voice-other-1"), ("revision", "3")])
async def test_a_revision_cannot_substitute_the_requested_identity(key: str, value: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer({**REVISION, key: value})

    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(handler).get_revision(voice_id="voice-demo-1", revision="2")


async def test_the_legacy_flattened_revision_is_not_a_public_revision() -> None:
    legacy = {k: v for k, v in REVISION.items() if k != "capabilities"}
    legacy |= {"languages": ["zh-CN"], "audio_formats": ["wav"]}

    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(lambda _: answer(legacy)).get_revision(
            voice_id="voice-demo-1", revision="2"
        )


@pytest.mark.parametrize(
    "value",
    [
        {"object": "other.list", "data": [SUMMARY], "has_more": False, "next_cursor": None},
        {
            "object": "nexus.list",
            "data": [SUMMARY],
            "has_more": False,
            "next_cursor": None,
            "private_endpoint": "synthetic-private-field",
        },
    ],
)
async def test_listing_does_not_ignore_noncanonical_fields_or_discriminators(value: Any) -> None:
    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(lambda _: answer(value)).list_summaries(
            workspace_id=WORKSPACE, language=None, limit=50, after=None
        )


@pytest.mark.parametrize("field", ["source", "validation", "capabilities", "resource_type"])
async def test_revision_requires_its_complete_public_provenance(field: str) -> None:
    value = deepcopy(REVISION)
    value.pop(field)
    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(lambda _: answer(value)).get_revision(voice_id="voice-demo-1", revision="2")


async def test_transport_failure_is_an_unavailable_catalogue() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connection failure")

    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(handler).get_revision(voice_id="voice-demo-1", revision="2")


async def test_the_total_deadline_closes_an_incomplete_response() -> None:
    class Blocked(Stream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.Event().wait()
            yield b""

    stream = Blocked(b"")
    async with httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    ) as client:
        with pytest.raises(VoiceCatalogueUnavailableError):
            await asyncio.wait_for(
                PublicVoiceCatalogue(client, deadline_seconds=0.01).get_revision(
                    voice_id="voice-demo-1", revision="2"
                ),
                0.3,
            )
    assert stream.closed


async def test_a_missing_revision_is_reported_as_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer({"error": {"code": "resource_not_found"}}, status=404)

    with pytest.raises(ResourceNotFoundError):
        await catalogue(handler).get_revision(voice_id="voice-demo-1", revision="9")


async def test_an_undeclared_listing_404_is_unavailable() -> None:
    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(lambda _: answer({}, status=404)).list_summaries(
            workspace_id=WORKSPACE, language=None, limit=50, after=None
        )


async def test_redirect_is_refused_even_when_the_injected_client_follows_redirects() -> None:
    reads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reads.append(request.url.path)
        return httpx.Response(302, headers={"location": "/synthetic-private-target"})

    async with httpx.AsyncClient(
        base_url=BASE, follow_redirects=True, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(VoiceCatalogueUnavailableError):
            await PublicVoiceCatalogue(client).list_summaries(
                workspace_id=WORKSPACE, language=None, limit=50, after=None
            )
    assert reads == ["/v1/nexus/voices"]


@pytest.mark.parametrize(
    ("status", "raw", "content_type"),
    [
        (503, b"{}", "application/json"),
        (200, b"<html>", "text/html"),
        (200, b"{not json", "application/json"),
        (200, b"\xff\xfe", "application/json"),
    ],
    ids=["upstream-503", "foreign-content-type", "broken-json", "undecodable-bytes"],
)
async def test_an_unusable_answer_is_an_unavailable_catalogue(
    status: int, raw: bytes, content_type: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(raw, status=status, content_type=content_type)

    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(handler).list_summaries(
            workspace_id=WORKSPACE, language=None, limit=50, after=None
        )


@pytest.mark.parametrize(
    "payload",
    [
        [SUMMARY],
        {"object": "nexus.list", "data": "not-a-list", "has_more": False, "next_cursor": None},
        {"object": "nexus.list", "data": [SUMMARY], "has_more": "no", "next_cursor": None},
        {"object": "nexus.list", "data": [SUMMARY], "has_more": True, "next_cursor": 7},
        {
            "object": "nexus.list",
            "data": [{**SUMMARY, "status": "draft"}],
            "has_more": False,
            "next_cursor": None,
        },
        {
            "object": "nexus.list",
            "data": [{**SUMMARY, "id": "Bad"}],
            "has_more": False,
            "next_cursor": None,
        },
        {
            "object": "nexus.list",
            "data": [{**SUMMARY, "revision": 2}],
            "has_more": False,
            "next_cursor": None,
        },
        {
            "object": "nexus.list",
            "data": [{**SUMMARY, "audio_formats": []}],
            "has_more": False,
            "next_cursor": None,
        },
    ],
    ids=[
        "not-an-object",
        "data-not-a-list",
        "has_more-not-a-bool",
        "cursor-not-a-string",
        "unknown-status",
        "invalid-identifier",
        "integer-revision",
        "empty-formats",
    ],
)
async def test_only_the_frozen_answer_survives_the_mapping(payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(payload)

    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(handler).list_summaries(
            workspace_id=WORKSPACE, language=None, limit=50, after=None
        )


async def test_a_contradictory_revision_is_unavailable_not_passed_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer({k: v for k, v in REVISION.items() if k != "model_binding"})

    with pytest.raises(VoiceCatalogueUnavailableError):
        await catalogue(handler).get_revision(voice_id="voice-demo-1", revision="2")


@pytest.mark.parametrize(
    ("base", "deadline"),
    [
        ("ftp://speech.internal:8080", 30),
        ("http://user:secret@speech.internal:8080", 30),
        ("http://speech.internal:8080?token=secret", 30),
        (BASE, 0),
        (BASE, 121),
        (BASE, float("inf")),
    ],
)
def test_the_configured_client_is_validated(base: str, deadline: float) -> None:
    client = httpx.AsyncClient(
        base_url=base, transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    with pytest.raises(ValueError):
        PublicVoiceCatalogue(client, deadline_seconds=deadline)
