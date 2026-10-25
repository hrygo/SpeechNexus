"""Approved runtime templates and the registry the controller resolves against.

The controller never accepts an arbitrary image, host mount, or shell command:
an intent is only admissible when its ``approved_template_id`` resolves to a
pinned template whose pool, memory architecture, and reservation bounds cover
the request. Everything the adapter is allowed to hand to the container runtime
— image, arguments, readiness rule, GPU request, read-only artifacts — is
declared here once, so an intent cannot widen it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
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
MIN_CONTAINER_MEMORY_BYTES: Final = 6 * 1024**2
_HEALTH_PATH: Final = re.compile(r"/[A-Za-z0-9_.~/-]*")
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


class ReadinessKind(StrEnum):
    """How the node decides that an instance generation finished loading."""

    PROCESS_RUNNING = "process_running"
    TCP_PORT_ACCEPTING = "tcp_port_accepting"
    HTTP_STATUS_OK = "http_status_ok"
    HTTP_WARMUP_OK = "http_warmup_ok"


class EngineAuthKind(StrEnum):
    NONE = "none"
    VLLM_API_KEY_V1 = "vllm_api_key_v1"


@dataclass(frozen=True, slots=True)
class WarmupRequest:
    """One approved warmup call that must succeed before an instance may serve.

    Transport readiness only proves that the process listens. A template that
    declares warmup stays at peak memory and reports ``warming_up`` until the
    same request, with a hash-verified body, has completed for every declared
    attempt. The controller never interprets the response body; judging the
    result belongs to the engine adapter that owns the protocol.
    """

    path: str
    content_type: str
    body_path: str = field(repr=False)
    body_sha256: str
    attempts: int = 2
    timeout_seconds: float = 60.0
    max_response_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if (
            len(self.path) > 256
            or _HEALTH_PATH.fullmatch(self.path) is None
            or "//" in self.path
            or any(part in {".", ".."} for part in self.path.split("/"))
        ):
            raise ValueError("warmup requires a fixed absolute path")
        if (
            not self.content_type
            or len(self.content_type) > 128
            or "/" not in self.content_type
            or any(not 32 <= ord(char) < 127 for char in self.content_type)
        ):
            raise ValueError("warmup content_type must be a single printable media type")
        if (
            not self.body_path.startswith("/")
            or len(self.body_path) > 1024
            or ".." in self.body_path.split("/")
            or any(not 32 <= ord(char) < 127 for char in self.body_path)
        ):
            raise ValueError("warmup body must be a fixed absolute file path")
        if _DIGEST.fullmatch(self.body_sha256) is None:
            raise ValueError("warmup body requires a sha256 digest")
        if type(self.attempts) is not int or not 1 <= self.attempts <= 4:
            raise ValueError("warmup attempts must be between 1 and 4")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 300:
            raise ValueError("warmup timeout must be positive and at most 300 seconds")
        if type(self.max_response_bytes) is not int or not 1024 <= self.max_response_bytes <= 8 * (
            1024**2
        ):
            raise ValueError("warmup response budget must be between 1 KiB and 8 MiB")


@dataclass(frozen=True, slots=True)
class ReadinessRule:
    """Approved readiness probe for one template."""

    kind: ReadinessKind
    port: int | None = None
    path: str | None = None
    warmup: WarmupRequest | None = None

    def __post_init__(self) -> None:
        if self.kind in {
            ReadinessKind.TCP_PORT_ACCEPTING,
            ReadinessKind.HTTP_STATUS_OK,
            ReadinessKind.HTTP_WARMUP_OK,
        }:
            if self.port is None or not 1 <= self.port <= 65535:
                raise ValueError(f"{self.kind} readiness requires a port between 1 and 65535")
        elif self.port is not None:
            raise ValueError("process_running readiness carries no port")
        if self.kind is ReadinessKind.HTTP_STATUS_OK:
            if (
                self.path is None
                or len(self.path) > 256
                or _HEALTH_PATH.fullmatch(self.path) is None
                or "//" in self.path
                or any(part in {".", ".."} for part in self.path.split("/"))
            ):
                raise ValueError("http_status_ok requires a fixed absolute health path")
        elif self.kind is ReadinessKind.HTTP_WARMUP_OK:
            if self.path is not None or self.warmup is None:
                raise ValueError("http_warmup_ok requires a declared warmup request")
        elif self.path is not None:
            raise ValueError("only http_status_ok readiness carries a path")
        if self.kind is not ReadinessKind.HTTP_WARMUP_OK and self.warmup is not None:
            raise ValueError("only http_warmup_ok readiness carries a warmup request")


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
    container_memory_limit_bytes: int
    readiness: ReadinessRule = ReadinessRule(ReadinessKind.PROCESS_RUNNING)
    command: tuple[str, ...] | None = None
    gpu_device_count: int = 0
    read_only_binds: tuple[ReadOnlyBind, ...] = ()
    network_name: str | None = None
    engine_auth: EngineAuthKind = EngineAuthKind.NONE

    def __post_init__(self) -> None:
        require_identifier(self.template_id, "template_id")
        if not self.template_id.startswith(_TEMPLATE_ID_PREFIX):
            raise ValueError(f"template_id must start with {_TEMPLATE_ID_PREFIX!r}")
        require_digest(self.image_digest, "image_digest")
        if not self.image_reference or " " in self.image_reference:
            raise ValueError("image_reference must be a single container image reference")
        if self.command is not None and not self.command:
            raise ValueError("a declared command must not be empty")
        if not isinstance(self.engine_auth, EngineAuthKind):
            raise ValueError("engine_auth must select an approved native authentication mode")
        if self.engine_auth is EngineAuthKind.VLLM_API_KEY_V1:
            if (
                self.command is None
                or self.readiness.kind is not ReadinessKind.HTTP_WARMUP_OK
                or self.readiness.warmup is None
                or self.readiness.warmup.path != "/v1/audio/transcriptions"
            ):
                raise ValueError("native ASR authentication requires explicit argv and ASR warmup")
            # CLI api-key overrides VLLM_API_KEY. Refuse abbreviations,
            # underscore spellings and config/root/middleware overrides too.
            forbidden = ("--api-key", "--config", "--root-path", "--middleware")
            options = (argument.replace("_", "-").split("=", 1)[0] for argument in self.command)
            if any(
                option.startswith("--")
                and any(option.startswith(flag) or flag.startswith(option) for flag in forbidden)
                for option in options
            ):
                raise ValueError("approved argv cannot override native engine authentication")
        if self.gpu_device_count < 0:
            raise ValueError("gpu_device_count must not be negative")
        if self.network_name is not None and (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.network_name) is None
            or self.network_name in {"host", "none", "default", "bridge"}
        ):
            raise ValueError("network_name must name an approved custom network")
        if not self.allowed_pool_ids:
            raise ValueError("an approved template declares at least one pool")
        unknown_pools = self.allowed_pool_ids - POOL_IDS
        if unknown_pools:
            raise ValueError(f"approved template declares unknown pools {sorted(unknown_pools)}")
        if self.max_peak_memory_bytes <= 0:
            raise ValueError("max_peak_memory_bytes must be positive")
        if (
            not MIN_CONTAINER_MEMORY_BYTES
            <= self.container_memory_limit_bytes
            <= (self.max_peak_memory_bytes)
        ):
            raise ValueError(
                "container_memory_limit_bytes must be at least 6 MiB and within the peak bound"
            )


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
