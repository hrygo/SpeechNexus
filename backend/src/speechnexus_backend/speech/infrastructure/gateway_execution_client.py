"""Transport-only client for the execution gateway control plane.

This module does not admit work. It does not choose a binding or workspace, derive a
lease/attempt/stage identity, allocate fencing tokens, build commands, dispatch schedules
or release budgets. A caller must already hold the exact admitted facts and pass a
complete ``SubmitExecutionRequest``; the gateway validates those facts against its own
authoritative tables.

The submit POST is never retried here. A connection failure or timeout after the request
may have reached the gateway is outcome-unknown, so the caller must query the original
submit intent instead of creating a second execution. Read-only calls (``query``,
``staging_result`` and ``audio_staging_result``) use the explicit retry policy.

Credentials and object bytes never appear in exception messages. The private result
reader accepts only a configured bucket and a version-pinned object, then verifies the
declared length and SHA-256 before returning bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

import httpx
from minio import Minio
from minio.error import S3Error
from pydantic import SecretStr
from speechnexus_contracts.execution import parse
from speechnexus_contracts.execution_models import (
    ArtifactStorageReference,
    ExecutionAudioStagingResultResponse,
    ExecutionStagingResultResponse,
    ExecutionStatusResponse,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)
from urllib3.response import BaseHTTPResponse

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.assets_minio import _FixedOriginHTTP

_SUBMIT_PATH = "/internal/v1/executions/submit"
_EXECUTION_PATH = "/internal/v1/executions/"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_RESULT_BYTES = 4 * 1024 * 1024

GatewayFailureKind = Literal["connection", "timeout", "rejected", "server", "invalid_response"]


class GatewayExecutionError(Exception):
    """Base class for a classified gateway call failure; safe to log."""

    kind: GatewayFailureKind

    def __init__(
        self,
        message: str,
        *,
        request_may_have_run: bool,
        status_code: int | None = None,
        gateway_code: str | None = None,
        recovery: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.request_may_have_run = request_may_have_run
        self.status_code = status_code
        self.gateway_code = gateway_code
        self.recovery = recovery
        self.retryable = retryable


class GatewayConnectionError(GatewayExecutionError):
    """The TCP/TLS connection failed; the gateway did not answer."""

    kind = "connection"


class GatewayTimeoutError(GatewayExecutionError):
    """The call exceeded its configured deadline."""

    kind = "timeout"


class GatewayRejectedError(GatewayExecutionError):
    """A 4xx answer: the request is invalid, unauthenticated or conflicts with authority."""

    kind = "rejected"


class GatewayServerError(GatewayExecutionError):
    """A 5xx answer: the gateway or its control store could not complete the call."""

    kind = "server"


class GatewayResponseError(GatewayExecutionError):
    """The answer was not a valid, bounded execution-control response."""

    kind = "invalid_response"


class GatewayResultObjectError(Exception):
    """A private result object was unavailable, out of scope or failed verification."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"execution result object: {code}")


def _nonfinite(value: str) -> Any:
    raise ValueError("Nonfinite gateway response value")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate gateway response field")
        result[key] = value
    return result


def _json_object(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite)
    if not isinstance(value, dict):
        raise ValueError("Gateway response must be a JSON object")
    return cast(dict[str, Any], value)


def _base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Configure an explicit execution-gateway base URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Configure an explicit execution-gateway base URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid execution identifier")
    return value


