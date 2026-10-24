"""Read checkpoint-backed node facts over fixed TLS; no control requests or budget writes."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import secrets
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import BoundedSemaphore
from typing import Any, cast
from urllib.parse import quote

import httpx
from speechnexus_contracts.runtime_http import validate_request, validate_response

from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.fencing import POOLS
from speechnexus_gateway.domain.recovery import InstanceTerminationEvidence
from speechnexus_gateway.domain.submission import (
    StopEvidenceKind,
    SubmissionIntent,
    contract_instant_text,
)

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_OPERATION = "getVerifiedInstanceStop"
_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",
        "::1/128",
    )
)


def _tls(context: ssl.SSLContext) -> None:
    if (
        context.protocol != ssl.PROTOCOL_TLS_CLIENT
        or context.verify_mode != ssl.CERT_REQUIRED
        or not context.check_hostname
        or context.minimum_version < ssl.TLSVersion.TLSv1_2
    ):
        raise ValueError("node stop queries require verified TLS and IP identity")


@dataclass(frozen=True, slots=True)
class NodeStopEndpoint:
    node_id: str
    address: str = field(repr=False)
    port: int
    bearer_token: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.address)
            if "%" in self.address or not any(address in network for network in _NETWORKS):
                raise ValueError
        except ValueError, TypeError:
            raise ValueError("node stop queries require a configured private IP literal") from None
        if (
            not isinstance(self.node_id, str)
            or not _IDENTIFIER.fullmatch(self.node_id)
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
            or not isinstance(self.bearer_token, str)
            or not 16 <= len(self.bearer_token) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in self.bearer_token)
        ):
            raise ValueError("invalid node stop endpoint configuration")

    @property
    def base_url(self) -> str:
        address = str(ipaddress.ip_address(self.address))
        return f"https://{'[' + address + ']' if ':' in address else address}:{self.port}"


@dataclass(frozen=True, slots=True, repr=False)
class NodeStopBinding:
    node_id: str
    gateway_id: str
    pool_id: str
    binding_revision_id: str
    approved_template_id: str
    manifest_id: str
    config_digest: str

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
                for value in (
                    self.node_id,
                    self.gateway_id,
                    self.binding_revision_id,
                    self.approved_template_id,
                    self.manifest_id,
                )
            )
            or self.pool_id not in POOLS
            or not isinstance(self.config_digest, str)
            or not _DIGEST.fullmatch(self.config_digest)
        ):
            raise ValueError("invalid immutable node stop binding")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate stop response field")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError("nonfinite stop response")


def _instant(value: str) -> datetime:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("a stop observation requires a timezone")
    return at.astimezone(UTC)


class NodeStopEvidenceSource:
    """Synchronous recovery port: call from a worker thread, never an event-loop thread."""

    def __init__(
        self,
        endpoint: NodeStopEndpoint,
        *,
        bindings: tuple[NodeStopBinding, ...],
        ssl_context: ssl.SSLContext,
        timeout_seconds: float = 3,
        max_in_flight: int = 4,
        max_response_bytes: int = 65536,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _tls(ssl_context)
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
            or type(max_in_flight) is not int
            or not 1 <= max_in_flight <= 16
            or type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= 65536
            or not 1 <= len(bindings) <= 512
            or any(binding.node_id != endpoint.node_id for binding in bindings)
        ):
            raise ValueError("node stop queries require bounded, node-scoped configuration")
        mapped = {(binding.pool_id, binding.binding_revision_id): binding for binding in bindings}
        if len(mapped) != len(bindings):
            raise ValueError("node stop bindings must not have ambiguous identities")
        self._endpoint = endpoint
        self._bindings = mapped
        self._context = ssl_context
        self._timeout = timeout_seconds
        self._max_bytes = max_response_bytes
        self._slots = BoundedSemaphore(max_in_flight)
        self._now = now
        self._transport = _transport

    def termination_evidence(self, intent: SubmissionIntent) -> InstanceTerminationEvidence | None:
        identity = intent.identity
        binding = self._bindings.get((identity.pool_id, identity.binding_revision_id))
        if (
            binding is None
            or identity.host_node_id != binding.node_id
            or identity.gateway_id != binding.gateway_id
            or not self._slots.acquire(blocking=False)
        ):
            return None
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError("run synchronous execution recovery in a worker thread")
            return asyncio.run(self._query(intent, binding))
        except (
            httpx.HTTPError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            RecursionError,
            OverflowError,
        ):
            return None
        finally:
            self._slots.release()

    async def _query(
        self, intent: SubmissionIntent, binding: NodeStopBinding
    ) -> InstanceTerminationEvidence | None:
        _tls(self._context)  # A supplied SSLContext remains mutable.
        identity = intent.identity
        request_id = secrets.token_hex(16)
        headers = {
            "Authorization": "Bearer " + self._endpoint.bearer_token,
            "X-Request-ID": request_id,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        path = {
            "node_id": self._endpoint.node_id,
            "instance_id": identity.instance_id,
            "instance_generation": identity.instance_generation,
        }
        validate_request(_OPERATION, {"path": path, "headers": headers})
        suffix = (
            f"/internal/v1/nodes/{quote(self._endpoint.node_id, safe='')}/instances/"
            f"{quote(identity.instance_id, safe='')}/generations/"
            f"{identity.instance_generation}/verified-stop"
        )
        async with asyncio.timeout(self._timeout):
            async with (
                httpx.AsyncClient(
                    verify=self._context,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self._timeout,
                    transport=self._transport,
                ) as client,
                client.stream("GET", self._endpoint.base_url + suffix, headers=headers) as response,
            ):
                value = await self._response(response, request_id)
        if response.status_code != 200:
            return None
        return self._evidence(intent, binding, value)

    async def _response(self, response: httpx.Response, request_id: str) -> dict[str, Any]:
        headers = response.headers
        if (
            headers.get_list("x-request-id") != [request_id]
            or headers.get_list("content-encoding") not in ([], ["identity"])
            or len(headers.get_list("content-type")) != 1
        ):
            raise ValueError("invalid stop response headers")
        media = [part.strip().lower() for part in headers["content-type"].split(";")]
        if media[0] != "application/json" or any(
            part not in ("charset=utf-8", 'charset="utf-8"') for part in media[1:]
        ):
            raise ValueError("invalid stop response media")
        lengths = headers.get_list("content-length")
        length = None
        if lengths:
            if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None:
                raise ValueError("invalid stop response size")
            length = int(lengths[0])
            if not 0 < length <= self._max_bytes:
                raise ValueError("stop response exceeds its bound")
        data = bytearray()
        async for chunk in response.aiter_raw(chunk_size=4096):
            if len(data) + len(chunk) > self._max_bytes:
                raise ValueError("stop response exceeds its bound")
            data.extend(chunk)
        if length is not None and length != len(data):
            raise ValueError("incomplete stop response")
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite
        )
        validate_response(
            _OPERATION, response.status_code, {"headers": dict(headers), "body": value}
        )
        return cast(dict[str, Any], value)

    def _evidence(
        self, intent: SubmissionIntent, binding: NodeStopBinding, value: dict[str, Any]
    ) -> InstanceTerminationEvidence:
        identity = intent.identity
        expected = {
            "target_node_id": binding.node_id,
            "gateway_id": binding.gateway_id,
            "pool_id": binding.pool_id,
            "binding_revision_id": binding.binding_revision_id,
            "approved_template_id": binding.approved_template_id,
            "manifest_id": binding.manifest_id,
            "config_digest": binding.config_digest,
            "instance_id": identity.instance_id,
            "instance_generation": identity.instance_generation,
        }
        if any(value[name] != expected_value for name, expected_value in expected.items()):
            raise ValueError("unrelated stop identity")
        if value["fencing_token"] < identity.fencing_token:
            raise ValueError("the same-pool stop token does not cover this execution")
        proof = value["stop_evidence"]
        observed = _instant(proof["verified_at"])
        if not (
            intent.recorded_at <= observed <= self._now()
            and _instant(value["created_at"]) <= observed <= _instant(value["updated_at"])
        ) or (intent.dispatched_at is not None and observed < intent.dispatched_at):
            raise ValueError("the stop time does not cover this execution")
        # Normalize instants before hashing so equivalent UTC presentations are stable.
        for name in ("created_at", "updated_at"):
            value[name] = contract_instant_text(_instant(value[name]))
        proof["verified_at"] = contract_instant_text(observed)
        source_id = command_digest([binding.node_id, value["intent_id"]]).removeprefix("sha256:")
        return InstanceTerminationEvidence(
            instance_id=identity.instance_id,
            instance_generation=identity.instance_generation,
            evidence_id="stop-node-" + source_id,
            evidence_kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
            evidence_digest=command_digest(
                ["checkpoint-backed-node-stop/v1", cast(JsonValue, value)]
            ),
            observed_at=observed,
            process_exited=proof["process_or_container_exited"],
            resources_released=proof["resources_released_confirmed"],
        )
