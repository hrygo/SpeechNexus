"""In-memory Docker engine double; it never contacts a real daemon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from speechnexus_node_controller.infrastructure.docker_api import (
    ContainerState,
    DockerApiError,
    DockerUnavailableError,
    ImageIdentity,
)


@dataclass
class FakeContainer:
    """One container the double simulates."""

    container_id: str
    name: str
    image_reference: str
    command: tuple[str, ...] | None
    labels: dict[str, str]
    host_config: dict[str, Any]
    environment: tuple[str, ...] = field(default=(), repr=False)
    image_id: str | None = None
    status: str = "created"
    running: bool = False
    exit_code: int | None = None
    addresses: tuple[str, ...] = ("172.18.0.2",)
    memory_peak_bytes: int | None = None


@dataclass
class FakeDockerEngine:
    """Deterministic engine double with scripted failures."""

    images: dict[str, str] = field(default_factory=dict)
    addresses: tuple[str, ...] = ("172.18.0.2",)
    memory_peak_bytes: int | None = 128 * 1024 * 1024
    exit_code: int = 0
    fail_wait: bool = False
    fail_stop: bool = False
    fail_remove: bool = False
    fail_remove_and_keep: bool = False
    daemon_unavailable: bool = False

    def __post_init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}
        self.created: list[FakeContainer] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self._sequence = 0

    def ping(self) -> None:
        self._require_available()

    def inspect_image(self, reference: str) -> ImageIdentity | None:
        self._require_available()
        image_id = self.images.get(reference)
        if image_id is None:
            return None
        return ImageIdentity(image_id=image_id, repository_digests=(f"{reference}@{image_id}",))

    def find_container(self, labels: dict[str, str]) -> ContainerState | None:
        self._require_available()
        for container in self.containers.values():
            if all(container.labels.get(key) == value for key, value in labels.items()):
                return self._state(container)
        return None

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
        self._require_available()
        if image_reference not in self.images:
            raise DockerApiError(404, "no such image")
        self._sequence += 1
        container_id = f"container-{self._sequence}"
        container = FakeContainer(
            container_id=container_id,
            name=name,
            image_reference=image_reference,
            command=command,
            labels=dict(labels),
            host_config=host_config,
            environment=environment,
            image_id=self.images[image_reference],
            addresses=self.addresses,
            memory_peak_bytes=self.memory_peak_bytes,
        )
        self.containers[container_id] = container
        self.created.append(container)
        return container_id

    def start_container(self, container_id: str) -> None:
        self._require_available()
        container = self._require(container_id)
        container.status = "running"
        container.running = True
        container.exit_code = None

    def inspect_container(self, container_id: str) -> ContainerState | None:
        self._require_available()
        container = self.containers.get(container_id)
        return None if container is None else self._state(container)

    def wait_container(self, container_id: str, *, timeout_seconds: float) -> int:
        self._require_available()
        container = self._require(container_id)
        if self.fail_wait:
            raise DockerApiError(500, "wait timed out")
        container.status = "exited"
        container.running = False
        container.exit_code = self.exit_code
        del timeout_seconds
        return self.exit_code

    def stop_container(self, container_id: str, *, timeout_seconds: float) -> None:
        self._require_available()
        self._require(container_id)
        self.stopped.append(container_id)
        del timeout_seconds
        if self.fail_stop:
            raise DockerApiError(500, "stop timed out")

    def remove_container(self, container_id: str) -> None:
        self._require_available()
        container = self._require(container_id)
        if self.fail_remove:
            raise DockerApiError(409, "container is in use")
        if self.fail_remove_and_keep:
            self.removed.append(container_id)
            return
        self.removed.append(container_id)
        del self.containers[container.container_id]

    def _require_available(self) -> None:
        if self.daemon_unavailable:
            raise DockerUnavailableError("daemon socket did not answer")

    def _require(self, container_id: str) -> FakeContainer:
        container = self.containers.get(container_id)
        if container is None:
            raise DockerApiError(404, "no such container")
        return container

    @staticmethod
    def _state(container: FakeContainer) -> ContainerState:
        return ContainerState(
            container_id=container.container_id,
            status=container.status,
            running=container.running,
            exit_code=container.exit_code,
            labels=dict(container.labels),
            network_addresses=container.addresses,
            memory_peak_bytes=container.memory_peak_bytes,
            memory_limit_bytes=container.host_config.get("Memory"),
            memory_swap_limit_bytes=container.host_config.get("MemorySwap"),
            network_mode=container.host_config.get("NetworkMode"),
            command=container.command,
            environment=container.environment,
            image_id=container.image_id,
        )
