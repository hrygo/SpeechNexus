"""Node runtime adapter backed by the local container daemon.

The adapter only ever acts on an approved template: the local image id is
verified against the template's pinned digest before anything is created, the
container carries labels that make its identity discoverable after a controller
restart, host mounts come from the template's approved read-only artifacts, and
no shell string is ever built from a request. A stop becomes evidence only after
the container exited *and* its removal was confirmed, which is what the caller
needs before releasing reserved capacity.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Final, ParamSpec, TypeVar

import httpx

from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    IntentConflictError,
    MemoryArchitecture,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntentRequest,
    RuntimeMemoryVector,
    UnapprovedRuntimeTemplateError,
)
from speechnexus_node_controller.domain.ports import StopCheckpointStore
from speechnexus_node_controller.domain.stop_checkpoint import StopCheckpoint
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ReadinessKind,
    WarmupRequest,
)
from speechnexus_node_controller.infrastructure.docker_api import (
    ContainerState,
    DockerApiError,
    DockerEngineApi,
    DockerUnavailableError,
)

_P = ParamSpec("_P")
_R = TypeVar("_R")
_MAX_WARMUP_BODY_BYTES: Final = 8 * 1024**2
logger = logging.getLogger(__name__)


def _runtime_action(action: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Report a daemon failure as an incomplete action instead of a success.

    The controller must never turn an unanswered daemon into a prepared or
    stopped instance, so an infrastructure failure leaves the caller's intent
    unresolved and retryable. Domain refusals raised inside the action (an
    unapproved template or a conflicting instance identity) are not translated.
    """

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return function(*args, **kwargs)
            except (DockerUnavailableError, DockerApiError) as error:
                raise RuntimeControllerUnavailableError(
                    f"the node runtime did not complete {action}: {type(error).__name__}"
                ) from error

        return wrapped

    return decorate


MANAGED_BY_LABEL: Final = "nexus.managed-by"
MANAGED_BY_VALUE: Final = "speechnexus-node-controller"
NODE_ID_LABEL: Final = "nexus.node-id"
INTENT_ID_LABEL: Final = "nexus.intent-id"
INSTANCE_ID_LABEL: Final = "nexus.instance-id"
GENERATION_LABEL: Final = "nexus.instance-generation"
TEMPLATE_ID_LABEL: Final = "nexus.template-id"
CONFIG_DIGEST_LABEL: Final = "nexus.config-digest"
POOL_ID_LABEL: Final = "nexus.pool-id"
GATEWAY_ID_LABEL: Final = "nexus.gateway-id"
BINDING_REVISION_LABEL: Final = "nexus.binding-revision-id"
MANIFEST_ID_LABEL: Final = "nexus.manifest-id"
FENCING_TOKEN_LABEL: Final = "nexus.fencing-token"


@dataclass(frozen=True, slots=True)
class DockerRuntimeSettings:
    """Bounded timings and naming for one daemon-backed node runtime."""

    stop_timeout_seconds: float = 20.0
    exit_wait_seconds: float = 30.0
    readiness_timeout_seconds: float = 2.0
    name_prefix: str = "speechnexus-instance"


