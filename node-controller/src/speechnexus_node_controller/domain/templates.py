"""Approved runtime templates and the registry the controller resolves against.

The controller never accepts an arbitrary image, host mount, or shell command:
an intent is only admissible when its ``approved_template_id`` resolves to a
pinned template whose pool, memory architecture, and reservation bounds cover
the request. Everything the adapter is allowed to hand to the container runtime
— image, arguments, readiness rule, GPU request, read-only artifacts — is
declared here once, so an intent cannot widen it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from speechnexus_node_controller.domain.intents import (
    POOL_IDS,
    MemoryArchitecture,
    UnapprovedRuntimeTemplateError,
    require_digest,
    require_identifier,
)

_TEMPLATE_ID_PREFIX: Final = "template-"


class ReadinessKind(StrEnum):
    """How the node decides that an instance generation finished loading."""

    PROCESS_RUNNING = "process_running"
    TCP_PORT_ACCEPTING = "tcp_port_accepting"


@dataclass(frozen=True, slots=True)
class ReadinessRule:
    """Approved readiness probe for one template."""

    kind: ReadinessKind
    port: int | None = None

    def __post_init__(self) -> None:
        if self.kind is ReadinessKind.TCP_PORT_ACCEPTING:
            if self.port is None or not 1 <= self.port <= 65535:
                raise ValueError("tcp_port_accepting readiness requires a port between 1 and 65535")
        elif self.port is not None:
            raise ValueError("process_running readiness carries no port")


@dataclass(frozen=True, slots=True)
class ReadOnlyBind:
    """Approved read-only artifact directory handed to the container runtime."""

    source: str
    target: str

    def __post_init__(self) -> None:
        if not self.source.startswith("/") or not self.target.startswith("/"):
            raise ValueError("read-only binds use absolute container-visible paths")


@dataclass(frozen=True, slots=True)
class ApprovedRuntimeTemplate:
    """One pre-approved runtime template pinned to an immutable image digest."""

    template_id: str
    image_reference: str
    image_digest: str
    memory_architecture: MemoryArchitecture
    allowed_pool_ids: frozenset[str]
    max_peak_memory_bytes: int
    readiness: ReadinessRule = ReadinessRule(ReadinessKind.PROCESS_RUNNING)
    command: tuple[str, ...] | None = None
    gpu_device_count: int = 0
    read_only_binds: tuple[ReadOnlyBind, ...] = ()
    requires_warmup_before_ready: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.template_id, "template_id")
        if not self.template_id.startswith(_TEMPLATE_ID_PREFIX):
            raise ValueError(f"template_id must start with {_TEMPLATE_ID_PREFIX!r}")
        require_digest(self.image_digest, "image_digest")
        if not self.image_reference or " " in self.image_reference:
            raise ValueError("image_reference must be a single container image reference")
        if self.command is not None and not self.command:
            raise ValueError("a declared command must not be empty")
        if self.gpu_device_count < 0:
            raise ValueError("gpu_device_count must not be negative")
        if not self.allowed_pool_ids:
            raise ValueError("an approved template declares at least one pool")
        unknown_pools = self.allowed_pool_ids - POOL_IDS
        if unknown_pools:
            raise ValueError(f"approved template declares unknown pools {sorted(unknown_pools)}")
        if self.max_peak_memory_bytes <= 0:
            raise ValueError("max_peak_memory_bytes must be positive")


class ApprovedTemplateRegistry:
    """The only source of runnable templates for this node."""

    def __init__(self, templates: Iterable[ApprovedRuntimeTemplate]) -> None:
        self._templates: dict[str, ApprovedRuntimeTemplate] = {}
        for template in templates:
            if template.template_id in self._templates:
                raise ValueError(f"template {template.template_id} is declared twice")
            self._templates[template.template_id] = template

    def require(self, template_id: str) -> ApprovedRuntimeTemplate:
        """Resolve an approved template or refuse the intent."""
        template = self._templates.get(template_id)
        if template is None:
            raise UnapprovedRuntimeTemplateError(
                f"template {template_id} is not approved on this node"
            )
        return template

    def contains(self, template_id: str) -> bool:
        return template_id in self._templates

    @property
    def template_ids(self) -> frozenset[str]:
        return frozenset(self._templates)
