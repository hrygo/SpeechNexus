"""Cluster adapter inspect: Pod phase and events, never read as presence.

Every test drives the real ``KubernetesApi`` over ``httpx.MockTransport`` against
the scripted API server in ``tests/fake/kubernetes_cluster.py``: no cluster and no
container daemon is involved. The tests are built around the mutations this slice
exists to catch:

* treating an API error as "the Pod does not exist", which would make a network
  fault look like a vanished instance;
* inventing stop evidence for an absent Pod, i.e. claiming a release that never
  happened;
* swallowing the event reason, so a stuck Pod only ever reports loading and an
  operator has to read kubelet logs to learn why;
* turning the Pod's Ready condition into instance readiness, which bypasses the
  controller-side readiness rule it only transports.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from tests.fake.controller import (
    SAMPLE_INSTANCE_ID,
    InMemoryStopCheckpoints,
    build_request,
    build_template,
)
from tests.fake.kubernetes_cluster import (
    NAMESPACE,
    REPLACEMENT_POD_UID,
    FakeClock,
    FakeKubernetesCluster,
    api_for,
    mark_deleting,
    mark_running,
    mark_terminated,
    pod_from_manifest,
)

from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    ObservedInstanceState,
    RuntimeControllerUnavailableError,
    RuntimeIntentErrorCode,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ReadinessKind,
    ReadinessRule,
    WarmupRequest,
)
from speechnexus_node_controller.infrastructure.kubernetes_runtime import (
    KubernetesNodeRuntime,
    KubernetesRuntimeSettings,
)

NODE_ID = "node-local-1"
POD_NAME = f"engine-{SAMPLE_INSTANCE_ID}-1"
PREPARE = build_request()
# Deploy text that must never reach an observation: a node name and a registry
# address are exactly what an event message carries.
NODE_NAME = "cube-ccba"
IMAGE_ADDRESS = "registry.internal:5000/asr/engine:0.1.0"
_WARMUP_BODY = b"synthetic-warmup-body"


def _runtime(
    tmp_path: Path,
    cluster: FakeKubernetesCluster,
    **settings: Any,
) -> tuple[KubernetesNodeRuntime, FakeClock]:
    clock = FakeClock()
    runtime = KubernetesNodeRuntime(
        api=api_for(tmp_path, cluster),
        node_id=NODE_ID,
        stop_checkpoints=InMemoryStopCheckpoints(),
        settings=KubernetesRuntimeSettings(namespace=NAMESPACE, **settings),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return runtime, clock


def _install_pod(
    cluster: FakeKubernetesCluster,
    runtime: KubernetesNodeRuntime,
    template: ApprovedRuntimeTemplate | None = None,
) -> dict[str, Any]:
    """Put this generation's Pod in the API, built from the driver's own manifest."""
    resolved = template if template is not None else build_template()
    manifest = runtime._pod_manifest(PREPARE, resolved, POD_NAME)
    pod = pod_from_manifest(manifest)
    cluster.pods[POD_NAME] = pod
    return pod


def _published(observation: InstanceObservation) -> str:
    """Everything an observation would publish, as one searchable string."""
    return json.dumps(asdict(observation), default=str)


def test_inspect_reports_an_absent_instance_without_inventing_stop_evidence(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    # No Pod was found, and nothing was removed: a release fact here would claim
    # a stop that never happened.
    assert observation.stop_evidence is None
    assert observation.error_code is None
    assert observation.native_runtime_evidence is None


def test_an_api_read_failure_is_never_reported_as_an_absent_instance(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.pod_read_status = 500
    runtime, _clock = _runtime(tmp_path, cluster)

    with pytest.raises(RuntimeControllerUnavailableError):
        runtime.inspect(PREPARE, build_template())


def test_inspect_reports_a_pending_pod_as_loading(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    _install_pod(cluster, runtime)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    # Pending is the same intermediate state ``prepare`` reports as loading for
    # this Pod: both paths must name it the same way.
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    # An intermediate state with no event carries no invented reason.
    assert observation.error_code is None
    assert observation.quarantine_reason is None


def test_inspect_reports_a_pod_with_no_interpretable_phase_as_preparing(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    pod = _install_pod(cluster, runtime)
    # The Pod exists but the API states nothing about what it is doing.
    pod["status"] = {}

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.PREPARING
    assert observation.quarantine_reason is None


def test_inspect_reports_a_running_pod_that_is_not_ready_as_loading(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    pod = _install_pod(cluster, runtime)
    mark_running(pod, ready=False)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.LOADING


def test_inspect_reports_a_transport_ready_pod_as_ready_for_a_process_rule(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    mark_running(_install_pod(cluster, runtime), ready=True)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.READY


def test_inspect_never_turns_a_ready_pod_into_a_ready_instance_for_warmup(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    template = build_template(readiness=_warmup_readiness(tmp_path))
    mark_running(_install_pod(cluster, runtime, template), ready=True)

    observation = runtime.inspect(PREPARE, template)

    assert observation is not None
    # §3.3: the Pod probe proves the transport layer only. Readiness stays the
    # controller's decision, and this driver never runs the warmup, so a ready
    # Pod is warming_up and never ready.
    assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert observation.native_runtime_evidence is None


def test_inspect_reports_a_failed_pod_as_failed_not_as_loading(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    cluster.terminal_phase = "Failed"
    cluster.terminal_exit_code = 1
    runtime, _clock = _runtime(tmp_path, cluster)
    _install_pod(cluster, runtime)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.FAILED
    assert observation.error_code is RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED


def test_inspect_names_the_event_that_explains_a_pod_that_never_started(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    _install_pod(cluster, runtime)
    cluster.record_event(
        POD_NAME,
        reason="ImagePullBackOff",
        message=f"Failed to pull image {IMAGE_ADDRESS} on node {NODE_NAME}",
    )

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    reason = observation.quarantine_reason
    assert reason is not None
    # The observer can see why the Pod is stuck instead of guessing from "loading".
    assert "ImagePullBackOff" in reason
    assert "could not be pulled" in reason
    published = _published(observation)
    # An event message is cluster text: the node name and image address stay out.
    assert NODE_NAME not in published
    assert IMAGE_ADDRESS not in published


def test_inspect_ignores_an_event_recorded_for_another_pod_identity(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    _install_pod(cluster, runtime)
    # The same name already hosted another Pod generation: its failure explains
    # that generation, not this one.
    cluster.record_event(
        POD_NAME,
        reason="ImagePullBackOff",
        uid=REPLACEMENT_POD_UID,
        message=f"Failed to pull image {IMAGE_ADDRESS}",
    )

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    assert observation.quarantine_reason is None


def test_inspect_describes_an_unclassified_warning_without_repeating_it(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    _install_pod(cluster, runtime)
    cluster.record_event(
        POD_NAME,
        reason="SyntheticUnexplainedReason",
        message=f"something happened on node {NODE_NAME} at {IMAGE_ADDRESS}",
    )

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    reason = observation.quarantine_reason
    assert reason is not None
    assert "does not classify" in reason
    assert "SyntheticUnexplainedReason" not in reason
    assert NODE_NAME not in _published(observation)


def test_inspect_keeps_the_pod_observation_when_the_event_read_fails(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    cluster.event_status = 403
    runtime, _clock = _runtime(tmp_path, cluster)
    _install_pod(cluster, runtime)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    # Losing the explanation is not a reason to lose the observation: the Pod's
    # own facts still say the instance is loading.
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    assert observation.quarantine_reason is None


def test_inspect_does_not_repeat_a_running_pods_past_warning_as_its_reason(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    pod = _install_pod(cluster, runtime)
    mark_running(pod, ready=False)
    # Recorded before the container started; it no longer explains a Pod that is
    # now running, so attributing it to the current state would be a stale claim.
    cluster.record_event(POD_NAME, reason="ImagePullBackOff")

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    assert observation.quarantine_reason is None


def test_inspect_quarantines_a_same_named_pod_with_another_identity(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, _clock = _runtime(tmp_path, cluster)
    pod = _install_pod(cluster, runtime)
    mark_running(pod, ready=True)
    # Same name, another configuration digest: adopting it would report this
    # generation ready on another generation's Pod.
    pod["metadata"]["annotations"]["speechnexus.io/config-digest"] = "sha256:" + "e" * 64

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observation.quarantine_reason is not None


def test_inspect_quarantines_a_pod_rebuilt_under_the_same_name(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, _clock = _runtime(tmp_path, cluster)
    prepared = runtime.prepare(PREPARE, build_template())
    assert prepared.observed_instance_state is ObservedInstanceState.READY
    # The Pod this controller created left and another UID took its name while
    # keeping the labels; the recorded UID is the only proof of identity.
    cluster.replacement_uid = REPLACEMENT_POD_UID

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observation.quarantine_reason is not None
    assert "another identity" in observation.quarantine_reason


def test_inspect_reports_a_deleting_pod_as_stopping_not_ready(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    pod = _install_pod(cluster, runtime)
    mark_running(pod, ready=True)
    # A deleted Pod is served until its grace period ends and can still carry a
    # stale Ready condition; it is leaving, so it is not ready.
    mark_deleting(pod)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.STOPPING


def test_inspect_reports_a_succeeded_pod_as_stopped_without_stop_evidence(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(tmp_path, cluster)
    mark_terminated(_install_pod(cluster, runtime), 0)

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.STOPPED
    # Read-only observation proves no release of its own.
    assert observation.stop_evidence is None


def test_inspect_reports_an_exhausted_prepare_budget_as_its_reason(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(
        tmp_path, cluster, pod_ready_timeout_seconds=2.0, poll_interval_seconds=1.0
    )
    prepare = runtime.prepare(PREPARE, build_template())
    assert prepare.observed_instance_state is ObservedInstanceState.LOADING
    assert prepare.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED

    observation = runtime.inspect(PREPARE, build_template())

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    assert observation.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED


def test_inspect_refuses_an_unsupported_template_without_reading_the_api(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    observation = runtime.inspect(
        PREPARE, build_template(network_name="synthetic-internal-network")
    )

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.FAILED
    assert observation.error_code is RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED
    # The driver refused the template, so there is no Pod to read.
    assert cluster.requests == []


def _warmup_readiness(tmp_path: Path) -> ReadinessRule:
    path = tmp_path / "warmup.wav"
    path.write_bytes(_WARMUP_BODY)
    return ReadinessRule(
        kind=ReadinessKind.HTTP_WARMUP_OK,
        port=9000,
        warmup=WarmupRequest(
            path="/v1/audio/transcriptions",
            content_type="audio/wav",
            body_path=str(path),
            body_sha256="sha256:" + hashlib.sha256(_WARMUP_BODY).hexdigest(),
            timeout_seconds=30.0,
        ),
    )
