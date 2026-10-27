"""Authenticated backend client for the gateway's execution-control protocol."""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx

from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchProtocolError,
    DispatchUnavailableError,
    ExecutionSubmission,
    GatewayReply,
    ResultReceipt,
    identifier,
)

_MAX_RESPONSE_BYTES = 65536
_ERROR_CODES = frozenset(
    {
        "stale_fencing_token",
        "gateway_barrier_fenced",
        "submit_intent_conflict",
        "instance_generation_mismatch",
        "instance_draining",
        "lease_not_active",
        "policy_or_resource_disabled",
        "engine_transport_unknown",
        "cancellation_unconfirmed",
        "execution_cleanup_pending",
        "invalid_execution_request",
        "component_credential_invalid",
        "control_store_unavailable",
        "control_outcome_unknown",
        "control_reconciliation_required",
        "control_state_conflict",
        "control_execution_not_found",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionGatewayEndpoint:
    base_url: str = field(repr=False)
    bearer_token: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.base_url)
        except ValueError:
            raise ValueError("Invalid execution gateway endpoint") from None
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Execution gateway requires an origin without credentials or path")
        if parsed.scheme == "http" and parsed.hostname not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise ValueError("Plain HTTP is allowed only for a loopback test endpoint")
        if (
            not isinstance(self.bearer_token, str)
            or not 16 <= len(self.bearer_token) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in self.bearer_token)
        ):
            raise ValueError("Invalid execution gateway credential")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DispatchProtocolError("Gateway response repeated a key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise DispatchProtocolError("Gateway response used a nonfinite number")


def _json(response: httpx.Response) -> Mapping[str, Any]:
    content = response.content
    if not content or len(content) > _MAX_RESPONSE_BYTES:
        raise DispatchProtocolError("Gateway response has an invalid size")
    try:
        value = json.loads(
            content.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except DispatchProtocolError:
        raise
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise DispatchProtocolError("Gateway response is not strict JSON") from None
    if not isinstance(value, dict):
        raise DispatchProtocolError("Gateway response is not an object")
    return value


def _safe_error(document: Mapping[str, Any]) -> str | None:
    error = document.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) and code in _ERROR_CODES else None


class HttpExecutionGateway:
    def __init__(
        self,
        endpoint: ExecutionGatewayEndpoint,
        *,
        timeout_seconds: float,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("Execution gateway calls require a bounded deadline")
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            transport=_transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, *, body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self._endpoint.bearer_token,
            "X-Request-ID": "jobs-" + secrets.token_hex(16),
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _send(
        self, method: str, path: str, *, payload: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                path,
                headers=self._headers(body=payload is not None),
                json=payload,
            )
        except httpx.HTTPError:
            raise DispatchUnavailableError("Gateway transport outcome is unknown") from None

    @staticmethod
    def _reply(response: httpx.Response, *, allow_not_found: bool) -> GatewayReply:
        if response.status_code == 200:
            return GatewayReply("status", _json(response))
        if response.status_code == 404 and allow_not_found:
            return GatewayReply("not_found")
        if response.status_code in (401, 409, 422):
            document = _json(response)
            return GatewayReply(
                "rejected", error_code=_safe_error(document) or "execution_rejected"
            )
        if response.status_code >= 500 or response.status_code in (408, 429):
            return GatewayReply("unknown", error_code="outcome_unknown")
        document = _json(response)
        return GatewayReply("rejected", error_code=_safe_error(document) or "execution_rejected")

    async def query(self, submit_intent_id: str) -> GatewayReply:
        identifier(submit_intent_id)
        response = await self._send(
            "GET", "/internal/v1/executions/" + quote(submit_intent_id, safe="")
        )
        return self._reply(response, allow_not_found=True)

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        identifier(submission.submit_intent_id)
        response = await self._send(
            "POST", "/internal/v1/executions/submit", payload=submission.body
        )
        return self._reply(response, allow_not_found=False)

    async def result_receipt(self, submit_intent_id: str, *, audio: bool) -> ResultReceipt:
        identifier(submit_intent_id)
        suffix = "/audio-staging-result" if audio else "/staging-result"
        response = await self._send(
            "GET",
            "/internal/v1/executions/" + quote(submit_intent_id, safe="") + suffix,
        )
        if response.status_code == 404:
            return ResultReceipt("not_found")
        if response.status_code in (401, 409, 422):
            return ResultReceipt("rejected", error_code=_safe_error(_json(response)))
        if response.status_code >= 500 or response.status_code in (408, 429):
            return ResultReceipt("unknown", error_code="outcome_unknown")
        if response.status_code != 200:
            return ResultReceipt(
                "rejected", error_code=_safe_error(_json(response)) or "execution_rejected"
            )
        document = _json(response)
        if document.get("submit_intent_id") != submit_intent_id:
            raise DispatchProtocolError("Gateway receipt belongs to another execution")
        staging = document.get("staging_result")
        if staging is None:
            return ResultReceipt("unknown", error_code="result_not_ready")
        if not isinstance(staging, dict):
            raise DispatchProtocolError("Gateway staging result is not an object")
        if staging.get("request_digest") != document.get("request_digest"):
            raise DispatchProtocolError("Gateway receipt disagrees with its request digest")
        return ResultReceipt("status", staging)


__all__ = ["ExecutionGatewayEndpoint", "HttpExecutionGateway"]
