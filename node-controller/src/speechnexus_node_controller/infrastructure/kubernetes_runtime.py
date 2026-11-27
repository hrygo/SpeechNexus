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

Stop fencing is anchored on the **Pod UID**, and the anchor is durable:

* The UID of the Pod a stop may act on is pinned in the same
  ``StopCheckpointStore`` the container driver uses, in the same
  target-then-exit-then-release order. ``StopCheckpoint.container_id`` holds it;
  a 36-character UUID is well inside the 128-character bound the checkpoint
  already enforces, so no public contract changes.
* A Pod recreated under the same name receives a new UID. A pinned stop whose
  UID no longer matches therefore refuses with a quarantine observation and
  never issues a delete, so an old generation's stop intent cannot kill the new
  instance that took its name.
* The pin is read back from the checkpoint, never from this process's memory, so
  a restarted controller can still decide. An in-memory map could not: after a
  restart it is empty, and a stop that fell back to the Pod name would delete
  whatever currently holds that name.

A deletion request is not a stop. The observation is only ``stopped`` after the
Pod is gone from the API, and the exit code is only ever the one the API
reported for the terminated engine container. A Pod that disappeared before any
terminated state was observed leaves no exit fact, so no release evidence is
written and the caller must treat the stop as unproven rather than as done.

Inspection is read-only, and it distinguishes three things ``inspect`` must not
confuse. "The API refused or did not answer" raises, so the intent stays open and
retryable instead of recording a vanished instance. "The API says this Pod does
not exist" is ``absent`` with no stop evidence, because nothing was removed.
"The Pod exists but is not serving" is reported as it is, with the event *reason*
that explains it — never the event message, which carries node names and image
references — and never promoted to ready because the Pod's Ready condition is
only the transport layer.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final

from speechnexus_node_controller.domain.gpu_budget import (
    GpuBudget,
    GpuBudgetUndeclaredError,
    resolve_gpu_budget,
)
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
from speechnexus_node_controller.domain.ports import StopCheckpointStore
from speechnexus_node_controller.domain.stop_checkpoint import StopCheckpoint
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

# Kubernetes event reasons that classify why a Pod has not started. Only these
# fixed phrases (and the token itself, which is one of these keys) may reach an
# observation: an event *message* carries node names, image references and text
# written by other cluster components, so it is never read at all.
EVENT_FAILURE_REASONS: Final[dict[str, str]] = {
    "FailedScheduling": "the scheduler could not place the pod on a node",
    "FailedMount": "an approved read-only bind could not be mounted",
    "FailedAttachVolume": "an approved read-only bind could not be attached",
    "ErrImagePull": "the pinned image could not be pulled",
    "ImagePullBackOff": "the pinned image could not be pulled",
    "ErrImageNeverPull": "the pinned image is missing from the node",
    "InvalidImageName": "the pinned image reference was rejected",
    "CreateContainerConfigError": "the container configuration was rejected",
    "CreateContainerError": "the container could not be created",
    "RunContainerError": "the container could not be started",
    "FailedCreatePodSandBox": "the pod sandbox could not be created",
    "BackOff": "the container was backed off while starting",
    "Failed": "the kubelet reported a container failure",
    "OOMKilling": "the container exceeded its memory limit",
    "OOMKilled": "the container exceeded its memory limit",
}

_MAX_STUCK_REASONS: Final = 2

_MAX_LABEL_VALUE: Final = 63
_MAX_POD_NAME: Final = 63


# Calibrated against a measured cold load, not guessed: the ASR engine reaches
# /health 200 about 240 s after the controller creates its Pod (two samples on
# MegaCube: 3m47s, 4m10s), so a 180 s budget reported ordinary loading as
# ``warmup_deadline_exceeded`` and sent whoever read it after a fault (#1250).
# This is the single definition point: the process settings and the driver settings
# both default to it, and an approved template that states ``load_budget_seconds``
# overrides both.
DEFAULT_POD_READY_TIMEOUT_SECONDS: Final = 600.0


