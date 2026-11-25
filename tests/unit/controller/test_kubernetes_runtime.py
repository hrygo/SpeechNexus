"""Cluster adapter: one digest-pinned Pod per generation, readiness from the Pod.

Every test drives the real ``KubernetesApi`` over ``httpx.MockTransport``: the
fake below is a scripted API server, so the assertions cover the JSON the driver
would really submit (image pin, resources, probes, environment) and the requests
it would really make. No cluster and no container daemon is involved.

The mutations these tests are built to catch:

* returning ``ready`` without waiting for the Pod's Ready condition;
* treating an API error while creating the Pod as a prepared instance;
* ignoring ``network_name`` and creating the Pod anyway;
* pinning ``image`` by tag instead of digest;
* writing an ``nvidia.com/gpu`` resource request, which no device plugin on this
  cluster satisfies, so the Pod would stay Pending forever.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from tests.fake.controller import (
    GIB,
    IMAGE_DIGEST,
    SAMPLE_IMAGE_REFERENCE,
    SAMPLE_INSTANCE_ID,
    InMemoryStopCheckpoints,
    build_request,
    build_template,
)
from tests.fake.kubernetes_cluster import (
    NAMESPACE,
    POD_UID,
    FakeClock,
    FakeKubernetesCluster,
    api_for,
    pod_from_manifest,
)

from speechnexus_node_controller.domain.intents import (
    IntentConflictError,
    ObservedInstanceState,
    RuntimeControllerUnavailableError,
    RuntimeIntentErrorCode,
)
from speechnexus_node_controller.domain.templates import (
    EngineAuthKind,
    ReadinessKind,
    ReadinessRule,
    ReadOnlyBind,
    WarmupRequest,
)
from speechnexus_node_controller.infrastructure.engine_credentials import EngineCredentials
from speechnexus_node_controller.infrastructure.kubernetes_runtime import (
    CONFIG_DIGEST_ANNOTATION,
    GENERATION_LABEL,
    INSTANCE_ID_ANNOTATION,
    INSTANCE_ID_LABEL,
    TEMPLATE_ID_ANNOTATION,
    KubernetesNodeRuntime,
    KubernetesRuntimeSettings,
)

NODE_ID = "node-local-1"
POD_NAME = f"engine-{SAMPLE_INSTANCE_ID}-1"


_Clock = FakeClock
_FakeCluster = FakeKubernetesCluster
_pod_from = pod_from_manifest


def _runtime(
    tmp_path: Path,
    cluster: _FakeCluster,
    *,
    credentials: EngineCredentials | None = None,
    clock: _Clock | None = None,
    checkpoints: InMemoryStopCheckpoints | None = None,
    **settings: Any,
) -> tuple[KubernetesNodeRuntime, _Clock]:
    resolved = _Clock() if clock is None else clock
    api = api_for(tmp_path, cluster)
    runtime = KubernetesNodeRuntime(
        api=api,
        node_id=NODE_ID,
        stop_checkpoints=checkpoints or InMemoryStopCheckpoints(),
        engine_credentials=credentials,
        settings=KubernetesRuntimeSettings(namespace=NAMESPACE, **settings),
        sleep=resolved.sleep,
        monotonic=resolved.monotonic,
    )
    return runtime, resolved


def test_prepare_creates_one_pod_for_the_generation_and_reports_ready(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    request = build_request()
    runtime, _clock = _runtime(tmp_path, cluster)

    observation = runtime.prepare(request, build_template())

    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert observation.native_runtime_evidence is None
    assert len(cluster.created) == 1
    body = cluster.created[0]
    assert body["kind"] == "Pod"
    assert body["metadata"]["name"] == POD_NAME
    assert body["metadata"]["namespace"] == NAMESPACE
    assert body["metadata"]["labels"][INSTANCE_ID_LABEL] == SAMPLE_INSTANCE_ID
    assert body["metadata"]["labels"][GENERATION_LABEL] == "1"
    assert body["metadata"]["annotations"][INSTANCE_ID_ANNOTATION] == SAMPLE_INSTANCE_ID
    assert body["metadata"]["annotations"][TEMPLATE_ID_ANNOTATION] == request.approved_template_id
    assert body["metadata"]["annotations"][CONFIG_DIGEST_ANNOTATION] == request.config_digest
    container = body["spec"]["containers"][0]
    assert container["resources"] == {"limits": {"memory": str(GIB)}}
    assert body["spec"]["restartPolicy"] == "Never"
    # The engine receives no API credential; only the controller has one.
    assert body["spec"]["automountServiceAccountToken"] is False
    # The Pod UID is the fencing anchor recorded the moment the API answered.
    assert runtime._pod_uids[(request.instance_id, request.instance_generation)] == POD_UID


def test_prepare_waits_for_the_pod_ready_condition_before_reporting_ready(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    cluster.ready_after_gets = 3
    runtime, clock = _runtime(tmp_path, cluster)

    observation = runtime.prepare(build_request(), build_template())

    assert observation.observed_instance_state is ObservedInstanceState.READY
    # One existence lookup plus two Pending polls before the Ready condition
    # appeared. Returning ready without waiting would leave both counters at zero.
    assert cluster.gets == 3
    assert len(clock.sleeps) == 2


def test_prepare_reports_loading_and_a_reason_when_the_budget_expires(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    cluster.ready_after_gets = None
    runtime, _clock = _runtime(
        tmp_path,
        cluster,
        pod_ready_timeout_seconds=2.5,
        poll_interval_seconds=1.0,
    )

    observation = runtime.prepare(build_request(), build_template())

    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    assert observation.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED


def test_prepare_never_reports_ready_when_the_api_refuses_creation(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    cluster.create_status = 422
    runtime, _clock = _runtime(tmp_path, cluster)

    observed: Any = None
    with contextlib.suppress(RuntimeControllerUnavailableError):
        observed = runtime.prepare(build_request(), build_template())
    assert observed is None, f"a refused create was observed as {observed}"

    assert len(cluster.created) == 1
    # Only the pre-create existence lookup happened; a refused create is never
    # observed as a Pod and never reported ready.
    assert cluster.gets == 1


def test_prepare_refuses_a_template_that_declares_a_dedicated_network(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    observation = runtime.prepare(
        build_request(), build_template(network_name="synthetic-internal-network")
    )

    # Nothing was created and no API call was made: the driver refuses the
    # template instead of silently widening the isolation it declares.
    assert cluster.created == []
    assert cluster.requests == []
    assert observation.observed_instance_state is ObservedInstanceState.FAILED
    assert observation.error_code is RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED
    assert observation.quarantine_reason is not None
    assert "unsupported" in observation.quarantine_reason
    assert "network_name" in observation.quarantine_reason


def test_prepare_runs_the_approved_reference_and_proves_it_by_digest(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    runtime.prepare(build_request(), build_template())

    # The reference is the approved one, exactly as docker_runtime passes it: a
    # digest reference names a different image to the runtime, so a locally built
    # image that only exists in the node's store can never be resolved by it.
    container = cluster.created[0]["spec"]["containers"][0]
    assert container["image"] == SAMPLE_IMAGE_REFERENCE


def test_prepare_never_reports_ready_for_an_image_that_is_not_the_approved_one(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    # A pod that started some other build: the tag was repointed, the node had a
    # stale layer, or the image was replaced under the same name.
    cluster.running_image_id = "docker.io/local/synthetic-engine@sha256:" + "d" * 64
    runtime, _clock = _runtime(tmp_path, cluster)

    observation = runtime.prepare(build_request(), build_template())

    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert IMAGE_DIGEST in (observation.quarantine_reason or "")


def test_prepare_uses_the_gpu_through_the_runtime_class_never_as_a_resource(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    runtime.prepare(build_request(), build_template(gpu_device_count=1))

    body = cluster.created[0]
    container = body["spec"]["containers"][0]
    assert body["spec"]["runtimeClassName"] == "nvidia"
    environment = {entry["name"]: entry["value"] for entry in container["env"]}
    assert environment["NVIDIA_VISIBLE_DEVICES"] == "all"
    assert environment["NVIDIA_DRIVER_CAPABILITIES"] == "compute,utility"
    # This cluster exposes the GPU through the RuntimeClass handler, not a device
    # plugin: a nvidia.com/gpu request would leave the Pod Pending forever.
    assert "requests" not in container["resources"]
    assert "nvidia.com/gpu" not in json.dumps(body)


def test_prepare_carries_the_approved_argv_binds_and_transport_probe(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    runtime.prepare(
        build_request(),
        build_template(
            command=("/usr/bin/engine", "--model", "/models/asr", "--port", "9000"),
            readiness=ReadinessRule(kind=ReadinessKind.HTTP_STATUS_OK, port=9000, path="/health"),
            read_only_binds=(ReadOnlyBind(source="/models/asr", target="/models/asr"),),
        ),
    )

    body = cluster.created[0]
    container = body["spec"]["containers"][0]
    # A read-only bind names its volume; it must never rename the Pod.  The Pod
    # name is the instance identity the driver later looks itself up by, so a
    # bind that rebinds it makes every instance collide on one name.
    assert body["metadata"]["name"] == POD_NAME
    assert body["spec"]["volumes"][0]["name"] == "approved-bind-0"
    # The template argv replaces the image CMD, never its ENTRYPOINT: Docker
    # passes it as ``Cmd`` and k8s spells that ``args``.  ``command`` must stay
    # absent so the image entrypoint still runs -- the deployed ASR template
    # starts at ``--model`` precisely because ``vllm serve`` is its entrypoint.
    assert "command" not in container
    assert container["args"] == ["/usr/bin/engine", "--model", "/models/asr", "--port", "9000"]
    assert container["readinessProbe"]["httpGet"] == {"path": "/health", "port": 9000}
    volume = body["spec"]["volumes"][0]
    assert volume["hostPath"] == {"path": "/models/asr", "type": "Directory"}
    mount = container["volumeMounts"][0]
    assert mount["mountPath"] == "/models/asr"
    assert mount["readOnly"] is True


def test_prepare_reuses_its_own_pod_instead_of_creating_a_second_one(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    request = build_request()
    runtime, _clock = _runtime(tmp_path, cluster)
    first = runtime.prepare(request, build_template())

    second = runtime.prepare(request, build_template())

    assert len(cluster.created) == 1
    assert second.observed_instance_state is ObservedInstanceState.READY
    assert first.observed_instance_state is second.observed_instance_state


def test_prepare_never_adopts_a_same_named_pod_with_another_identity(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    request = build_request()
    template = build_template()
    # A Pod of this name exists, but it belongs to another configuration digest:
    # adopting it would report this generation prepared on someone else's Pod.
    cluster.pods[POD_NAME] = _pod_from(
        {
            "metadata": {
                "name": POD_NAME,
                "namespace": NAMESPACE,
                "labels": {INSTANCE_ID_LABEL: SAMPLE_INSTANCE_ID},
                "annotations": {
                    INSTANCE_ID_ANNOTATION: SAMPLE_INSTANCE_ID,
                    TEMPLATE_ID_ANNOTATION: request.approved_template_id,
                    CONFIG_DIGEST_ANNOTATION: "sha256:" + "e" * 64,
                },
            },
            "spec": {},
        }
    )
    runtime, _clock = _runtime(tmp_path, cluster)

    with pytest.raises(IntentConflictError):
        runtime.prepare(request, template)

    assert cluster.created == []


def test_prepare_reports_a_terminated_pod_as_failed(tmp_path: Path) -> None:
    cluster = _FakeCluster()
    cluster.terminal_phase = "Failed"
    cluster.terminal_exit_code = 1
    runtime, _clock = _runtime(tmp_path, cluster)

    observation = runtime.prepare(build_request(), build_template())

    assert observation.observed_instance_state is ObservedInstanceState.FAILED
    assert observation.error_code is RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED


def test_prepare_quarantines_a_pod_replaced_by_another_identity(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    cluster.ready_after_gets = None
    cluster.replacement_uid = "pod-uid-2"
    runtime, _clock = _runtime(
        tmp_path, cluster, pod_ready_timeout_seconds=30.0, poll_interval_seconds=1.0
    )

    observation = runtime.prepare(build_request(), build_template())

    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observation.quarantine_reason is not None


def test_a_pod_ready_observation_is_not_instance_readiness_for_warmup(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    runtime, _clock = _runtime(tmp_path, cluster)
    warmup = WarmupRequest(
        path="/v1/audio/transcriptions",
        content_type="audio/wav",
        body_path=str(_warmup_body(tmp_path)),
        body_sha256=_warmup_digest(),
        timeout_seconds=30.0,
    )

    observation = runtime.prepare(
        build_request(),
        build_template(
            command=("/usr/bin/engine", "--port", "9000"),
            readiness=ReadinessRule(kind=ReadinessKind.HTTP_WARMUP_OK, port=9000, warmup=warmup),
        ),
    )

    # Existence lookup, then one poll that sees the Pod Ready: the transport
    # layer is up, so the only remaining state is controller-side warmup.
    assert cluster.gets == 2
    assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert observation.error_code is None


def test_prepare_supplies_the_per_generation_native_credential(
    tmp_path: Path,
) -> None:
    cluster = _FakeCluster()
    credentials = EngineCredentials("a" * 64)
    runtime, _clock = _runtime(tmp_path, cluster, credentials=credentials)
    request = build_request()
    template = build_template(
        command=("/usr/bin/engine", "--port", "9000"),
        engine_auth=EngineAuthKind.VLLM_API_KEY_V1,
        readiness=ReadinessRule(
            kind=ReadinessKind.HTTP_WARMUP_OK,
            port=9000,
            warmup=WarmupRequest(
                path="/v1/audio/transcriptions",
                content_type="audio/wav",
                body_path=str(_warmup_body(tmp_path)),
                body_sha256=_warmup_digest(),
                timeout_seconds=30.0,
            ),
        ),
    )

    runtime.prepare(request, template)

    container = cluster.created[0]["spec"]["containers"][0]
    environment = {entry["name"]: entry["value"] for entry in container["env"]}
    assert environment["VLLM_API_KEY"] == credentials.for_instance(request)
    evidence = runtime._native_evidence(request, template)
    assert evidence is not None
    assert evidence.native_auth_kind == "vllm_api_key_v1"
    assert evidence.image_digest == IMAGE_DIGEST


_WARMUP_BODY = b"synthetic-warmup-body"


def _warmup_body(tmp_path: Path) -> Path:
    path = tmp_path / "warmup.wav"
    path.write_bytes(_WARMUP_BODY)
    return path


def _warmup_digest() -> str:
    return "sha256:" + hashlib.sha256(_WARMUP_BODY).hexdigest()
