"""Minimal Docker Engine API client for the restricted node controller.

Only the operations the controller needs are exposed, and every call is fixed
by the caller: this module never accepts a shell string, an arbitrary bind, or
an image that an approved template did not already pin. The controller talks to
the local daemon over its unix socket because that socket is the only interface
that can start and remove the approved engine containers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class DockerUnavailableError(Exception):
    """The daemon socket did not answer."""


class DockerApiError(Exception):
    """The daemon answered with an error status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Docker API answered {status_code}: {message}")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ImageIdentity:
    """Pinned identity of a local image."""

    image_id: str
    repository_digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContainerState:
    """The subset of container facts the controller reasons about."""

    container_id: str
    status: str
    running: bool
    exit_code: int | None
    labels: dict[str, str]
    network_addresses: tuple[str, ...]
    memory_peak_bytes: int | None
    memory_limit_bytes: int | None = None
    memory_swap_limit_bytes: int | None = None
    network_mode: str | None = None
    command: tuple[str, ...] | None = field(default=None, repr=False)
    environment: tuple[str, ...] = field(default=(), repr=False)
    image_id: str | None = None


class DockerEngineApi(Protocol):
    """The daemon surface the node runtime adapter depends on."""

    def ping(self) -> None: ...

    def inspect_image(self, reference: str) -> ImageIdentity | None: ...

    def find_container(self, labels: dict[str, str]) -> ContainerState | None: ...

    def create_container(
        self,
        *,
        name: str,
        image_reference: str,
        command: tuple[str, ...] | None,
        labels: dict[str, str],
        host_config: dict[str, Any],
        environment: tuple[str, ...] = (),
    ) -> str: ...

    def start_container(self, container_id: str) -> None: ...

    def inspect_container(self, container_id: str) -> ContainerState | None: ...

    def wait_container(self, container_id: str, *, timeout_seconds: float) -> int: ...

    def stop_container(self, container_id: str, *, timeout_seconds: float) -> None: ...

    def remove_container(self, container_id: str) -> None: ...