@dataclass(frozen=True, slots=True)
class KubernetesRuntimeSettings:
    """Bounded timings and naming for one cluster-backed node runtime."""

    # The single namespace the documented Role grants; never a request field, so
    # an intent cannot place an engine outside the controller's own namespace.
    namespace: str
    # How long one prepare action may wait for the Pod to report Ready. This is
    # the transport budget; a template that declares controller-side warmup keeps
    # its own budget from ``readiness.warmup.timeout_seconds``, and a template that
    # declares ``load_budget_seconds`` overrides this one.
    pod_ready_timeout_seconds: float = DEFAULT_POD_READY_TIMEOUT_SECONDS
    poll_interval_seconds: float = 1.0
    # Deletion grace the API server hands the kubelet (the same "stop timeout"
    # the container driver passes to its daemon), and the wall-clock budget for
    # the Pod actually leaving the API afterwards (the container driver's
    # "exit wait"). A Pod still visible when that budget expires is reported as
    # stopping, never as stopped.
    stop_grace_seconds: float = 20.0
    exit_wait_seconds: float = 30.0
    name_prefix: str = "engine"
    nvidia_runtime_class_name: str = "nvidia"
    # Deployment-supplied VRAM budget. The driver never expresses the peak bound
    # as a Kubernetes resource request; `resolve_gpu_budget` turns these three
    # facts into the one ratio the engine is told, or refuses the template.
    gpu_memory_mib: int | None = None
    gpu_total_mib: int | None = None
    gpu_memory_utilization: float | None = None


