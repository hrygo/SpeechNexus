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

import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Final, ParamSpec, TypeVar

from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    IntentConflictError,
    MemoryArchitecture,
    ObservedInstanceState,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntentRequest,
    RuntimeMemoryVector,
    RuntimeStopEvidence,
    UnapprovedRuntimeTemplateError,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ReadinessKind,
)
from speechnexus_node_controller.infrastructure.docker_api import (
    ContainerState,
    DockerApiError,
    DockerEngineApi,
    DockerUnavailableError,
)

_P = ParamSpec("_P")
_R = TypeVar("_R")


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
        settings: DockerRuntimeSettings | None = None,
    ) -> None:
        self._api = api
        self._node_id = node_id
        self._settings = settings or DockerRuntimeSettings()

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
        """Stop, wait for exit, remove, and only then report stop evidence."""
        existing = self._find(request.instance_id, request.instance_generation)
        if existing is None:
            return self._absent_observation(request, template)
        self._require_same_identity(existing, request, template)

        if existing.running:
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

        exit_code = self._wait_for_exit(existing.container_id)
        if exit_code is None:
            return self._inspect_or_absent(request, template, existing.container_id)

        if not self._remove_and_confirm(existing.container_id):
            return self._inspect_or_absent(request, template, existing.container_id)

        return self._observation(
            request,
            template,
            ContainerState(
                container_id=existing.container_id,
                status="exited",
                running=False,
                exit_code=exit_code,
                labels=existing.labels,
                network_addresses=(),
                memory_peak_bytes=existing.memory_peak_bytes,
            ),
            stop_evidence=RuntimeStopEvidence(
                verified_at=datetime.now(UTC),
                exit_code=exit_code,
                runtime_container_id=existing.container_id,
            ),
        )

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
        }
        if template.gpu_device_count:
            host_config["DeviceRequests"] = [
                {
                    "Driver": "nvidia",
                    "Count": template.gpu_device_count,
                    "Capabilities": [["gpu"]],
                }
            ]
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
            try:
                with socket.create_connection(
                    (address, port), timeout=self._settings.readiness_timeout_seconds
                ):
                    return True
            except OSError:
                continue
        return False

    def _observation(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        state: ContainerState,
        *,
        stop_evidence: RuntimeStopEvidence | None = None,
    ) -> InstanceObservation:
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=request.config_digest,
            observed_instance_state=self._observed_state(template, state),
            observed_peak_memory=self._observed_memory(request, state),
            stop_evidence=stop_evidence,
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
        self, template: ApprovedRuntimeTemplate, state: ContainerState
    ) -> ObservedInstanceState:
        if state.status == "created":
            return ObservedInstanceState.PREPARING
        if state.status == "running":
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