class DockerApi:
    """Synchronous engine API client bound to one daemon socket."""

    def __init__(
        self,
        *,
        socket_path: str = "/var/run/docker.sock",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path),
            base_url="http://docker-engine",
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DockerApi:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def ping(self) -> None:
        self._send("GET", "/_ping")

    def inspect_image(self, reference: str) -> ImageIdentity | None:
        payload = self._send_optional_json("GET", f"/images/{reference}/json")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise DockerApiError(200, "image inspect returned no object")
        image_id = payload.get("Id")
        if not isinstance(image_id, str):
            raise DockerApiError(200, "image inspect returned no image id")
        digests = payload.get("RepoDigests") or []
        return ImageIdentity(
            image_id=image_id,
            repository_digests=tuple(digest for digest in digests if isinstance(digest, str)),
        )

    def find_container(self, labels: dict[str, str]) -> ContainerState | None:
        for container_id in self.list_container_ids(labels):
            return self.inspect_container(container_id)
        return None

    def list_container_ids(self, labels: dict[str, str]) -> tuple[str, ...]:
        """List containers carrying every given label; read-only."""
        filters = {"label": [f"{key}={value}" for key, value in sorted(labels.items())]}
        payload = self._send_json(
            "GET", "/containers/json", params={"all": "1", "filters": json.dumps(filters)}
        )
        if not isinstance(payload, list):
            raise DockerApiError(200, "container list returned no array")
        identifiers = []
        for entry in payload:
            if not isinstance(entry, dict):
                raise DockerApiError(200, "container list returned a non-object entry")
            container_id = entry.get("Id")
            if isinstance(container_id, str):
                identifiers.append(container_id)
        return tuple(identifiers)

    def create_container(
        self,
        *,
        name: str,
        image_reference: str,
        command: tuple[str, ...] | None,
        labels: dict[str, str],
        host_config: dict[str, Any],
        environment: tuple[str, ...] = (),
    ) -> str:
        body: dict[str, Any] = {
            "Image": image_reference,
            "Labels": labels,
            "HostConfig": host_config,
            "NetworkingConfig": {},
        }
        if command is not None:
            body["Cmd"] = list(command)
        if environment:
            body["Env"] = list(environment)
        payload = self._send_json("POST", "/containers/create", params={"name": name}, json=body)
        if not isinstance(payload, dict):
            raise DockerApiError(201, "container create returned no object")
        container_id = payload.get("Id")
        if not isinstance(container_id, str):
            raise DockerApiError(201, "container create returned no id")
        return container_id

    def start_container(self, container_id: str) -> None:
        self._send("POST", f"/containers/{container_id}/start")

    def inspect_container(self, container_id: str) -> ContainerState | None:
        payload = self._send_optional_json("GET", f"/containers/{container_id}/json")
        if payload is None:
            return None
        state = payload.get("State") or {}
        config = payload.get("Config") or {}
        labels = config.get("Labels") or {}
        host_config = payload.get("HostConfig") or {}
        statistics = self._send_optional_json(
            "GET", f"/containers/{container_id}/stats", params={"stream": "false"}
        )
        return ContainerState(
            container_id=container_id,
            status=str(state.get("Status", "unknown")),
            running=bool(state.get("Running", False)),
            exit_code=state.get("ExitCode") if isinstance(state.get("ExitCode"), int) else None,
            labels={str(key): str(value) for key, value in labels.items()},
            network_addresses=_network_addresses(payload),
            memory_peak_bytes=_memory_peak(statistics),
            memory_limit_bytes=host_config.get("Memory"),
            memory_swap_limit_bytes=host_config.get("MemorySwap"),
            network_mode=host_config.get("NetworkMode"),
            command=(
                tuple(config["Cmd"])
                if isinstance(config.get("Cmd"), list)
                and all(isinstance(item, str) for item in config["Cmd"])
                else None
            ),
            environment=(
                tuple(config["Env"])
                if isinstance(config.get("Env"), list)
                and all(isinstance(item, str) for item in config["Env"])
                else ()
            ),
            image_id=payload.get("Image"),
        )

    def wait_container(self, container_id: str, *, timeout_seconds: float) -> int:
        payload = self._send_json(
            "POST",
            f"/containers/{container_id}/wait",
            params={"condition": "not-running"},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise DockerApiError(200, "container wait returned no object")
        status_code = payload.get("StatusCode")
        if not isinstance(status_code, int):
            raise DockerApiError(200, "container wait returned no exit code")
        return status_code

    def stop_container(self, container_id: str, *, timeout_seconds: float) -> None:
        self._send(
            "POST",
            f"/containers/{container_id}/stop",
            params={"t": str(max(0, round(timeout_seconds)))},
            timeout_seconds=timeout_seconds + 10.0,
        )

    def remove_container(self, container_id: str) -> None:
        self._send("DELETE", f"/containers/{container_id}", params={"force": "1", "v": "1"})

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        """Send one request and raise for any error status."""
        timeout = httpx.USE_CLIENT_DEFAULT if timeout_seconds is None else timeout_seconds
        try:
            response = self._client.request(method, path, params=params, json=json, timeout=timeout)
        except httpx.HTTPError as error:
            raise DockerUnavailableError(
                f"docker daemon request failed: {type(error).__name__}"
            ) from error
        if response.status_code >= 400:
            raise DockerApiError(response.status_code, _error_message(response))
        return response

    def _send_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Send one request and decode its JSON body, if any."""
        response = self._send(
            method, path, params=params, json=json, timeout_seconds=timeout_seconds
        )
        if not response.content:
            return None
        return response.json()

    def _send_optional_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Send one read-only request, mapping a missing resource to ``None``."""
        try:
            return self._send_json(method, path, params=params)
        except DockerApiError as error:
            if error.status_code == 404:
                return None
            raise


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return str(payload["message"])[:200]
    return response.text[:200]


def _network_addresses(payload: dict[str, Any]) -> tuple[str, ...]:
    networks = (payload.get("NetworkSettings") or {}).get("Networks") or {}
    addresses = []
    for network in networks.values():
        address = network.get("IPAddress") if isinstance(network, dict) else None
        if isinstance(address, str) and address:
            addresses.append(address)
    return tuple(addresses)


def _memory_peak(statistics: Any) -> int | None:
    if not isinstance(statistics, dict):
        return None
    memory = statistics.get("memory_stats")
    if not isinstance(memory, dict):
        return None
    for key in ("max_usage", "usage"):
        value = memory.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None