class DockerNodeRuntime:
    """A ``NodeRuntimePort`` that prepares and stops approved engine containers."""

    def __init__(
        self,
        *,
        api: DockerEngineApi,
        node_id: str,
        stop_checkpoints: StopCheckpointStore,
        settings: DockerRuntimeSettings | None = None,
    ) -> None:
        self._api = api
        self._node_id = node_id
        self._stop_checkpoints = stop_checkpoints
        self._settings = settings or DockerRuntimeSettings()
        self._warmup_attempts: dict[tuple[str, int], int] = {}

    @_runtime_action("prepare")
    def prepare(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        """Reuse a matching container or create exactly one for this generation."""
        self._require_approved_image(template)
        existing = self._find(request.instance_id, request.instance_generation)
        if existing is not None:
            self._require_same_identity(existing, request, template)
            return self._observation(request, template, existing)

        container_id = self._api.create_container(
            name=self._container_name(request),
            image_reference=template.image_reference,
            command=template.command,
            labels=self._labels(request),
            host_config=self._host_config(template),
        )
        self._api.start_container(container_id)
        state = self._api.inspect_container(container_id)
        if state is None:
            raise DockerUnavailableError("container disappeared right after start")
        return self._observation(request, template, state)

    @_runtime_action("stop")
    def stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        """Pin before acting, commit exit before deletion, then commit release evidence."""
        if request.action is not RuntimeActionKind.STOP or request.target_node_id != self._node_id:
            raise IntentConflictError("the original stop does not belong to this node")
        checkpoint = self._stop_checkpoints.get_stop_checkpoint(request)
        if checkpoint is None:
            existing = self._find(request.instance_id, request.instance_generation)
            if existing is None:
                return self._absent_observation(request, template)
            self._require_stop_identity(existing, request, template)
            checkpoint = self._stop_checkpoints.save_stop_checkpoint(
                StopCheckpoint(request=request, container_id=existing.container_id)
            )
        return self._resume_stop(checkpoint, template)

    @_runtime_action("recover stop")
    def recover_stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        """Resume the persisted original stop; never retarget another container."""
        return self.stop(request, template)

    def _resume_stop(
        self, checkpoint: StopCheckpoint, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        request = checkpoint.request
        matching = self._find(request.instance_id, request.instance_generation)
        if matching is not None and matching.container_id != checkpoint.container_id:
            return self._stop_conflict(request, template)
        existing = self._api.inspect_container(checkpoint.container_id)
        if existing is None:
            if checkpoint.exited_at is None:
                return self._absent_observation(request, template)
            return self._released_observation(checkpoint, template)
        self._require_stop_identity(existing, request, template)
        if checkpoint.released_at is not None or (
            checkpoint.exited_at is not None and existing.running
        ):
            return self._stop_conflict(request, template)
        if checkpoint.exited_at is None and existing.running:
            try:
                self._api.stop_container(
                    existing.container_id, timeout_seconds=self._settings.stop_timeout_seconds
                )
            except DockerApiError as error:
                state = self._api.inspect_container(existing.container_id)
                if state is not None and state.running:
                    return self._observation(request, template, state)
                raise DockerUnavailableError(
                    f"container stop failed with {error.status_code}"
                ) from error

        if checkpoint.exited_at is None:
            exit_code = self._wait_for_exit(existing.container_id)
            if exit_code is None:
                return self._inspect_or_absent(request, template, existing.container_id)
            exited = self._api.inspect_container(existing.container_id)
            if exited is None:
                return self._absent_observation(request, template)
            self._require_stop_identity(exited, request, template)
            if exited.running or exited.exit_code != exit_code:
                return self._stop_conflict(request, template)
            checkpoint = self._stop_checkpoints.save_stop_checkpoint(
                replace(checkpoint, exit_code=exit_code, exited_at=datetime.now(UTC))
            )
            existing = exited
        elif existing.exit_code != checkpoint.exit_code:
            return self._stop_conflict(request, template)
        if not self._remove_and_confirm(existing.container_id):
            return self._inspect_or_absent(request, template, existing.container_id)
        self._warmup_attempts.pop((existing.container_id, request.instance_generation), None)
        return self._released_observation(checkpoint, template, exited=existing)

    def _released_observation(
        self,
        checkpoint: StopCheckpoint,
        template: ApprovedRuntimeTemplate,
        *,
        exited: ContainerState | None = None,
    ) -> InstanceObservation:
        if (
            self._find(checkpoint.request.instance_id, checkpoint.request.instance_generation)
            is not None
        ):
            return self._stop_conflict(checkpoint.request, template)
        if checkpoint.released_at is None:
            checkpoint = self._stop_checkpoints.save_stop_checkpoint(
                replace(checkpoint, released_at=datetime.now(UTC))
            )
        return InstanceObservation(
            instance_id=checkpoint.request.instance_id,
            instance_generation=checkpoint.request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=checkpoint.request.config_digest,
            observed_instance_state=(
                ObservedInstanceState.STOPPED if exited else ObservedInstanceState.ABSENT
            ),
            observed_peak_memory=(
                self._observed_memory(checkpoint.request, exited) if exited else None
            ),
            stop_evidence=checkpoint.evidence(),
        )

    @staticmethod
    def _stop_conflict(
        request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=request.config_digest,
            observed_instance_state=ObservedInstanceState.QUARANTINED,
            quarantine_reason="the pinned stop target no longer matches its recorded facts",
        )

    def _require_stop_identity(
        self,
        state: ContainerState,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
    ) -> None:
        self._require_same_identity(state, request, template)
        expected = self._labels(request)
        if any(
            state.labels.get(name) != value
            for name, value in expected.items()
            if name not in {INTENT_ID_LABEL, FENCING_TOKEN_LABEL}
        ):
            raise IntentConflictError("the pinned stop target belongs to another runtime identity")

    @_runtime_action("inspect")
    def inspect(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        """Read-only observation; it never fabricates stop evidence."""
        state = self._find(request.instance_id, request.instance_generation)
        if state is None:
            return self._absent_observation(request, template)
        if (
            state.labels.get(TEMPLATE_ID_LABEL) != template.template_id
            or state.labels.get(CONFIG_DIGEST_LABEL) != request.config_digest
        ):
            return InstanceObservation(
                instance_id=request.instance_id,
                instance_generation=request.instance_generation,
                approved_template_id=template.template_id,
                config_digest=request.config_digest,
                observed_instance_state=ObservedInstanceState.QUARANTINED,
                quarantine_reason="container labels do not match the recorded intent",
            )
        return self._observation(request, template, state)

    def _require_approved_image(self, template: ApprovedRuntimeTemplate) -> None:
        identity = self._api.inspect_image(template.image_reference)
        if identity is None or identity.image_id != template.image_digest:
            raise UnapprovedRuntimeTemplateError(
                f"image {template.image_reference} is not the approved digest "
                f"{template.image_digest}"
            )

    def _find(self, instance_id: str, instance_generation: int) -> ContainerState | None:
        return self._api.find_container(
            {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                NODE_ID_LABEL: self._node_id,
                INSTANCE_ID_LABEL: instance_id,
                GENERATION_LABEL: str(instance_generation),
            }
        )

    @staticmethod
    def _require_same_identity(
        state: ContainerState,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
    ) -> None:
        if (
            state.labels.get(TEMPLATE_ID_LABEL) != template.template_id
            or state.labels.get(CONFIG_DIGEST_LABEL) != request.config_digest
        ):
            raise IntentConflictError(
                f"instance {request.instance_id} generation {request.instance_generation} already "
                "carries another template or configuration digest"
            )

    def _labels(self, request: RuntimeInstanceIntentRequest) -> dict[str, str]:
        return {
            MANAGED_BY_LABEL: MANAGED_BY_VALUE,
            NODE_ID_LABEL: self._node_id,
            INTENT_ID_LABEL: request.intent_id,
            INSTANCE_ID_LABEL: request.instance_id,
            GENERATION_LABEL: str(request.instance_generation),
            TEMPLATE_ID_LABEL: request.approved_template_id,
            CONFIG_DIGEST_LABEL: request.config_digest,
            POOL_ID_LABEL: request.pool_id,
            GATEWAY_ID_LABEL: request.gateway_id,
            BINDING_REVISION_LABEL: request.binding_revision_id,
            MANIFEST_ID_LABEL: request.manifest_id,
            FENCING_TOKEN_LABEL: str(request.fencing_token),
        }

    def _host_config(self, template: ApprovedRuntimeTemplate) -> dict[str, Any]:
        host_config: dict[str, Any] = {
            "Binds": [f"{bind.source}:{bind.target}:ro" for bind in template.read_only_binds],
            "Privileged": False,
            "AutoRemove": False,
            # An init process forwards signals and reaps children, so a graceful
            # stop reaches the engine instead of being ignored by PID 1.
            "Init": True,
            "SecurityOpt": ["no-new-privileges"],
            "CapDrop": ["ALL"],
            "RestartPolicy": {"Name": "no"},
            "Memory": template.container_memory_limit_bytes,
            # Docker interprets equal Memory/MemorySwap as no additional swap.
            "MemorySwap": template.container_memory_limit_bytes,
        }
        if template.gpu_device_count:
            host_config["DeviceRequests"] = [
                {
                    "Driver": "nvidia",
                    "Count": template.gpu_device_count,
                    "Capabilities": [["gpu"]],
                }
            ]
        if template.network_name is not None:
            host_config["NetworkMode"] = template.network_name
        return host_config

    def _container_name(self, request: RuntimeInstanceIntentRequest) -> str:
        sanitized = "".join(
            character if character.isalnum() or character in "_.-" else "-"
            for character in request.instance_id
        )
        return f"{self._settings.name_prefix}-{sanitized}-g{request.instance_generation}"

    def _wait_for_exit(self, container_id: str) -> int | None:
        try:
            return self._api.wait_container(
                container_id, timeout_seconds=self._settings.exit_wait_seconds
            )
        except DockerApiError, DockerUnavailableError:
            return None

    def _remove_and_confirm(self, container_id: str) -> bool:
        try:
            self._api.remove_container(container_id)
        except DockerApiError, DockerUnavailableError:
            return False
        return self._api.inspect_container(container_id) is None

    def _inspect_or_absent(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        container_id: str,
    ) -> InstanceObservation:
        state = self._api.inspect_container(container_id)
        if state is None:
            return self._absent_observation(request, template)
        return self._observation(request, template, state)

    def _is_ready(self, template: ApprovedRuntimeTemplate, state: ContainerState) -> bool:
        if not state.running:
            return False
        if template.readiness.kind is ReadinessKind.PROCESS_RUNNING:
            return True
        port = template.readiness.port
        if port is None:
            return False
        for address in state.network_addresses:
            if template.readiness.kind is ReadinessKind.HTTP_STATUS_OK:
                if self._http_ready(address, port, template.readiness.path):
                    return True
                continue
            try:
                with socket.create_connection(
                    (address, port), timeout=self._settings.readiness_timeout_seconds
                ):
                    return True
            except OSError:
                continue
        return False

    def _http_ready(self, address: str, port: int, path: str | None) -> bool:
        """Read headers from the inspected container IP; never proxy or redirect."""
        if path is None:
            return False
        try:
            literal = ip_address(address)
        except ValueError:
            return False
        host = f"[{literal}]" if literal.version == 6 else str(literal)
        try:
            with (
                httpx.Client(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self._settings.readiness_timeout_seconds,
                ) as client,
                client.stream("GET", f"http://{host}:{port}{path}") as response,
            ):
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    def _observation(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        state: ContainerState,
    ) -> InstanceObservation:
        if state.running and (
            state.memory_limit_bytes != template.container_memory_limit_bytes
            or state.memory_swap_limit_bytes != template.container_memory_limit_bytes
            or (template.network_name is not None and state.network_mode != template.network_name)
        ):
            return InstanceObservation(
                instance_id=request.instance_id,
                instance_generation=request.instance_generation,
                approved_template_id=template.template_id,
                config_digest=request.config_digest,
                observed_instance_state=ObservedInstanceState.QUARANTINED,
                observed_peak_memory=self._observed_memory(request, state),
                quarantine_reason="container resource/network configuration is not approved",
            )
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=request.config_digest,
            observed_instance_state=self._observed_state(request, template, state),
            observed_peak_memory=self._observed_memory(request, state),
        )

    @staticmethod
    def _absent_observation(
        request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=request.config_digest,
            observed_instance_state=ObservedInstanceState.ABSENT,
            stop_evidence=None,
        )

    def _observed_state(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        state: ContainerState,
    ) -> ObservedInstanceState:
        if state.status == "created":
            return ObservedInstanceState.PREPARING
        if state.status == "running":
            if template.readiness.kind is ReadinessKind.HTTP_WARMUP_OK:
                return self._warmup_state(request, template, state)
            return (
                ObservedInstanceState.READY
                if self._is_ready(template, state)
                else ObservedInstanceState.LOADING
            )
        if state.status == "paused":
            return ObservedInstanceState.DRAINING
        if state.status == "restarting":
            return ObservedInstanceState.LOADING
        if state.status in {"exited", "dead", "removing"}:
            return ObservedInstanceState.STOPPED
        return ObservedInstanceState.FAILED

    def _warmup_state(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        state: ContainerState,
    ) -> ObservedInstanceState:
        """Stay at peak until every declared warmup request has completed."""
        port = template.readiness.port
        warmup = template.readiness.warmup
        if port is None or warmup is None:
            return ObservedInstanceState.LOADING
        address = self._warmup_address(self._literal_addresses(state), port)
        if address is None:
            return ObservedInstanceState.LOADING
        key = (state.container_id, request.instance_generation)
        completed = self._warmup_attempts.get(key, 0)
        if completed >= warmup.attempts:
            return ObservedInstanceState.READY
        if request.action is not RuntimeActionKind.PREPARE:
            # A stop or drain observation never spends model time on warmup.
            return ObservedInstanceState.WARMING_UP
        body = self._warmup_body(warmup)
        if body is not None and self._run_warmup(
            address, port, warmup, body, attempt=completed + 1
        ):
            completed += 1
            self._warmup_attempts[key] = completed
        if completed >= warmup.attempts:
            self._warmup_attempts[key] = completed
            return ObservedInstanceState.READY
        return ObservedInstanceState.WARMING_UP

    @staticmethod
    def _literal_addresses(state: ContainerState) -> tuple[str, ...]:
        literals: list[str] = []
        for address in state.network_addresses:
            try:
                literals.append(str(ip_address(address)))
            except ValueError:
                continue
        return tuple(literals)

    def _warmup_address(self, addresses: tuple[str, ...], port: int) -> str | None:
        """Select before POST; an uncertain warmup is never retried at another IP."""
        deadline = time.monotonic() + self._settings.readiness_timeout_seconds
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with socket.create_connection((address, port), timeout=remaining):
                    return address if time.monotonic() < deadline else None
            except OSError:
                continue
        return None

    @staticmethod
    def _warmup_body(warmup: WarmupRequest) -> bytes | None:
        """Read the declared body and prove its digest before every attempt."""
        try:
            path = Path(warmup.body_path)
            if path.stat().st_size > _MAX_WARMUP_BODY_BYTES:
                return None
            body = path.read_bytes()
        except OSError:
            logger.warning("warmup body is unreadable")
            return None
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if not body or digest != warmup.body_sha256:
            logger.warning("warmup body does not match its approved digest")
            return None
        return body

    def _run_warmup(
        self,
        address: str,
        port: int,
        warmup: WarmupRequest,
        body: bytes,
        *,
        attempt: int,
    ) -> bool:
        """Send one fixed request and require a complete 200 response within budget."""
        literal = ip_address(address)
        host = f"[{literal}]" if literal.version == 6 else str(literal)
        started = time.monotonic()
        deadline = started + warmup.timeout_seconds

        async def request() -> int | None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # One timer covers connect, response headers and the entire body.
            # Per-read inactivity timeouts cannot stop a peer that keeps trickling.
            async with asyncio.timeout(remaining):
                async with (
                    httpx.AsyncClient(
                        trust_env=False,
                        follow_redirects=False,
                        timeout=remaining,
                    ) as client,
                    client.stream(
                        "POST",
                        f"http://{host}:{port}{warmup.path}",
                        content=body,
                        headers={
                            "Content-Type": warmup.content_type,
                            "Accept": "*/*",
                            "Accept-Encoding": "identity",
                        },
                    ) as response,
                ):
                    if (
                        response.status_code != 200
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        return None
                    total = 0
                    async for chunk in response.aiter_bytes(4096):
                        total += len(chunk)
                        if total > warmup.max_response_bytes:
                            return None
                    return total

        try:
            # The synchronous runtime port is called by the controller's worker
            # thread. Closing this loop joins transport cleanup before returning.
            total = asyncio.run(request())
        except TimeoutError, httpx.HTTPError:
            return False
        if total is None or time.monotonic() >= deadline:
            return False
        logger.info(
            "warmup attempt completed",
            extra={
                "attempt": attempt,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "response_bytes": total,
            },
        )
        return True

    @staticmethod
    def _observed_memory(
        request: RuntimeInstanceIntentRequest, state: ContainerState
    ) -> RuntimeMemoryVector | None:
        peak_bytes = state.memory_peak_bytes
        if peak_bytes is None or peak_bytes <= 0:
            return None
        if request.reserved_memory.architecture is MemoryArchitecture.UNIFIED_MEMORY:
            return RuntimeMemoryVector(unified_memory_bytes=peak_bytes)
        return RuntimeMemoryVector(host_memory_bytes=peak_bytes)
