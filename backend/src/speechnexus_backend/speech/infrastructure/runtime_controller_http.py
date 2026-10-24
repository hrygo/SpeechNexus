"""Bounded, authenticated TLS to one configured private node; never retry a POST."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import secrets
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import quote

import httpx
from speechnexus_contracts.runtime_http import validate_request, validate_response

from speechnexus_backend.speech.domain.runtime_controller import (
    ControllerCallError,
    ControllerFailure,
    ControllerObservation,
)
from speechnexus_backend.speech.domain.runtime_intents import RuntimeIntent, identifier
from speechnexus_backend.speech.infrastructure.runtime_codec import prepare_request
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    prepare_observation,
)

_V4 = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
)
_V6 = ipaddress.ip_network("fc00::/7")
_SUBMIT = "submitRuntimeInstanceIntent"
_QUERY = "getRuntimeInstanceIntent"


@dataclass(frozen=True, slots=True)
class RuntimeControllerEndpoint:
    node_id: str
    address: str = field(repr=False)
    port: int
    bearer_token: str = field(repr=False)

    def __post_init__(self) -> None:
        identifier(self.node_id)
        try:
            if not isinstance(self.address, str) or "%" in self.address:
                raise ValueError
            address = ipaddress.ip_address(self.address)
            private = (
                any(address in network for network in _V4)
                if address.version == 4
                else address in _V6 or address == ipaddress.ip_address("::1")
            )
            if not private:
                raise ValueError
        except ValueError, TypeError:
            raise ValueError(
                "Controller endpoint requires a private or loopback IP literal"
            ) from None
        if (
            type(self.port) is not int
            or not 1 <= self.port <= 65535
            or not isinstance(self.bearer_token, str)
            or not 16 <= len(self.bearer_token) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in self.bearer_token)
        ):
            raise ValueError("Invalid configured controller endpoint")

    @property
    def base_url(self) -> str:
        address = str(ipaddress.ip_address(self.address))
        host = f"[{address}]" if ":" in address else address
        return f"https://{host}:{self.port}"


def _tls(context: ssl.SSLContext) -> None:
    if (
        context.protocol != ssl.PROTOCOL_TLS_CLIENT
        or context.verify_mode != ssl.CERT_REQUIRED
        or not context.check_hostname
        or context.minimum_version < ssl.TLSVersion.TLSv1_2
    ):
        raise ValueError("Controller TLS requires certificate and hostname verification")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate controller response field")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError("Nonfinite controller response value")


class HttpRuntimeController:
    def __init__(
        self,
        endpoint: RuntimeControllerEndpoint,
        *,
        timeout_seconds: float,
        ssl_context: ssl.SSLContext | None = None,
        max_in_flight: int = 4,
        max_waiting: int = 16,
        max_response_bytes: int = 65536,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 1800
            or type(max_in_flight) is not int
            or not 1 <= max_in_flight <= 16
            or type(max_waiting) is not int
            or not 1 <= max_waiting <= 64
            or type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= 65536
        ):
            raise ValueError("Controller calls require bounded deadlines, queues and responses")
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        _tls(context)
        self._context = context
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._max_bytes = max_response_bytes
        self._slots = asyncio.BoundedSemaphore(max_in_flight)
        self._waiting = 0
        self._max_waiting = max_waiting
        self._closed = False
        self._client = httpx.AsyncClient(
            verify=context,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=max_in_flight, max_keepalive_connections=max_in_flight
            ),
            transport=_transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        await self._client.aclose()

    async def submit(self, original: RuntimeIntent) -> ControllerObservation:
        return await self._call(original, submit=True)

    async def query(self, original: RuntimeIntent) -> ControllerObservation:
        return await self._call(original, submit=False)

    async def _call(self, original: RuntimeIntent, *, submit: bool) -> ControllerObservation:
        sent = False
        if self._closed:
            raise ControllerCallError(ControllerFailure.CLOSED, request_may_have_run=False)
        try:
            _tls(self._context)
            prepared = prepare_request(original)
            if original.binding.target_node_id != self._endpoint.node_id:
                raise ValueError
            operation = _SUBMIT if submit else _QUERY
            request_id = "runtime-" + secrets.token_hex(16)
            headers = {
                "Authorization": "Bearer " + self._endpoint.bearer_token,
                "X-Request-ID": request_id,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            path = {"node_id": self._endpoint.node_id}
            envelope: dict[str, Any] = {"path": path, "headers": headers}
            if submit:
                envelope["body"] = json.loads(prepared.document)
                headers["Content-Type"] = "application/json"
            else:
                path["intent_id"] = original.intent_id
            validate_request(operation, envelope)
        except ValueError, TypeError, KeyError, UnicodeError, RecursionError:
            raise ControllerCallError(
                ControllerFailure.INVALID_REQUEST, request_may_have_run=False
            ) from None
        if self._waiting >= self._max_waiting:
            raise ControllerCallError(ControllerFailure.BUSY, request_may_have_run=False)
        budget = self._timeout
        if submit:
            budget = min(budget, (original.deadline.at_us - time.time_ns() // 1000) / 1_000_000)
        if budget <= 0:
            raise ControllerCallError(
                ControllerFailure.DEADLINE_EXCEEDED, request_may_have_run=False
            )
        deadline = asyncio.get_running_loop().time() + budget
        suffix = f"/internal/v1/nodes/{quote(self._endpoint.node_id, safe='')}/instance-intents"
        if not submit:
            suffix += "/" + quote(original.intent_id, safe="")
        self._waiting += 1
        waiting = True
        try:
            async with asyncio.timeout_at(deadline):
                async with self._slots:
                    self._waiting -= 1
                    waiting = False
                    if self._closed:
                        raise ControllerCallError(
                            ControllerFailure.CLOSED, request_may_have_run=False
                        )
                    if asyncio.get_running_loop().time() >= deadline or (
                        submit and original.deadline.at_us <= time.time_ns() // 1000
                    ):
                        raise TimeoutError
                    sent = True  # Any error after this point is conservatively unresolved.
                    async with self._client.stream(
                        "POST" if submit else "GET",
                        self._endpoint.base_url + suffix,
                        headers=headers,
                        content=prepared.document.encode() if submit else None,
                        timeout=budget,
                    ) as response:
                        result = await self._response(
                            response, original, operation, request_id, submit=submit
                        )
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError
                    return result
        except TimeoutError, httpx.TimeoutException:
            raise ControllerCallError(
                ControllerFailure.DEADLINE_EXCEEDED, request_may_have_run=sent
            ) from None
        except httpx.HTTPError, OSError:
            raise ControllerCallError(
                ControllerFailure.UNAVAILABLE, request_may_have_run=sent
            ) from None
        except ValueError, TypeError, KeyError, UnicodeError, RecursionError:
            raise ControllerCallError(
                ControllerFailure.INVALID_RESPONSE, request_may_have_run=sent
            ) from None
        finally:
            if waiting:
                self._waiting -= 1

    async def _response(
        self,
        response: httpx.Response,
        original: RuntimeIntent,
        operation: str,
        request_id: str,
        *,
        submit: bool,
    ) -> ControllerObservation:
        headers = response.headers
        if (
            headers.get_list("x-request-id") != [request_id]
            or headers.get_list("content-encoding") not in ([], ["identity"])
            or len(headers.get_list("content-type")) != 1
        ):
            raise ValueError
        media = [part.strip().lower() for part in headers["content-type"].split(";")]
        if media[0] != "application/json" or any(
            part not in ("charset=utf-8", 'charset="utf-8"') for part in media[1:]
        ):
            raise ValueError
        lengths = headers.get_list("content-length")
        length = None
        if lengths:
            if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None:
                raise ValueError
            length = int(lengths[0])
            if not 0 < length <= self._max_bytes:
                raise ValueError
        data = bytearray()
        async for chunk in response.aiter_raw(chunk_size=4096):
            if len(data) + len(chunk) > self._max_bytes:
                raise ValueError
            data.extend(chunk)
        if length is not None and length != len(data):
            raise ValueError
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite
        )
        validate_response(
            operation, response.status_code, {"headers": dict(headers), "body": value}
        )
        if response.status_code != 200:
            code = value["error"]["code"]
            failure = (
                ControllerFailure.NOT_FOUND
                if not submit and response.status_code == 404 and code == "runtime_intent_not_found"
                else ControllerFailure.UNAVAILABLE
                if code == "runtime_controller_unavailable"
                else ControllerFailure.REJECTED
            )
            raise ControllerCallError(failure, request_may_have_run=True, controller_code=code)
        if not submit and "idempotent_replay" in value:
            raise ValueError
        prepared = prepare_observation(value)
        if not prepared.observation.matches(original):
            raise ValueError
        return ControllerObservation(prepared.observation, prepared.document)
