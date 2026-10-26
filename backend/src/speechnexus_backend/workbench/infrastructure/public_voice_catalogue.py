"""Reads the speech base's published voice catalogue over its public protocol.

The base owns voice identity, design, cloning, validation, publication and retirement, and
this adapter never designs, publishes, retires or promotes anything. The base's published
values are mapped structurally into the workbench's own validated values: a malformed,
contradictory or oversized answer is reported as an unavailable catalogue rather than being
passed on, and the domain constructor stays the single place that decides what is sane.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.application.voices import VoiceCatalogueUnavailableError
from speechnexus_backend.workbench.domain.voices import (
    VoiceCataloguePage,
    VoiceRevision,
    VoiceSummary,
)

_MAX_BODY_BYTES = 256 * 1024
_VOICES_PATH = "/v1/nexus/voices"


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceCatalogueUnavailableError
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate catalogue field")
    return result


def _text(node: dict[str, Any], key: str) -> str:
    value = node.get(key)
    if type(value) is not str:
        raise VoiceCatalogueUnavailableError
    return value


def _strings(node: dict[str, Any], key: str) -> tuple[str, ...]:
    value = node.get(key)
    if type(value) is not list or any(type(item) is not str for item in value):
        raise VoiceCatalogueUnavailableError
    return tuple(value)


def _timestamp(node: dict[str, Any], key: str) -> datetime:
    try:
        value = datetime.fromisoformat(_text(node, key).replace("Z", "+00:00"))
    except ValueError:
        raise VoiceCatalogueUnavailableError from None
    if value.tzinfo is None:
        raise VoiceCatalogueUnavailableError
    return value


class PublicVoiceCatalogue:
    """The composition root owns/configures the client and its approved base URL.

    Does not accept URLs or credentials from a workbench command, follow redirects, close
    the injected client, retry via an alternate service, or write anything upstream. A
    private URL alone is not proof of the deployment's network egress qualification.
    """

    def __init__(self, client: httpx.AsyncClient, *, deadline_seconds: float = 30) -> None:
        if (
            client.base_url.scheme not in {"http", "https"}
            or not client.base_url.host
            or client.base_url.userinfo
            or client.base_url.query
            or client.base_url.fragment
            or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= 120
        ):
            raise ValueError("invalid configured voice catalogue")
        self._client = client
        self._deadline = deadline_seconds

    async def _body(self, operation: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            async with (
                asyncio.timeout(self._deadline),
                self._client.stream(
                    "GET",
                    path,
                    params=params or None,
                    follow_redirects=False,
                    timeout=15,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response,
            ):
                if response.status_code == 404 and operation == "getVoiceRevision":
                    raise ResourceNotFoundError
                if response.status_code != 200:
                    raise VoiceCatalogueUnavailableError
                if (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    raise VoiceCatalogueUnavailableError
                buffer = bytearray()
                async for chunk in response.aiter_raw():
                    if len(buffer) + len(chunk) > _MAX_BODY_BYTES:
                        raise VoiceCatalogueUnavailableError
                    buffer.extend(chunk)
                try:
                    value = json.loads(
                        bytes(buffer).decode("utf-8"), object_pairs_hook=_unique_object
                    )
                    validate_response(
                        operation,
                        200,
                        {
                            "headers": dict(response.headers),
                            "body": value,
                        },
                    )
                except UnicodeDecodeError, ValueError:
                    raise VoiceCatalogueUnavailableError from None
                return _object(value)
        except httpx.RequestError, TimeoutError:
            raise VoiceCatalogueUnavailableError from None

    def _summary(self, item: Any) -> VoiceSummary:
        node = _object(item)
        try:
            return VoiceSummary(
                voice_id=_text(node, "id"),
                revision=_text(node, "revision"),
                display_name=_text(node, "display_name"),
                source_type=_text(node, "source_type"),
                status=_text(node, "status"),
                languages=_strings(node, "languages"),
                audio_formats=_strings(node, "audio_formats"),
            )
        except ValueError:
            # The base answered something the frozen workbench values refuse to carry.
            raise VoiceCatalogueUnavailableError from None

    async def list_summaries(
        self, *, workspace_id: str, language: str | None, limit: int, after: str | None
    ) -> VoiceCataloguePage:
        params: dict[str, Any] = {"workspace_id": workspace_id, "limit": limit}
        if language is not None:
            params["language"] = language
        if after is not None:
            # The workbench calls it `after`; the base's own query name is `cursor`.
            params["cursor"] = after
        value = await self._body("listVoices", _VOICES_PATH, params)
        data = value.get("data")
        has_more = value.get("has_more")
        cursor = value.get("next_cursor")
        if type(data) is not list or type(has_more) is not bool:
            raise VoiceCatalogueUnavailableError
        if cursor is not None and type(cursor) is not str:
            raise VoiceCatalogueUnavailableError
        try:
            return VoiceCataloguePage(
                items=tuple(self._summary(item) for item in data),
                has_more=has_more,
                next_cursor=cursor,
            )
        except ValueError:
            raise VoiceCatalogueUnavailableError from None

    async def get_revision(self, *, voice_id: str, revision: str) -> VoiceRevision:
        path = f"{_VOICES_PATH}/{quote(voice_id, safe='')}/revisions/{quote(revision, safe='')}"
        node = await self._body("getVoiceRevision", path, {})
        if node["voice_id"] != voice_id or node["revision"] != revision:
            raise VoiceCatalogueUnavailableError
        retired = node.get("retired_at")
        disabled = node.get("disabled_reason")
        try:
            binding = _object(node.get("model_binding"))
            capabilities = _object(node.get("capabilities"))
            return VoiceRevision(
                voice_id=_text(node, "voice_id"),
                revision=_text(node, "revision"),
                display_name=_text(node, "display_name"),
                status=_text(node, "status"),
                languages=_strings(capabilities, "languages"),
                audio_formats=_strings(capabilities, "audio_formats"),
                model_binding_id=_text(binding, "id"),
                model_binding_revision=_text(binding, "revision"),
                published_at=_timestamp(node, "published_at"),
                retired_at=None if retired is None else _timestamp(node, "retired_at"),
                disabled_reason=None if disabled is None else _text(node, "disabled_reason"),
            )
        except ValueError:
            raise VoiceCatalogueUnavailableError from None
