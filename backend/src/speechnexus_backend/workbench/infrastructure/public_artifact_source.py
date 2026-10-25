"""Bounded artifact copies through public speech routes, using the generated validator."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, BinaryIO, cast
from urllib.parse import quote

import httpx
from speechnexus_contracts import ContractValidationError, parse

from speechnexus_backend.workbench.application.speech_sources import (
    ArtifactCopy,
    SpeechSourceError,
)

MAX_SAVED_ASSET_BYTES = 512 * 1024 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_SPOOL_BYTES = 1024 * 1024
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")


class PublicArtifactSource:
    """The composition root owns/configures the client and its approved base URL.

    Does not accept URLs or credentials from a workbench command, follow redirects,
    close the injected client, retry via an alternate service, or mutate source state.
    A private URL alone is not proof of the deployment's network egress qualification.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        now: Callable[[], datetime] | None = None,
        deadline_seconds: float = 300,
    ) -> None:
        if (
            client.base_url.scheme not in {"http", "https"}
            or not client.base_url.host
            or client.base_url.userinfo
            or client.base_url.query
            or client.base_url.fragment
            or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= 900
        ):
            raise ValueError("invalid configured artifact source")
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))
        self._deadline = deadline_seconds

    async def _metadata(self, path: str) -> dict[str, Any]:
        async with self._client.stream(
            "GET",
            path,
            follow_redirects=False,
            timeout=15,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as response:
            if response.status_code == 410:
                raise SpeechSourceError("source_expired")
            if response.status_code == 404:
                raise SpeechSourceError("source_invalid")
            if response.status_code != 200:
                raise SpeechSourceError("source_unavailable")
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise SpeechSourceError("source_unsupported")
            buffer = bytearray()
            async for chunk in response.aiter_raw():
                if len(chunk) > _MAX_METADATA_BYTES - len(buffer):
                    raise SpeechSourceError("source_invalid")
                buffer.extend(chunk)
            return dict(parse("Artifact", json.loads(bytes(buffer).decode("utf-8"))))

    async def _content(self, path: str, *, limit: int) -> tuple[BinaryIO, str, int]:
        # Ownership transfers to the caller, which closes it on every path.
        spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
        # The spool implements the binary stream surface MinIO and the zone consume;
        # typeshed models it generically, so pin the boundary type once here.
        body = cast("BinaryIO", spool)
        digest = hashlib.sha256()
        size = 0
        try:
            async with self._client.stream(
                "GET",
                path,
                follow_redirects=False,
                timeout=60,
                headers={"Accept": "application/octet-stream", "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code == 410:
                    raise SpeechSourceError("source_expired")
                if response.status_code == 404:
                    raise SpeechSourceError("source_invalid")
                if response.status_code == 206 or "content-range" in response.headers:
                    # A saved copy is the whole artifact: a partial body is never pinned.
                    raise SpeechSourceError("source_invalid")
                if response.status_code != 200:
                    raise SpeechSourceError("source_unavailable")
                if (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/octet-stream"
                    or response.headers.get("content-encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise SpeechSourceError("source_unsupported")
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > limit:
                        # Longer than the declared size: bounded, and never pinned.
                        raise SpeechSourceError("source_invalid")
                    digest.update(chunk)
                    spool.write(chunk)
        except BaseException:
            spool.close()
            raise
        return body, "sha256:" + digest.hexdigest(), size

    def _check_expiry(self, expires_at: str | None) -> None:
        if expires_at is not None and datetime.fromisoformat(expires_at) <= self._now():
            raise SpeechSourceError("source_expired")

    @staticmethod
    def _media_type(value: str) -> str:
        normalized = value.strip().lower()
        if _MEDIA_TYPE.fullmatch(normalized) is None:
            raise SpeechSourceError("source_unsupported")
        return normalized

    async def copy(self, *, artifact_id: str, receipt_id: str, workspace_id: str) -> ArtifactCopy:
        # Quote each path component; even invalid internal IDs cannot change routes.
        path = "/v1/nexus/artifacts/" + quote(artifact_id, safe="").replace(".", "%2E")
        try:
            async with asyncio.timeout(self._deadline):
                artifact = await self._metadata(path)
                if artifact["status"] == "expired":
                    raise SpeechSourceError("source_expired")
                if (
                    artifact["id"] != artifact_id
                    or artifact["workspace_id"] != workspace_id
                    or artifact.get("receipt_id") != receipt_id
                    or artifact["status"] != "ready"
                ):
                    raise SpeechSourceError("source_invalid")
                # Never wait for expiry to notice a source that already expired.
                self._check_expiry(artifact.get("expires_at"))
                media_type = self._media_type(str(artifact["media_type"]))
                expected = int(artifact["bytes"])
                if expected > MAX_SAVED_ASSET_BYTES:
                    raise SpeechSourceError("source_too_large")
                body, digest, size = await self._content(path + "/content", limit=expected)
                try:
                    if size != expected or not hmac.compare_digest(digest, artifact["sha256"]):
                        raise SpeechSourceError("source_invalid")
                    self._check_expiry(artifact.get("expires_at"))
                    body.seek(0)
                    return ArtifactCopy(
                        artifact_id=artifact["id"],
                        receipt_id=receipt_id,
                        media_type=media_type,
                        bytes=size,
                        sha256=artifact["sha256"],
                        body=body,
                    )
                except BaseException:
                    body.close()
                    raise
        except httpx.HTTPError, httpx.InvalidURL, TimeoutError:
            raise SpeechSourceError("source_unavailable") from None
        except ContractValidationError, ValueError:
            raise SpeechSourceError("source_invalid") from None
