"""Node runtime adapter backed by the cluster API: one engine Pod per generation.

The adapter only acts on an approved template. The Pod's image is pinned by
digest, the container memory limit comes from the template, host directories are
the template's approved read-only artifacts, and no shell string is ever built
from a request. GPU access is handed to the RuntimeClass handler the cluster runs
(nvidia) instead of a ``nvidia.com/gpu`` resource request: this cluster does not
run a device plugin, so a resource request would leave every engine Pod Pending
forever even though the GPU is present.

Two things this driver deliberately does *not* do:

* It never turns "the Pod reports Ready" into "this instance may serve". A Pod
  probe is transport health; for a template that declares controller-side warmup
  the instance stays ``warming_up`` until that action exists for this driver.
* It never quietly drops ``network_name``. Docker's dedicated instance network
  has no Kubernetes equivalent, so a template that asks for one is refused with
  an explicit unsupported observation rather than silently widening isolation.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    IntentConflictError,
    NativeRuntimeEvidence,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntentRequest,
    RuntimeIntentErrorCode,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    EngineAuthKind,
    ReadinessKind,
)
from speechnexus_node_controller.infrastructure.engine_credentials import (
    EngineCredentials,
    engine_environment,
)
from speechnexus_node_controller.infrastructure.kubernetes_api import (
    KubernetesApiError,
    KubernetesPodApi,
    KubernetesUnavailableError,
    PodState,
)
from speechnexus_node_controller.infrastructure.native_evidence import (
    controller_artifact_digest,
    template_digest,
)

RUNTIME_PLAN: Final = "docs/plans/2026-11-25-engine-runtime-k8s.md"

MANAGED_BY_LABEL: Final = "speechnexus.io/managed-by"
MANAGED_BY_VALUE: Final = "speechnexus-node-controller"
NODE_ID_LABEL: Final = "speechnexus.io/node-id"
INSTANCE_ID_LABEL: Final = "speechnexus.io/instance-id"
GENERATION_LABEL: Final = "speechnexus.io/generation"
TEMPLATE_ID_LABEL: Final = "speechnexus.io/template-id"

# The raw intent identity does not survive label validation (instance IDs may
# contain ':' and digests always do, and a label value is capped at 63
# characters). Labels carry the sanitized identity used for discovery; the exact
# identity lives in annotations so a same-named Pod can be classified instead of
# being mistaken for this generation's own creation.
INSTANCE_ID_ANNOTATION: Final = "speechnexus.io/instance-id"
TEMPLATE_ID_ANNOTATION: Final = "speechnexus.io/template-id"
CONFIG_DIGEST_ANNOTATION: Final = "speechnexus.io/config-digest"
FENCING_TOKEN_ANNOTATION: Final = "speechnexus.io/fencing-token"

ENGINE_CONTAINER_NAME: Final = "engine"
_MAX_LABEL_VALUE: Final = 63
_MAX_POD_NAME: Final = 63


@dataclass(frozen=True, slots=True)
class KubernetesRuntimeSettings:
    """Bounded timings and naming for one cluster-backed node runtime."""

    # The single namespace the documented Role grants; never a request field, so
    # an intent cannot place an engine outside the controller's own namespace.
    namespace: str
    # How long one prepare action may wait for the Pod to report Ready. This is
    # the transport budget; a template that declares controller-side warmup keeps
    # its own budget from ``readiness.warmup.timeout_seconds``.
    pod_ready_timeout_seconds: float = 180.0
    poll_interval_seconds: float = 1.0
    name_prefix: str = "engine"
    nvidia_runtime_class_name: str = "nvidia"
    # Deployment-supplied VRAM budget, passed to the engine's own flag. The
    # driver never expresses the peak bound as a Kubernetes resource request.
    gpu_memory_utilization: float | None = None


class KubernetesNodeRuntime:
    """A ``NodeRuntimePort`` whose engine instances are Pods in one namespace."""

    def __init__(
        self,
        *,
        api: KubernetesPodApi,
        node_id: str,
        settings: KubernetesRuntimeSettings,
        engine_credentials: EngineCredentials | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._api = api
        self._node_id = node_id
        self._settings = settings
        self._engine_credentials = engine_credentials
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        # One prepare action's single deadline, anchored before any API call and
        # kept across retries so a repeated prepare cannot restart the budget.
        self._deadlines: dict[tuple[str, int], float] = {}
        # Fencing anchors recorded as soon as the API returns a Pod identity: the
        # UID a stop has to match, so a same-named replacement is never stopped as
        # if it were the generation this controller created.
        self._pod_uids: dict[tuple[str, int], str] = {}

    def prepare(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        unsupported = self._unsupported_template(request, template)
        if unsupported is not None:
            return unsupported
        try:
            return self._prepare(request, template)
        except (KubernetesUnavailableError, KubernetesApiError) as error:
            # An unanswered or refusing API server is not a failed instance and not
            # a ready one: the intent stays unresolved and retryable, exactly as
            # an unanswered container daemon does.
            raise RuntimeControllerUnavailableError(
                f"the node runtime did not complete prepare: {type(error).__name__}"
            ) from error

    def _prepare(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        deadline = self._anchor_deadline(request, template)
        name = self._pod_name(request)
        manifest = self._pod_manifest(request, template, name)
        key = (request.instance_id, request.instance_generation)
        observed = self._api.get_pod(name)
        if observed is None:
            try:
                observed = self._api.create_pod(manifest)
            except KubernetesApiError as error:
                if error.status_code != 409:
                    raise
                # A concurrent create of the same generation may have won; only an
                # exact identity match may be adopted, never an unknown Pod.
                observed = self._api.get_pod(name)
                if observed is None:
                    raise
        self._require_same_identity(observed, request, template)
        self._pod_uids[key] = observed.uid
        return self._await_generation(request, template, observed, deadline)

    def stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        raise NotImplementedError(f"the Kubernetes stop path is S3 of {RUNTIME_PLAN}")

    def inspect(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        *,
        resumed: bool = False,
    ) -> InstanceObservation | None:
        raise NotImplementedError(f"the Kubernetes inspection path is S4 of {RUNTIME_PLAN}")

    def recover_stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        raise NotImplementedError(f"the Kubernetes stop recovery path is S3 of {RUNTIME_PLAN}")

    def _unsupported_template(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        """Refuse a template field this driver cannot represent, without acting."""
        if template.network_name is None:
            return None
        return self._observation(
            request,
            template,
            ObservedInstanceState.FAILED,
            error_code=RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED,
            quarantine_reason=(
                "unsupported template field: network_name has no Kubernetes equivalent; "
                "silently ignoring it would widen the isolation the approved template "
                "declares, so this driver refuses the template instead"
            ),
        )

    def _await_generation(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        pod: PodState,
        deadline: float,
    ) -> InstanceObservation:
        """Wait for this generation's Pod to become transport-ready or spend its budget."""
        observed = pod
        while True:
            terminated = self._terminated_observation(request, template, observed)
            if terminated is not None:
                return terminated
            if observed.ready:
                return self._ready_observation(request, template, deadline)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._observation(
                    request,
                    template,
                    ObservedInstanceState.LOADING,
                    error_code=RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED,
                )
            self._sleep(min(self._settings.poll_interval_seconds, remaining))
            current = self._api.get_pod(pod.name)
            if current is None:
                return self._observation(request, template, ObservedInstanceState.ABSENT)
            if current.uid != pod.uid:
                return self._observation(
                    request,
                    template,
                    ObservedInstanceState.QUARANTINED,
                    quarantine_reason=(
                        "the engine pod name now belongs to another identity than the one "
                        "this controller created"
                    ),
                )
            observed = current

    def _ready_observation(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        deadline: float,
    ) -> InstanceObservation:
        """Decide what a transport-ready Pod means for this template's readiness rule."""
        if template.readiness.kind is ReadinessKind.HTTP_WARMUP_OK:
            # §3.3: the Pod probe only proves the transport layer. The controller
            # still has to run the declared warmup before the instance may serve,
            # so this driver keeps reporting warming_up instead of claiming ready.
            return self._observation(
                request,
                template,
                ObservedInstanceState.WARMING_UP,
                error_code=(
                    RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED
                    if self._monotonic() >= deadline
                    else None
                ),
            )
        self._deadlines.pop((request.instance_id, request.instance_generation), None)
        return self._observation(request, template, ObservedInstanceState.READY)

    def _terminated_observation(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        pod: PodState,
    ) -> InstanceObservation | None:
        if pod.exit_code is None and pod.phase not in {"Failed", "Succeeded"}:
            return None
        if pod.exit_code == 0 or (pod.exit_code is None and pod.phase == "Succeeded"):
            return self._observation(request, template, ObservedInstanceState.STOPPED)
        return self._observation(
            request,
            template,
            ObservedInstanceState.FAILED,
            error_code=RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED,
        )

    def _observation(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        state: ObservedInstanceState,
        *,
        error_code: RuntimeIntentErrorCode | None = None,
        quarantine_reason: str | None = None,
    ) -> InstanceObservation:
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=request.config_digest,
            observed_instance_state=state,
            # The pod API reports no memory peak; the driver never invents one.
            observed_peak_memory=None,
            quarantine_reason=quarantine_reason,
            error_code=error_code,
            native_runtime_evidence=(
                self._native_evidence(request, template)
                if state is ObservedInstanceState.READY
                else None
            ),
        )

    def _native_evidence(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> NativeRuntimeEvidence | None:
        if (
            request.action is not RuntimeActionKind.PREPARE
            or template.engine_auth is EngineAuthKind.NONE
        ):
            return None
        try:
            return NativeRuntimeEvidence(
                template.image_digest,
                template_digest(template),
                controller_artifact_digest(),
                template.engine_auth.value,
                self._wall_clock(),
            )
        except OSError, ValueError:
            # Readiness stands; qualification must fail closed instead.
            return None

    def _anchor_deadline(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> float:
        """One prepare action's deadline, capped by the intent's own deadline."""
        key = (request.instance_id, request.instance_generation)
        deadline = self._deadlines.get(key)
        if deadline is not None:
            return deadline
        warmup = template.readiness.warmup
        budget = (
            warmup.timeout_seconds
            if warmup is not None
            else self._settings.pod_ready_timeout_seconds
        )
        remaining = (request.deadline_at - self._wall_clock()).total_seconds()
        deadline = self._monotonic() + min(budget, remaining)
        self._deadlines[key] = deadline
        return deadline

    def _pod_manifest(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        name: str,
    ) -> dict[str, Any]:
        container: dict[str, Any] = {
            "name": ENGINE_CONTAINER_NAME,
            "image": _pinned_image(template),
            "imagePullPolicy": "IfNotPresent",
            "resources": {"limits": {"memory": str(template.container_memory_limit_bytes)}},
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
        }
        environment = self._environment(request, template)
        if environment:
            container["env"] = [{"name": name, "value": value} for name, value in environment]
        command = self._engine_command(template)
        if command is not None:
            # The approved template stores argv: the first element is the program
            # and the rest are its arguments, exactly as a container runtime splits
            # an exec form.
            container["command"] = [command[0]]
            if len(command) > 1:
                container["args"] = list(command[1:])
        volumes: list[dict[str, Any]] = []
        mounts: list[dict[str, Any]] = []
        for index, bind in enumerate(template.read_only_binds):
            name = f"approved-bind-{index}"
            volumes.append({"name": name, "hostPath": {"path": bind.source, "type": "Directory"}})
            mounts.append({"name": name, "mountPath": bind.target, "readOnly": True})
        if mounts:
            container["volumeMounts"] = mounts
        probe = _transport_probe(template)
        if probe is not None:
            container["readinessProbe"] = probe

        spec: dict[str, Any] = {
            # The controller owns the lifecycle: an engine that crashes must be
            # observed as terminated, not restarted behind the intent's back.
            "restartPolicy": "Never",
            # The engine needs no API credential; only the controller has one.
            "automountServiceAccountToken": False,
            "containers": [container],
        }
        if template.gpu_device_count:
            spec["runtimeClassName"] = self._settings.nvidia_runtime_class_name
        if volumes:
            spec["volumes"] = volumes
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self._settings.namespace,
                "labels": self._labels(request, template),
                "annotations": self._annotations(request, template),
            },
            "spec": spec,
        }

    def _environment(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> tuple[tuple[str, str], ...]:
        """Template environment, then the per-generation credential and GPU visibility."""
        entries = list(template.environment)
        entries.extend(
            engine_environment(
                request,
                template,
                node_id=self._node_id,
                credentials=self._engine_credentials,
            )
        )
        if template.gpu_device_count:
            # The RuntimeClass handler exposes the host GPU; the same two variables
            # the working qwenpaw-asr pod uses are what make it visible to CUDA.
            entries.extend(
                (
                    ("NVIDIA_VISIBLE_DEVICES", "all"),
                    ("NVIDIA_DRIVER_CAPABILITIES", "compute,utility"),
                )
            )
        return tuple(entries)

    def _engine_command(self, template: ApprovedRuntimeTemplate) -> tuple[str, ...] | None:
        """The approved argv, with the deployment's utilization cap appended.

        The template never carries a VRAM number: the same approved file runs on
        cards with different memory, so the bound is deployment configuration. The
        peak stays the engine's own ``--gpu-memory-utilization`` limit rather than
        a pod resource request.
        """
        if template.command is None:
            return None
        command = tuple(template.command)
        utilization = self._settings.gpu_memory_utilization
        if not template.gpu_device_count or utilization is None:
            return command
        if "--gpu-memory-utilization" in command:
            return command
        return (*command, "--gpu-memory-utilization", f"{utilization:g}")

    def _pod_name(self, request: RuntimeInstanceIntentRequest) -> str:
        """``engine-{instance_id}-{generation}``, bounded to a valid DNS label."""
        generation = str(request.instance_generation)
        prefix = self._settings.name_prefix
        sanitized = _sanitize_identifier(request.instance_id)
        stem = f"{prefix}-{sanitized}-{generation}"
        if len(stem) <= _MAX_POD_NAME:
            return stem
        # Keep the name a legal label while preserving uniqueness: two long
        # instance IDs must not truncate to the same Pod name.
        suffix = "-" + hashlib.sha256(request.instance_id.encode()).hexdigest()[:8]
        available = _MAX_POD_NAME - len(prefix) - 1 - len(suffix) - 1 - len(generation)
        return f"{prefix}-{sanitized[: max(available, 1)]}{suffix}-{generation}"

    def _labels(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> dict[str, str]:
        return {
            MANAGED_BY_LABEL: MANAGED_BY_VALUE,
            NODE_ID_LABEL: _label_value(self._node_id),
            INSTANCE_ID_LABEL: _label_value(request.instance_id),
            GENERATION_LABEL: str(request.instance_generation),
            TEMPLATE_ID_LABEL: _label_value(template.template_id),
        }

    @staticmethod
    def _annotations(
        request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> dict[str, str]:
        return {
            INSTANCE_ID_ANNOTATION: request.instance_id,
            TEMPLATE_ID_ANNOTATION: template.template_id,
            CONFIG_DIGEST_ANNOTATION: request.config_digest,
            FENCING_TOKEN_ANNOTATION: str(request.fencing_token),
        }

    @staticmethod
    def _require_same_identity(
        pod: PodState,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
    ) -> None:
        """A same-named Pod is only this generation's when its recorded identity matches."""
        if (
            pod.annotations.get(INSTANCE_ID_ANNOTATION) != request.instance_id
            or pod.annotations.get(TEMPLATE_ID_ANNOTATION) != template.template_id
            or pod.annotations.get(CONFIG_DIGEST_ANNOTATION) != request.config_digest
        ):
            raise IntentConflictError(
                f"instance {request.instance_id} generation {request.instance_generation} "
                "already exists with another template or configuration digest"
            )


def _pinned_image(template: ApprovedRuntimeTemplate) -> str:
    """The image reference in digest form; a tag is a moving target for an intent.

    The repository is kept as approved and only the tag is dropped, so a registry
    with a port in its name keeps that port: the tag is the last ``:`` segment
    after the final ``/``.
    """
    reference = template.image_reference.split("@", 1)[0]
    cut = reference.rfind(":")
    if cut > reference.rfind("/"):
        reference = reference[:cut]
    return f"{reference}@{template.image_digest}"


def _transport_probe(template: ApprovedRuntimeTemplate) -> dict[str, Any] | None:
    """The Pod probe, which carries only the readiness rule's transport half.

    The controller remains the authority for the readiness decision; this probe
    exists so the Pod is not reported Ready while the engine does not listen. A
    template that declares controller-side warmup still gets a transport probe
    only: no kubelet probe can perform the engine's warmup request.
    """
    port = template.readiness.port
    if port is None:
        return None
    if template.readiness.kind is ReadinessKind.HTTP_STATUS_OK and template.readiness.path:
        return {
            "httpGet": {"path": template.readiness.path, "port": port},
            "periodSeconds": 2,
            "timeoutSeconds": 3,
        }
    return {
        "tcpSocket": {"port": port},
        "periodSeconds": 2,
        "timeoutSeconds": 3,
    }


def _sanitize_identifier(value: str) -> str:
    """Fold an opaque intent identifier into the pod-name character set."""
    folded = value.lower().replace(".", "-").replace("_", "-").replace(":", "-")
    sanitized = re.sub(r"[^a-z0-9-]+", "-", folded).strip("-")
    return sanitized or "instance"


def _label_value(value: str) -> str:
    """A legal label value; the exact identity is kept in the annotation."""
    sanitized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not sanitized:
        return "unset"
    if len(sanitized) <= _MAX_LABEL_VALUE:
        return sanitized
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"{sanitized[: _MAX_LABEL_VALUE - len(digest) - 1]}-{digest}"
