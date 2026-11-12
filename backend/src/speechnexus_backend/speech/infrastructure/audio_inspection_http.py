"""AssetInspector backed by the frozen internal inspection protocol.

The audio bytes are decoded by the separate, outbound-free ``asset-inspector``
deployment unit. This module is the only API-side caller: it reads the already
buffered upload once, sends exactly one request, revalidates the receipt against
its own digest, byte count, media type, sample clock and duration budget, and
maps every private outcome onto the public error vocabulary frozen in
``contracts/speech-openapi.yaml``.

Two failure families are kept strictly apart, because mislabelling one as the
other either deletes valid content or retries invalid content forever:

* a permanent content verdict (``invalid_media``, ``unsupported_media``,
  ``media_limit_exceeded``) raises ``AssetError`` with a ``422`` public code
  before any Asset row exists, so no metadata, object or event is written;
* an infrastructure outcome (``busy``, ``deadline_exceeded``, an unreachable or
  timing-out inspector) raises ``asset_inspection_busy``, which is the only
  retryable Asset ``503``;
* a configuration or protocol refusal (``bad_request``, ``unauthorized``,
  ``encoded_too_large``, an unparsable or invalid receipt) raises
  ``asset_inspection_unavailable``: retrying cannot fix a deployment that cannot
  speak this protocol.

No response body, header, token or input fragment is ever propagated: the
failure envelope's ``status`` string is the only private value that is read.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable
from typing import Any, BinaryIO, NoReturn

import httpx
from pydantic import SecretStr
from speechnexus_contracts.audio_inspection import parse

from speechnexus_backend.speech.application.assets import TextAssetInspector
from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetPolicy,
    AssetPurpose,
    Inspection,
    MediaFacts,
)

INSPECTION_PATH = "/internal/v1/inspection/audio"
# The frozen recipe accepts this one media type. `audio-inspection.schema.json`
# also allows `audio/pcm`, which carries a caller-supplied sample clock the
# public upload contract does not have; accepting it would authorize a receipt
# the API cannot cross-check.
RECIPE_MEDIA_TYPE = "audio/wav"

# `status` is the closed envelope's only stable discriminator; prose, HTTP reason
# phrases and vendor messages are never parsed.
_STATUS_CODES: dict[str, str] = {
    # Permanent content verdicts. Retrying the same bytes cannot succeed and no
    # Asset row is written. `unsupported_media` belongs here because the public
    # upload path bounds only the byte count and forwards the caller's encoding
    # unchanged: a format this deployment does not accept is the caller's file,
    # so an outage code would tell them to retry forever.
    "invalid_media": "invalid_asset_audio",
    "unsupported_media": "invalid_asset_audio",
    "media_limit_exceeded": "asset_audio_too_long",
    # Temporary infrastructure outcomes. Inspection is side-effect free, so a new
    # request with the same body may succeed later.
    "busy": "asset_inspection_busy",
    "unavailable": "asset_inspection_busy",
    "deadline_exceeded": "asset_inspection_busy",
    # Configuration and protocol refusals. These are deployment faults, not
    # content faults and not retryable: a byte cap or token that this process
    # cannot satisfy will not change by asking again. `encoded_too_large` means
    # the inspector's own cap is stricter than this process's, because the
    # ingress already refuses anything over `asset_max_bytes + 16 KiB`.
    "bad_request": "asset_inspection_unavailable",
    "unauthorized": "asset_inspection_unavailable",
    "encoded_too_large": "asset_inspection_unavailable",
}

# The API's own upload concurrency is bounded at four, and the protocol uses one
# short-lived connection per in-flight inspection with keep-alive allowed.
_MAX_CONNECTIONS = 4


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate inspection field")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> NoReturn:
    raise ValueError("Invalid inspection number")


def _service_unavailable() -> NoReturn:
    """Raise the non-retryable refusal for a configuration or protocol fault."""
    raise AssetError("asset_inspection_unavailable")


class HttpAudioAssetInspector:
    """Decode ``asr_input`` audio through the private inspection endpoint.

    The port is unchanged: one bounded ``BinaryIO``, one policy, one result. This
    implementation owns an HTTP connection pool and no local process, so it
    performs no retry, no fallback to the text inspector, and writes nothing.
    """

    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        timeout_seconds: float,
        wall_seconds: float,
        max_duration_ms: int,
        max_input_bytes: int,
        max_receipt_bytes: int,
        cancelled: Callable[[], bool] = lambda: False,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(token, SecretStr) or not token.get_secret_value():
            raise ValueError("Configure a non-empty asset-inspection token")
        if (
            type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 60
            or type(wall_seconds) not in (int, float)
            or not 0 < wall_seconds <= 60
            or type(max_duration_ms) is not int
            or not 1 <= max_duration_ms <= 86_400_000
            or type(max_input_bytes) is not int
            or not 1 <= max_input_bytes <= 1024**3
            or type(max_receipt_bytes) is not int
            or not 1 <= max_receipt_bytes <= 65_536
        ):
            raise ValueError("Invalid private audio inspection limits")
        self._url = base_url.rstrip("/") + INSPECTION_PATH
        self._token = token
        self._wall_ms = max(1, round(wall_seconds * 1000))
        self._max_duration_ms = max_duration_ms
        self._max_input_bytes = max_input_bytes
        self._max_receipt_bytes = max_receipt_bytes
        self._cancelled = cancelled
        self._closed = False
        self._text = TextAssetInspector()
        self._client = httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout_seconds)),
            limits=httpx.Limits(
                max_connections=_MAX_CONNECTIONS,
                max_keepalive_connections=_MAX_CONNECTIONS,
            ),
            transport=_transport,
        )

    def inspect(self, purpose: AssetPurpose, body: BinaryIO, policy: AssetPolicy) -> Inspection:
        if purpose == "text_input":
            return self._text.inspect(purpose, body, policy)
        if purpose != "asr_input":
            return Inspection(available=False)
        if self._closed:
            # The runtime is being dismantled. No local work exists to finish and
            # the replacement process will serve a new request.
            raise AssetError("asset_inspection_busy")
        try:
            payload = self._buffered(body, policy)
        finally:
            with contextlib.suppress(OSError, ValueError):
                body.seek(0)
        if payload is None or self._cancelled():
            return Inspection(available=False)
        content, digest = payload
        status_code, raw = self._exchange(content)
        if self._cancelled():
            return Inspection(available=False)
        if status_code != 200:
            raise AssetError(self._failure_code(raw))
        return self._receipt(raw, digest, len(content))

    def close(self) -> bool:
        """Close the pool and refuse new inspections.

        Returns ``True`` because no local process needs a stop confirmation: the
        inspector container is owned by the deployment, and the API never claims
        to have stopped it. Closing an unused client is safe, which the
        composition root relies on for its ``open()`` failure path.
        """
        if not self._closed:
            self._closed = True
            self._client.close()
        return True

    def _buffered(self, body: BinaryIO, policy: AssetPolicy) -> tuple[bytes, str] | None:
        """Read the prepared body once, hashing the exact bytes that are sent."""
        body.seek(0)
        limit = min(policy.max_bytes, self._max_input_bytes)
        buffer = bytearray()
        digest = hashlib.sha256()
        while True:
            if self._cancelled():
                return None
            chunk = body.read(65_536)
            if type(chunk) is not bytes:
                return None
            if not chunk:
                break
            if len(buffer) + len(chunk) > limit:
                # The upload path already rejected anything over the policy cap,
                # so reaching this bound means the two API-side limits disagree.
                # That is a deployment configuration fault, not a content verdict.
                _service_unavailable()
            buffer.extend(chunk)
            digest.update(chunk)
        if not buffer:
            return None
        return bytes(buffer), digest.hexdigest()

    def _exchange(self, content: bytes) -> tuple[int, bytes]:
        request = self._client.build_request(
            "POST",
            self._url,
            content=content,
            headers={
                # The body is the raw encoded audio; a JSON or base64 wrapper would
                # add size without adding authenticity.
                "Content-Type": "application/octet-stream",
                "Authorization": "Bearer " + self._token.get_secret_value(),
                "X-Nexus-Deadline-Ms": str(self._wall_ms),
                "X-Nexus-Max-Duration-Ms": str(self._max_duration_ms),
            },
        )
        try:
            response = self._client.send(request, stream=True)
        except httpx.HTTPError, OSError:
            # Unreachable, reset or timed out. One attempt only; the caller issues
            # a new request. Inspection writes no state, so this is retryable.
            raise AssetError("asset_inspection_busy") from None
        try:
            return response.status_code, self._read_bounded(response)
        except httpx.HTTPError, OSError:
            raise AssetError("asset_inspection_busy") from None
        finally:
            response.close()

    def _read_bounded(self, response: httpx.Response) -> bytes:
        # A response over the bound is refused whole; parsing a truncated receipt
        # could accept facts the inspector never proved.
        if response.is_stream_consumed:
            raw = response.content
            if len(raw) > self._max_receipt_bytes:
                _service_unavailable()
            return raw
        buffer = bytearray()
        for chunk in response.iter_raw():
            if len(buffer) + len(chunk) > self._max_receipt_bytes:
                _service_unavailable()
            buffer.extend(chunk)
        return bytes(buffer)

    def _failure_code(self, raw: bytes) -> str:
        try:
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_fields,
                parse_constant=_invalid_constant,
            )
        except UnicodeError, ValueError, RecursionError:
            return "asset_inspection_unavailable"
        if not isinstance(document, dict) or set(document) != {"schema_version", "status"}:
            return "asset_inspection_unavailable"
        status = document["status"]
        if type(document["schema_version"]) is not int or type(status) is not str:
            return "asset_inspection_unavailable"
        return _STATUS_CODES.get(status, "asset_inspection_unavailable")

    def _receipt(self, raw: bytes, digest: str, encoded_bytes: int) -> Inspection:
        """Revalidate the receipt against locally computed facts.

        Nothing self-reported is trusted until it matches the bytes this process
        read, and a receipt that fails is API-side unavailability rather than a
        content rejection: the bytes may be perfectly valid.
        """
        try:
            receipt = parse(
                "AudioInspectionSuccess",
                json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_unique_fields,
                    parse_constant=_invalid_constant,
                ),
            )
        except UnicodeError, ValueError, RecursionError:
            _service_unavailable()
        counters = (
            receipt["schema_version"],
            receipt["encoded_bytes"],
            receipt["source_samples"],
            receipt["sample_rate_hz"],
            receipt["channels"],
            receipt["duration_ms"],
        )
        if (
            any(type(value) is not int for value in counters)
            or receipt["sha256"] != "sha256:" + digest
            or receipt["encoded_bytes"] != encoded_bytes
            or receipt["media_type"] != RECIPE_MEDIA_TYPE
            or receipt["duration_ms"] > self._max_duration_ms
            or receipt["duration_ms"]
            != (receipt["source_samples"] * 1000 + receipt["sample_rate_hz"] - 1)
            // receipt["sample_rate_hz"]
        ):
            _service_unavailable()
        if self._cancelled():
            return Inspection(available=False)
        return Inspection(
            facts=MediaFacts(
                receipt["media_type"],
                receipt["duration_ms"],
                receipt["sample_rate_hz"],
                receipt["channels"],
            )
        )