class GatewayExecutionClient:
    """One authenticated HTTP client for submit, query and private result reads.

    ``timeout_seconds``, ``max_retries`` and ``backoff_seconds`` are explicit
    constructor inputs; they are not hidden defaults inside individual calls. The retry
    budget applies only to idempotent reads. Every submit attempt is single-shot.
    """

    def __init__(
        self,
        *,
        base_url: str,
        credential: SecretStr,
        timeout_seconds: float,
        max_retries: int,
        backoff_seconds: float,
        max_response_bytes: int = 1_048_576,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(credential, SecretStr) or not credential.get_secret_value():
            raise ValueError("Configure a non-empty execution-gateway component credential")
        if (
            type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 600
            or type(max_retries) is not int
            or not 0 <= max_retries <= 5
            or type(backoff_seconds) not in (int, float)
            or not 0 <= backoff_seconds <= 10
            or type(max_response_bytes) is not int
            or not 1024 <= max_response_bytes <= 4_194_304
        ):
            raise ValueError("Configure bounded execution-gateway timeouts and retries")
        self._base_url = _base_url(base_url)
        self._credential = credential
        self._timeout = float(timeout_seconds)
        self._max_retries = max_retries
        self._backoff = float(backoff_seconds)
        self._max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(self._timeout),
            transport=_transport,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> GatewayExecutionClient:
        """Build from deployment settings; credentials stay in ``SecretStr``."""
        if (
            settings.execution_gateway_base_url is None
            or settings.execution_gateway_component_credential is None
        ):
            raise ValueError("Execution gateway client is not configured")
        return cls(
            base_url=str(settings.execution_gateway_base_url),
            credential=settings.execution_gateway_component_credential,
            timeout_seconds=settings.execution_gateway_timeout_seconds,
            max_retries=settings.execution_gateway_max_retries,
            backoff_seconds=settings.execution_gateway_backoff_seconds,
            max_response_bytes=settings.execution_gateway_max_response_bytes,
            _transport=_transport,
        )

    async def __aenter__(self) -> GatewayExecutionClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def submit(self, request: SubmitExecutionRequest) -> SubmitExecutionResponse:
        """Persist one already-admitted execution; never retried by this client."""
        validated = parse("SubmitExecutionRequest", request)
        body = json.dumps(
            validated, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        payload = await self._send("POST", _SUBMIT_PATH, body=body, idempotent=False)
        try:
            return parse("SubmitExecutionResponse", payload)
        except ValueError:
            raise self._invalid_response() from None

    async def query(self, submit_intent_id: str) -> ExecutionStatusResponse:
        """Read authoritative execution state by the original submit identity."""
        path = _EXECUTION_PATH + quote(_identifier(submit_intent_id), safe="")
        payload = await self._send("GET", path, body=None, idempotent=True)
        try:
            return parse("ExecutionStatusResponse", payload)
        except ValueError:
            raise self._invalid_response() from None

    async def staging_result(self, submit_intent_id: str) -> ExecutionStagingResultResponse:
        """Read the private JSON result receipt without publishing or executing."""
        path = _EXECUTION_PATH + quote(_identifier(submit_intent_id), safe="") + "/staging-result"
        payload = await self._send("GET", path, body=None, idempotent=True)
        try:
            return parse("ExecutionStagingResultResponse", payload)
        except ValueError:
            raise self._invalid_response() from None

    async def audio_staging_result(
        self, submit_intent_id: str
    ) -> ExecutionAudioStagingResultResponse:
        """Read the private audio result receipt without publishing or executing."""
        path = (
            _EXECUTION_PATH
            + quote(_identifier(submit_intent_id), safe="")
            + "/audio-staging-result"
        )
        payload = await self._send("GET", path, body=None, idempotent=True)
        try:
            return parse("ExecutionAudioStagingResultResponse", payload)
        except ValueError:
            raise self._invalid_response() from None

    @staticmethod
    def _invalid_response() -> GatewayResponseError:
        return GatewayResponseError(
            "the execution gateway returned a response outside the frozen schema",
            request_may_have_run=True,
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        idempotent: bool,
    ) -> dict[str, Any]:
        attempts = self._max_retries + 1 if idempotent else 1
        for attempt in range(attempts):
            try:
                return await self._attempt(method, path, body)
            except (GatewayConnectionError, GatewayTimeoutError, GatewayServerError) as error:
                if attempt + 1 >= attempts or not self._retryable(error):
                    raise
                await asyncio.sleep(self._backoff * (2**attempt))
        raise AssertionError("unreachable retry loop")

    @staticmethod
    def _retryable(error: GatewayExecutionError) -> bool:
        if isinstance(error, GatewayServerError):
            return error.retryable is not False
        return True

    async def _attempt(self, method: str, path: str, body: bytes | None) -> dict[str, Any]:
        headers = {
            "Authorization": "Bearer " + self._credential.get_secret_value(),
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Request-ID": uuid.uuid4().hex,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        sent = False
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    method, self._base_url + path, headers=headers, content=body
                ) as response:
                    sent = True
                    raw = await self._read_bounded(response)
                    status = response.status_code
                    if 200 <= status < 300:
                        if status != 200:
                            raise GatewayResponseError(
                                "the execution gateway returned an unexpected success status",
                                request_may_have_run=True,
                                status_code=status,
                            )
                        self._check_success_headers(response, raw)
                        return _json_object(raw)
                    details = self._error_details(raw) if status >= 400 else None
                    if 400 <= status < 500:
                        code, recovery, retryable = details or (None, None, False)
                        raise GatewayRejectedError(
                            "the execution gateway rejected the request",
                            request_may_have_run=True,
                            status_code=status,
                            gateway_code=code,
                            recovery=recovery,
                            retryable=retryable,
                        )
                    if 500 <= status < 600:
                        code, recovery, retryable = details or (None, None, True)
                        raise GatewayServerError(
                            "the execution gateway could not complete the request",
                            request_may_have_run=True,
                            status_code=status,
                            gateway_code=code,
                            recovery=recovery,
                            retryable=retryable,
                        )
                    raise GatewayResponseError(
                        "the execution gateway returned an unexpected status",
                        request_may_have_run=True,
                        status_code=status,
                    )
        except GatewayExecutionError:
            raise
        except TimeoutError:
            raise GatewayTimeoutError(
                "the execution gateway did not answer before the deadline",
                request_may_have_run=sent or method == "POST",
            ) from None
        except httpx.ConnectTimeout:
            raise GatewayTimeoutError(
                "the execution gateway connection timed out", request_may_have_run=False
            ) from None
        except httpx.ConnectError:
            raise GatewayConnectionError(
                "the execution gateway is unreachable", request_may_have_run=False
            ) from None
        except httpx.TimeoutException:
            raise GatewayTimeoutError(
                "the execution gateway did not answer before the deadline",
                request_may_have_run=True,
            ) from None
        except httpx.HTTPError:
            raise GatewayConnectionError(
                "the execution gateway connection failed", request_may_have_run=True
            ) from None
        except ValueError, UnicodeError, RecursionError:
            raise GatewayResponseError(
                "the execution gateway returned an invalid response",
                request_may_have_run=sent,
            ) from None

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        if response.is_stream_consumed:
            # ``MockTransport`` (and an already-buffered response) delivers bytes that
            # were read before this client saw the response. Real transport streams.
            preloaded = response.content
            if len(preloaded) > self._max_response_bytes:
                raise GatewayResponseError(
                    "the execution gateway response exceeded its bound",
                    request_may_have_run=True,
                    status_code=response.status_code,
                )
            return preloaded
        buffer = bytearray()
        async for chunk in response.aiter_raw():
            if len(buffer) + len(chunk) > self._max_response_bytes:
                raise GatewayResponseError(
                    "the execution gateway response exceeded its bound",
                    request_may_have_run=True,
                    status_code=response.status_code,
                )
            buffer.extend(chunk)
        return bytes(buffer)

    def _check_success_headers(self, response: httpx.Response, raw: bytes) -> None:
        encodings = response.headers.get_list("content-encoding")
        if encodings not in ([], ["identity"]):
            raise GatewayResponseError(
                "the execution gateway response used an unexpected encoding",
                request_may_have_run=True,
                status_code=response.status_code,
            )
        content_types = response.headers.get_list("content-type")
        if len(content_types) != 1:
            raise GatewayResponseError(
                "the execution gateway response had no single content type",
                request_may_have_run=True,
                status_code=response.status_code,
            )
        media = [part.strip().lower() for part in content_types[0].split(";")]
        if media[0] != "application/json" or any(
            part not in ("charset=utf-8", 'charset="utf-8"') for part in media[1:]
        ):
            raise GatewayResponseError(
                "the execution gateway response was not JSON",
                request_may_have_run=True,
                status_code=response.status_code,
            )
        lengths = response.headers.get_list("content-length")
        if lengths and (
            len(lengths) != 1
            or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None
            or int(lengths[0]) != len(raw)
        ):
            raise GatewayResponseError(
                "the execution gateway response length did not match its body",
                request_may_have_run=True,
                status_code=response.status_code,
            )

    @staticmethod
    def _error_details(raw: bytes) -> tuple[str | None, str | None, bool | None] | None:
        try:
            payload = _json_object(raw)
            parsed = parse("ExecutionControlErrorResponse", payload)
        except ValueError, UnicodeError, RecursionError:
            return None
        error = parsed["error"]
        return error["code"], error.get("recovery"), error["retryable"]


class MinioExecutionResultObjects:
    """Read one version-pinned private execution result from the configured bucket.

    The gateway receipt is authoritative for the object reference. This reader still
    refuses a reference outside the configured bucket, enforces the declared byte bound,
    requires the exact storage version and verifies the declared SHA-256. It returns no
    partial bytes on any failure.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        max_bytes: int = _MAX_RESULT_BYTES,
        _client: Any | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not bucket
            or not access_key
            or not secret_key
            or type(max_bytes) is not int
            or not 11 <= max_bytes <= _MAX_RESULT_BYTES
        ):
            raise ValueError("Configure an explicit result endpoint, bucket and credentials")
        self._bucket = bucket
        self._max_bytes = max_bytes
        self._http: _FixedOriginHTTP | None = None
        if _client is not None:
            self._client = _client
        else:
            try:
                self._http = _FixedOriginHTTP(endpoint)
            except Exception:
                raise ValueError("Invalid execution result object endpoint") from None
            self._client = Minio(
                parsed.netloc,
                access_key=access_key,
                secret_key=secret_key,
                secure=parsed.scheme == "https",
                region="us-east-1",
                http_client=self._http,
            )

    def close(self) -> None:
        if self._http is not None:
            self._http.clear()

    def read(self, reference: ArtifactStorageReference, storage_version: str) -> bytes:
        """Return verified bytes for the exact reference and storage version."""
        if reference["storage_bucket"] != self._bucket:
            raise GatewayResultObjectError("result_object_scope_rejected")
        length = reference["byte_length"]
        if type(length) is not int or not 11 <= length <= self._max_bytes:
            raise GatewayResultObjectError("result_object_limit_rejected")
        expected_digest = reference["content_sha256"]
        if _SHA256.fullmatch(expected_digest) is None:
            raise GatewayResultObjectError("result_object_digest_rejected")
        if not isinstance(storage_version, str) or not storage_version or storage_version == "null":
            raise GatewayResultObjectError("result_object_version_rejected")
        try:
            response = cast(
                BaseHTTPResponse,
                self._client.get_object(
                    self._bucket,
                    reference["object_key"],
                    offset=0,
                    length=length,
                    version_id=storage_version,
                ),
            )
        except S3Error as error:
            if error.code in ("NoSuchKey", "NoSuchObject", "NoSuchVersion"):
                raise GatewayResultObjectError("result_object_unavailable") from None
            raise GatewayResultObjectError("result_object_storage_unavailable") from None
        except Exception:
            raise GatewayResultObjectError("result_object_storage_unavailable") from None
        try:
            if (
                response.status != 206
                or response.headers.get("x-amz-version-id") != storage_version
                or response.headers.get("content-range") != f"bytes 0-{length - 1}/{length}"
                or response.headers.get("content-length") != str(length)
            ):
                raise GatewayResultObjectError("result_object_mismatch")
            body = response.read(length + 1)
        except GatewayResultObjectError:
            raise
        except Exception:
            raise GatewayResultObjectError("result_object_storage_unavailable") from None
        finally:
            response.close()
            response.release_conn()
        if (
            not isinstance(body, bytes)
            or len(body) != length
            or hashlib.sha256(body).hexdigest() != expected_digest.removeprefix("sha256:")
        ):
            raise GatewayResultObjectError("result_object_mismatch")
        return body

    def read_json(
        self, reference: ArtifactStorageReference, storage_version: str
    ) -> dict[str, Any]:
        """Return one verified JSON object; the caller owns its business interpretation."""
        body = self.read(reference, storage_version)
        try:
            return _json_object(body)
        except ValueError, UnicodeError, RecursionError:
            raise GatewayResultObjectError("result_object_invalid_json") from None


__all__ = [
    "GatewayConnectionError",
    "GatewayExecutionClient",
    "GatewayExecutionError",
    "GatewayRejectedError",
    "GatewayResponseError",
    "GatewayResultObjectError",
    "GatewayServerError",
    "GatewayTimeoutError",
    "MinioExecutionResultObjects",
]
