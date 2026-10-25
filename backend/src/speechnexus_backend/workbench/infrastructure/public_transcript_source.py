"""Bounded reads through public speech routes, using Team A's generated validator."""

import asyncio
import hashlib
import hmac
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from speechnexus_contracts import ContractValidationError, parse

from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError

_MAX_METADATA_BYTES = 64 * 1024
_MAX_TEXT_BYTES = 4_000_000
_MAX_TEXT_CHARACTERS = 1_000_000


def _transcript_format(media_type: str) -> str:
    parts = [part.strip().lower() for part in media_type.split(";")]
    if parts[0] not in {"text/plain", "application/json"} or any(
        parameter not in {"charset=utf-8", 'charset="utf-8"', "charset=utf8", 'charset="utf8"'}
        for parameter in parts[1:]
    ):
        raise TranscriptSourceError("source_unsupported")
    return parts[0]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate transcription field")
    return result


class PublicTranscriptSource:
    """The composition root owns/configures the client and its approved base URL.

    Does not accept URLs or credentials from a workbench command, follow redirects,
    close the injected client, or retry via an alternate service. A private URL
    alone is not proof of the deployment's network egress qualification.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        now: Callable[[], datetime] | None = None,
        deadline_seconds: float = 30,
    ) -> None:
        if (
            client.base_url.scheme not in {"http", "https"}
            or not client.base_url.host
            or client.base_url.userinfo
            or client.base_url.query
            or client.base_url.fragment
            or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= 120
        ):
            raise ValueError("invalid configured transcript source")
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))
        self._deadline = deadline_seconds

    async def _body(self, path: str, *, limit: int, media_type: str) -> tuple[bytes, str]:
        async with self._client.stream(
            "GET",
            path,
            follow_redirects=False,
            timeout=15,
            headers={"Accept": media_type, "Accept-Encoding": "identity"},
        ) as response:
            if response.status_code == 410:
                raise TranscriptSourceError("source_expired")
            if response.status_code == 206 or "content-range" in response.headers:
                raise TranscriptSourceError("source_invalid")
            if response.status_code != 200:
                raise TranscriptSourceError("source_unavailable")
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != media_type
                or response.headers.get("content-encoding", "identity").strip().lower()
                != "identity"
            ):
                raise TranscriptSourceError("source_unsupported")
            buffer = bytearray()
            digest = hashlib.sha256()
            async for chunk in response.aiter_raw():
                if len(chunk) > limit - len(buffer):
                    raise TranscriptSourceError("source_too_large")
                buffer.extend(chunk)
                digest.update(chunk)
            return bytes(buffer), "sha256:" + digest.hexdigest()

    def _check_expiry(self, expires_at: str | None) -> None:
        if expires_at is not None and datetime.fromisoformat(expires_at) <= self._now():
            raise TranscriptSourceError("source_expired")

    async def read(self, *, artifact_id: str, receipt_id: str, workspace_id: str) -> str:
        # Quote each path component; even invalid internal IDs cannot change routes.
        path = "/v1/nexus/artifacts/" + quote(artifact_id, safe="").replace(".", "%2E")
        try:
            async with asyncio.timeout(self._deadline):
                raw, _ = await self._body(
                    path, limit=_MAX_METADATA_BYTES, media_type="application/json"
                )
                artifact = parse("Artifact", json.loads(raw.decode("utf-8")))
                if artifact["status"] == "expired":
                    raise TranscriptSourceError("source_expired")
                if (
                    artifact["id"] != artifact_id
                    or artifact["workspace_id"] != workspace_id
                    or artifact.get("receipt_id") != receipt_id
                    or artifact["kind"] != "transcript"
                    or artifact["status"] != "ready"
                ):
                    raise TranscriptSourceError("source_invalid")
                media_type = _transcript_format(artifact["media_type"])
                self._check_expiry(artifact.get("expires_at"))
                expected_bytes = int(artifact["bytes"])
                if expected_bytes > _MAX_TEXT_BYTES:
                    raise TranscriptSourceError("source_too_large")
                content, digest = await self._body(
                    path + "/content", limit=expected_bytes, media_type="application/octet-stream"
                )
                if len(content) != expected_bytes or not hmac.compare_digest(
                    digest, artifact["sha256"]
                ):
                    raise TranscriptSourceError("source_invalid")
                text = content.decode("utf-8", errors="strict")
                if media_type == "application/json":
                    # File ASR stages the public Transcription shape. Validate that
                    # generated boundary, then preserve its exact text, not JSON markup.
                    text = parse(
                        "Transcription", json.loads(text, object_pairs_hook=_unique_object)
                    )["text"]
                    text.encode("utf-8", errors="strict")
                # Valid UTF-8 may contain NUL, which cannot be represented in a PG
                # text column. Reject the source before opening a final transaction;
                # never silently remove characters from a sealed original.
                if "\x00" in text:
                    raise TranscriptSourceError("source_invalid")
                if len(text) > _MAX_TEXT_CHARACTERS:
                    raise TranscriptSourceError("source_too_large")
                self._check_expiry(artifact.get("expires_at"))
                return text
        except httpx.HTTPError, httpx.InvalidURL, TimeoutError:
            raise TranscriptSourceError("source_unavailable") from None
        except ContractValidationError, ValueError, RecursionError:
            raise TranscriptSourceError("source_invalid") from None