class KubernetesNodeRuntime:
    """A ``NodeRuntimePort`` whose engine instances are Pods in one namespace."""

    def __init__(
        self,
        *,
        api: KubernetesPodApi,
        node_id: str,
        stop_checkpoints: StopCheckpointStore,
        settings: KubernetesRuntimeSettings,
        engine_credentials: EngineCredentials | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._api = api
        self._node_id = node_id
        self._stop_checkpoints = stop_checkpoints
        self._settings = settings
        self._engine_credentials = engine_credentials
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        # One prepare action's single deadline, anchored before any API call and
        # kept across retries so a repeated prepare cannot restart the budget.
        self._deadlines: dict[tuple[str, int], float] = {}
        # What this process observed when it created or adopted a Pod. It is a
        # record, not the fencing authority: the UID a stop must match is read
        # back from the durable checkpoint, because this map is empty after a
        # restart and a generation may never have been prepared by this process.
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
        """Pin this generation's Pod UID durably, then delete and confirm absence."""
        if request.action is not RuntimeActionKind.STOP or request.target_node_id != self._node_id:
            raise IntentConflictError("the original stop does not belong to this node")
        try:
            return self._stop(request, template)
        except (KubernetesUnavailableError, KubernetesApiError) as error:
            # An unanswered or refusing API server is neither a stopped nor a live
            # instance: the intent stays unresolved and retryable.
            raise RuntimeControllerUnavailableError(
                f"the node runtime did not complete stop: {type(error).__name__}"
            ) from error

    def recover_stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        """Resume the persisted original stop; the checkpoint, not memory, fences it."""
        return self.stop(request, template)

    def _stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        checkpoint = self._stop_checkpoints.get_stop_checkpoint(request)
        if checkpoint is None:
            pod = self._api.get_pod(self._pod_name(request))
            if pod is None:
                # Nothing recorded and nothing observed: this node cannot report
                # that it removed anything, so no release evidence is written.
                return self._observation(request, template, ObservedInstanceState.ABSENT)
            self._require_stop_identity(pod, request, template)
            # Pin before acting, exactly as the container driver does: the UID is
            # the fence every later decision of this stop is measured against.
            checkpoint = self._stop_checkpoints.save_stop_checkpoint(
                StopCheckpoint(request=request, container_id=pod.uid)
            )
        return self._resume_stop(checkpoint, template)

    def _resume_stop(
        self, checkpoint: StopCheckpoint, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        request = checkpoint.request
        name = self._pod_name(request)
        pod = self._api.get_pod(name)
        if pod is not None and pod.uid != checkpoint.container_id:
            # A Pod of this name now belongs to another UID. That is a rebuilt
            # instance, not this generation's Pod, so the pinned stop may not
            # delete it: the old generation's intent must never kill the new one.
            return self._stop_conflict(request, template)
        if pod is None:
            if checkpoint.exited_at is not None:
                return self._released_observation(checkpoint, template, observed=False)
            if checkpoint.deletion_requested_at is None:
                # Gone with no recorded exit fact and no recorded removal request:
                # do not turn "already absent" into "this stop succeeded".
                return self._observation(request, template, ObservedInstanceState.ABSENT)
            # The API accepted this stop's removal of the pinned UID and the UID is
            # gone: the generation exited, even though no exit code was published.
            checkpoint = self._record_observed_absence(checkpoint)
            return self._released_observation(checkpoint, template, observed=False)
        self._require_stop_identity(pod, request, template)
        if checkpoint.released_at is not None:
            # A released stop whose Pod is visible again: the release claim and
            # the API disagree, so report the conflict instead of re-deleting.
            return self._stop_conflict(request, template)
        if (
            checkpoint.exit_code is not None
            and pod.exit_code is not None
            and pod.exit_code != checkpoint.exit_code
        ):
            # A container's terminated exit code never changes, so a different one
            # means this Pod is no longer the runtime the checkpoint recorded.
            return self._stop_conflict(request, template)
        checkpoint = self._record_observed_exit(checkpoint, pod)
        accepted, conflict = self._request_deletion(checkpoint, template)
        if conflict is not None:
            return conflict
        if not accepted:
            # The API server had no Pod of this name: the generation was already
            # gone when the removal was requested, so its exit is not ours to
            # claim. Only absence stays reportable here, without release evidence.
            return self._observation(request, template, ObservedInstanceState.ABSENT)
        deadline = self._monotonic() + self._settings.exit_wait_seconds
        while True:
            current = self._api.get_pod(name)
            if current is None:
                break
            if current.uid != checkpoint.container_id:
                # The name changed identity while terminating; nothing else may be
                # deleted with this stop's authority.
                return self._stop_conflict(request, template)
            checkpoint = self._record_observed_exit(checkpoint, current)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                # The delete was requested and the Pod is still in the API: that
                # is a stopping instance, never a stopped one.
                return self._observation(
                    request,
                    template,
                    ObservedInstanceState.STOPPING,
                    error_code=RuntimeIntentErrorCode.INSTANCE_DRAIN_INCOMPLETE,
                )
            self._sleep(min(self._settings.poll_interval_seconds, remaining))
        if checkpoint.exited_at is None:
            # The Pod left the API after this stop's deletion was accepted. That is
            # the exit fact a cluster runtime can observe: the object carrying the
            # pinned UID is gone, so the generation's process is gone with it. The
            # exit code stays unset because the API never published one.
            checkpoint = self._record_observed_absence(checkpoint)
        return self._released_observation(checkpoint, template, observed=True)

    def _request_deletion(
        self, checkpoint: StopCheckpoint, template: ApprovedRuntimeTemplate
    ) -> tuple[bool, InstanceObservation | None]:
        """Delete the pinned Pod.

        The first member is whether the API server accepted the removal, the second
        is a conflict observation when the fence refused it.
        """
        try:
            accepted = self._api.delete_pod(
                self._pod_name(checkpoint.request),
                # Sent as the API server's own precondition so the fence holds even
                # if the Pod is replaced between the read above and this request.
                uid=checkpoint.container_id,
                grace_period_seconds=self._settings.stop_grace_seconds,
            )
        except KubernetesApiError as error:
            if error.status_code == 409:
                return False, self._stop_conflict(checkpoint.request, template)
            raise
        if not accepted:
            return False, None
        # Persist the accepted request before waiting: if this process dies while
        # the Pod is leaving, the next one can still tell our removal from a Pod
        # that was already absent when the stop arrived.
        self._stop_checkpoints.save_stop_checkpoint(
            replace(checkpoint, deletion_requested_at=self._wall_clock())
        )
        return True, None

    def _record_observed_absence(self, checkpoint: StopCheckpoint) -> StopCheckpoint:
        """Commit the exit fact the API allows us to see: the pinned Pod left."""
        if checkpoint.exited_at is not None:
            return checkpoint
        return self._stop_checkpoints.save_stop_checkpoint(
            replace(checkpoint, exited_at=self._wall_clock(), exit_code=None)
        )

    def _record_observed_exit(self, checkpoint: StopCheckpoint, pod: PodState) -> StopCheckpoint:
        """Commit the API's terminated exit code once, before any release fact."""
        if checkpoint.exited_at is not None or pod.exit_code is None:
            return checkpoint
        return self._stop_checkpoints.save_stop_checkpoint(
            replace(checkpoint, exit_code=pod.exit_code, exited_at=self._wall_clock())
        )

    def _released_observation(
        self,
        checkpoint: StopCheckpoint,
        template: ApprovedRuntimeTemplate,
        *,
        observed: bool,
    ) -> InstanceObservation:
        """Write the release fact only once the Pod is gone from the API."""
        request = checkpoint.request
        if self._api.get_pod(self._pod_name(request)) is not None:
            return self._stop_conflict(request, template)
        if checkpoint.released_at is None:
            checkpoint = self._stop_checkpoints.save_stop_checkpoint(
                replace(checkpoint, released_at=self._wall_clock())
            )
        self._deadlines.pop((request.instance_id, request.instance_generation), None)
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=request.config_digest,
            # ``stopped`` when this call watched the Pod leave, ``absent`` when it
            # was already gone; both carry the same release evidence.
            observed_instance_state=(
                ObservedInstanceState.STOPPED if observed else ObservedInstanceState.ABSENT
            ),
            observed_peak_memory=None,
            stop_evidence=checkpoint.evidence(),
            # The exit code itself is the durable fact (it travels in the stop
            # evidence); this reason only keeps a non-zero exit visible on the
            # observation instead of letting it read as a clean exit.
            error_code=(
                RuntimeIntentErrorCode.ENGINE_EXIT_CODE_NONZERO
                if checkpoint.exit_code not in (None, 0)
                else None
            ),
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
        pod: PodState,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
    ) -> None:
        """A Pod of this name is only the stop target when its identity is ours."""
        self._require_same_identity(pod, request, template)
        if (
            pod.labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE
            or pod.labels.get(NODE_ID_LABEL) != _label_value(self._node_id)
            or pod.labels.get(GENERATION_LABEL) != str(request.instance_generation)
        ):
            raise IntentConflictError("the stop target belongs to another runtime identity")

    def inspect(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        *,
        resumed: bool = False,
    ) -> InstanceObservation | None:
        """Read one generation's Pod and its events without acting on anything.

        A failed read is **not** an absent instance. An API server that refuses
        or cannot be reached raises ``RuntimeControllerUnavailableError``, exactly
        as ``prepare`` and ``stop`` do, so the caller keeps the intent unresolved
        and retries; only the API answering that this Pod does not exist is
        reported as ``absent``. That observation carries no stop evidence: a
        missing Pod is not a release fact and nothing here removed it.

        A Pod that is not serving is reported as it is, with the event reason
        that explains it when the cluster recorded one. The Pod's Ready condition
        is transport health only: for a template that declares controller-side
        warmup the instance stays ``warming_up``, exactly as it does after
        ``prepare``.

        ``resumed`` matches the port's signature. This driver reads no warmup
        progress, so a re-observed intent simply has no in-memory deadline here
        and therefore no deadline reason to report; nothing is re-anchored, so
        observation can never grant a second budget.
        """
        unsupported = self._unsupported_template(request, template)
        if unsupported is not None:
            return unsupported
        try:
            return self._inspect(request, template)
        except (KubernetesUnavailableError, KubernetesApiError) as error:
            # An unanswered or refusing API server is neither an absent instance
            # nor a stopped one: the observation stays open and retryable.
            raise RuntimeControllerUnavailableError(
                f"the node runtime did not complete inspect: {type(error).__name__}"
            ) from error

    def _inspect(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
    ) -> InstanceObservation:
        pod = self._api.get_pod(self._pod_name(request))
        if pod is None:
            return self._absent_observation(request, template)
        mismatch = self._observed_identity_mismatch(pod, request, template)
        if mismatch is not None:
            return self._observation(
                request,
                template,
                ObservedInstanceState.QUARANTINED,
                quarantine_reason=mismatch,
            )
        state, error_code = self._observed_state(request, template, pod)
        if state is ObservedInstanceState.READY:
            # Ready means "this is the engine we approved", on every path. This
            # sweep used to skip that proof, so a Pod running bytes the template
            # never approved was reported ready one sweep after prepare refused it:
            # the refusal looked like a hiccup that healed itself (#1270).
            unapproved = self._require_approved_image(request, template, pod)
            if unapproved is not None:
                return unapproved
        return self._observation(
            request,
            template,
            state,
            error_code=error_code,
            quarantine_reason=(
                self._stuck_reason(pod) if _is_event_explained(state, pod) else None
            ),
        )

    def _observed_identity_mismatch(
        self,
        pod: PodState,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
    ) -> str | None:
        """Why the Pod holding this name is not this generation's, or ``None``.

        The name is a discovery key, not an identity. The annotations carry the
        exact intent identity, and a UID this process recorded when it created
        the Pod is the only proof that the Pod is still that creation: a rebuilt
        Pod of the same name has a different UID even if it copied the labels.
        """
        created_uid = self._pod_uids.get((request.instance_id, request.instance_generation))
        if created_uid is not None and pod.uid != created_uid:
            return (
                "the engine pod name now belongs to another identity than the one "
                "this controller created"
            )
        if (
            pod.labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE
            or pod.labels.get(NODE_ID_LABEL) != _label_value(self._node_id)
            or pod.labels.get(GENERATION_LABEL) != str(request.instance_generation)
        ):
            return "the pod at this name belongs to another runtime identity"
        if (
            pod.annotations.get(INSTANCE_ID_ANNOTATION) != request.instance_id
            or pod.annotations.get(TEMPLATE_ID_ANNOTATION) != template.template_id
            or pod.annotations.get(CONFIG_DIGEST_ANNOTATION) != request.config_digest
        ):
            return "the pod at this name carries another instance identity or configuration digest"
        return None

    def _observed_state(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        pod: PodState,
    ) -> tuple[ObservedInstanceState, RuntimeIntentErrorCode | None]:
        """Map the Pod's own facts onto the controller's observation vocabulary.

        Nothing is inferred beyond what the API reports. A Pod whose phase this
        driver does not interpret is an instance that exists and has not reached
        a runnable state, not a failure; a Pod being removed is stopping even if
        its last condition still said Ready.
        """
        if pod.deleting:
            return ObservedInstanceState.STOPPING, None
        if pod.exit_code is not None:
            if pod.exit_code == 0:
                return ObservedInstanceState.STOPPED, None
            return ObservedInstanceState.FAILED, RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED
        if pod.phase == "Succeeded":
            return ObservedInstanceState.STOPPED, None
        if pod.phase == "Failed":
            return ObservedInstanceState.FAILED, RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED
        if pod.phase == "Running":
            if not pod.ready:
                return ObservedInstanceState.LOADING, self._deadline_reason(request)
            if template.readiness.kind is ReadinessKind.HTTP_WARMUP_OK:
                # §3.3: the Pod probe is the transport half only. The controller
                # still owns the warmup decision, and this driver never performs
                # that warmup, so a ready Pod is warming_up and never ready.
                return ObservedInstanceState.WARMING_UP, self._deadline_reason(request)
            return ObservedInstanceState.READY, None
        if pod.phase == "Pending":
            # Created but not yet running: scheduling, image pull and container
            # creation all report Pending. That is the same intermediate state
            # ``prepare`` reports as ``loading`` for this Pod, so inspecting it
            # must not name it differently.
            return ObservedInstanceState.LOADING, self._deadline_reason(request)
        # A Pod that exists while the API reports no interpretable phase: the
        # instance exists, and what it is doing is simply not stated yet.
        return ObservedInstanceState.PREPARING, self._deadline_reason(request)

    def _deadline_reason(
        self, request: RuntimeInstanceIntentRequest
    ) -> RuntimeIntentErrorCode | None:
        """Name an exhausted prepare budget without changing the observation.

        Only an anchor this process already holds can be measured. An intent the
        controller merely re-observed after a restart has none, and observing it
        must not create one: the reason would otherwise be invented by the very
        read that reports it.
        """
        deadline = self._deadlines.get((request.instance_id, request.instance_generation))
        if deadline is not None and self._monotonic() >= deadline:
            return RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED
        return None

    def _stuck_reason(self, pod: PodState) -> str | None:
        """Say why a Pod that never started or already ended failed, from its events.

        Only the event *reason* token is read and only through a fixed
        vocabulary: an event message carries node names, image references and
        free text written by other cluster components, and none of that may reach
        an observation. An event the vocabulary does not classify contributes a
        bounded statement instead of its token.

        Callers only ask for a running Pod's events when that Pod has not started
        or has ended (see ``_is_event_explained``). Events are history: a warning
        raised before a Pod started says nothing about why it is not ready now,
        and repeating it would attribute an old failure to the current state.

        A failed event read is not a failed observation: the Pod's own facts
        stand on their own, so losing the explanation never turns into reporting
        something else about the instance.
        """
        try:
            events = self._api.list_pod_events(pod.name, uid=pod.uid)
        except KubernetesApiError, KubernetesUnavailableError:
            return None
        reasons: list[str] = []
        saw_unclassified = False
        for event in events:
            if event.event_type != "Warning":
                continue
            phrase = EVENT_FAILURE_REASONS.get(event.reason)
            if phrase is None:
                saw_unclassified = True
                continue
            entry = f"{phrase} ({event.reason})"
            if entry not in reasons:
                reasons.append(entry)
        if reasons:
            return "the engine pod is not serving: " + "; ".join(reasons[:_MAX_STUCK_REASONS])
        if saw_unclassified:
            return (
                "the engine pod is not serving: a cluster event reported a failure "
                "this controller does not classify"
            )
        return None

    @staticmethod
    def _absent_observation(
        request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        """The generation has no Pod; nothing was removed, so nothing is proven."""
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=template.template_id,
            config_digest=request.config_digest,
            observed_instance_state=ObservedInstanceState.ABSENT,
            stop_evidence=None,
        )

    def _unsupported_template(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        """Refuse a template this deployment cannot represent, without acting."""
        if template.network_name is not None:
            return self._observation(
                request,
                template,
                ObservedInstanceState.FAILED,
                error_code=RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED,
                quarantine_reason=(
                    "unsupported template field: network_name has no Kubernetes "
                    "equivalent; silently ignoring it would widen the isolation the "
                    "approved template declares, so this driver refuses the template "
                    "instead"
                ),
            )
        try:
            self._gpu_budget(template)
        except GpuBudgetUndeclaredError as error:
            return self._observation(
                request,
                template,
                ObservedInstanceState.FAILED,
                error_code=RuntimeIntentErrorCode.GPU_BUDGET_UNDECLARED,
                quarantine_reason=error.reason,
                nothing_was_created=True,
            )
        return None

    def _gpu_budget(self, template: ApprovedRuntimeTemplate) -> GpuBudget | None:
        """The VRAM bound this deployment states for this template, or a refusal."""
        return resolve_gpu_budget(
            gpu_device_count=template.gpu_device_count,
            budget_mib=self._settings.gpu_memory_mib,
            total_mib=self._settings.gpu_total_mib,
            utilization=self._settings.gpu_memory_utilization,
        )

    def _require_approved_image(
        self,
        request: RuntimeInstanceIntentRequest,
        template: ApprovedRuntimeTemplate,
        observed: PodState,
    ) -> InstanceObservation | None:
        """Prove the running bytes are the approved ones before ready is claimed.

        ``docker_runtime`` proves the same thing by inspecting the image *before*
        it starts the container. A Pod must exist before the runtime reports which
        image it installed, so here the proof lands on the first observation that
        carries an image id. A Pod that never reports one is never called ready:
        ready means "this is the engine we approved", and an unproven image is not
        that. The Pod is left in place so the mismatch stays diagnosable; the
        observation is quarantined, which is what stops the intent from resolving.
        """
        if observed.image_id is not None and (
            _image_digest_of(observed.image_id) == template.image_digest
        ):
            return None
        running = _image_digest_of(observed.image_id) if observed.image_id else "unknown"
        return self._observation(
            request,
            template,
            ObservedInstanceState.QUARANTINED,
            quarantine_reason=(
                f"the container runs image {running}, not the approved digest "
                f"{template.image_digest}"
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
                approved = self._require_approved_image(request, template, observed)
                if approved is not None:
                    return approved
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
        nothing_was_created: bool = False,
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
            nothing_was_created=nothing_was_created,
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
        # The budget answers "how long may this model take to become reachable",
        # which is a fact about the model and the hardware, so the approved template
        # states it. Falling back to the deployment default keeps a template that
        # declares nothing working, but a cold load longer than that default turns
        # ordinary progress into a reported fault (#1250).
        budget = (
            warmup.timeout_seconds
            if warmup is not None
            else template.load_budget_seconds or self._settings.pod_ready_timeout_seconds
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
            "image": _image_reference(template),
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
            # The approved template's argv replaces the image CMD, not its
            # ENTRYPOINT: ``docker_runtime`` passes it as the Docker API ``Cmd``
            # field and keeps the image entrypoint.  Kubernetes ``command`` is
            # the entrypoint, so the same argv belongs in ``args`` and
            # ``command`` must stay unset.  The deployed ASR template relies on
            # this: its argv starts with ``--model`` because the image entrypoint
            # is ``vllm serve``.
            container["args"] = list(command)
        volumes: list[dict[str, Any]] = []
        mounts: list[dict[str, Any]] = []
        for index, bind in enumerate(template.read_only_binds):
            # Names the volume, never the Pod: ``name`` is the Pod's own name and
            # rebinding it here renamed every Pod after its last read-only bind.
            volume_name = f"approved-bind-{index}"
            volumes.append(
                {"name": volume_name, "hostPath": {"path": bind.source, "type": "Directory"}}
            )
            mounts.append({"name": volume_name, "mountPath": bind.target, "readOnly": True})
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
        budget = self._gpu_budget(template)
        if budget is None or "--gpu-memory-utilization" in command:
            return command
        return (*command, *budget.argument)

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


def _is_event_explained(state: ObservedInstanceState, pod: PodState) -> bool:
    """Whether an event can explain this observation at all.

    Only a Pod that never started (its phase is still Pending) or that already
    ended can be explained by an event. A running Pod's warnings are history.
    """
    if state is ObservedInstanceState.FAILED:
        return True
    return state is ObservedInstanceState.LOADING and pod.phase == "Pending"


def _image_reference(template: ApprovedRuntimeTemplate) -> str:
    """The reference the Pod runs, exactly as the template approved it.

    ``docker_runtime`` does the same: it starts the container from
    ``image_reference`` and then proves the identity by inspecting the image and
    comparing its id against ``image_digest``.  A digest reference would be a
    different image name to the container runtime, so a locally built image that
    lives only in the node's store cannot be resolved by it at all
    (``docker.io/local/...@sha256:...`` is not ``docker.io/local/...:tag``).
    The invariant is therefore enforced on the running container's image id, not
    on the spelling of the reference -- see ``_require_approved_image``.
    """
    return template.image_reference


def _image_digest_of(image_id: str) -> str:
    """The ``sha256:...`` part of a container status imageID, or the input."""
    marker = image_id.rfind("sha256:")
    return image_id[marker:] if marker >= 0 else image_id


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
