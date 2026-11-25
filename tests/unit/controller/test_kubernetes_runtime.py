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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.fake.controller import (
    GIB,
    IMAGE_DIGEST,
    SAMPLE_IMAGE_REFERENCE,
    SAMPLE_INSTANCE_ID,
    build_request,
    build_template,
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
from speechnexus_node_controller.infrastructure.kubernetes_api import (
    KubernetesApi,
    KubernetesApiSettings,
)
from speechnexus_node_controller.infrastructure.kubernetes_runtime import (
    CONFIG_DIGEST_ANNOTATION,
    GENERATION_LABEL,
    INSTANCE_ID_ANNOTATION,
    INSTANCE_ID_LABEL,
    TEMPLATE_ID_ANNOTATION,
    KubernetesNodeRuntime,
    KubernetesRuntimeSettings,
)

NAMESPACE = "speechnexus"
SERVICE_ACCOUNT = "speechnexus-node-controller"
API_URL = "https://kubernetes.test"
NODE_ID = "node-local-1"
POD_NAME = f"engine-{SAMPLE_INSTANCE_ID}-1"
POD_UID = "pod-uid-1"


class _Clock:
    """A monotonic clock the tests advance instead of sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeCluster:
    """A scripted Kubernetes API server for the pod endpoints only."""

    def __init__(self) -> None:
        self.pods: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.gets = 0
        self.create_status = 201
        # None means "never Ready": the readiness condition never appears.
        self.ready_after_gets: int | None = 1
        self.terminal_phase: str | None = None
        self.terminal_exit_code: int | None = None
        self.replacement_uid: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/version":
            return httpx.Response(200, json={"gitVersion": "v1.35.5"})
        if request.method == "POST" and path == f"/api/v1/namespaces/{NAMESPACE}/pods":
            body = json.loads(request.content)
            self.created.append(body)
            if self.create_status >= 400:
                return httpx.Response(self.create_status, json={"message": "pod create rejected"})
            pod = _pod_from(body)
            self.pods[body["metadata"]["name"]] = pod
            return httpx.Response(201, json=pod)
        if request.method == "GET" and path.startswith(f"/api/v1/namespaces/{NAMESPACE}/pods/"):
            return self._get_pod(path.rsplit("/", 1)[-1])
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def _get_pod(self, name: str) -> httpx.Response:
        self.gets += 1
        pod = self.pods.get(name)
        if pod is None:
            return httpx.Response(404, json={"message": "not found"})
        self._advance(pod)
        return httpx.Response(200, json=pod)

    def _advance(self, pod: dict[str, Any]) -> None:
        status = pod["status"]
        if self.replacement_uid is not None:
            pod["metadata"]["uid"] = self.replacement_uid
        if self.terminal_phase is not None:
            status["phase"] = self.terminal_phase
            status["conditions"] = []
        if self.terminal_exit_code is not None:
            status["containerStatuses"] = [
                {"name": "engine", "state": {"terminated": {"exitCode": self.terminal_exit_code}}}
            ]
        if (
            self.terminal_phase is None
            and self.ready_after_gets is not None
            and self.gets >= self.ready_after_gets
        ):
            status["phase"] = "Running"
            status["conditions"] = [{"type": "Ready", "status": "True"}]
            status["podIP"] = "10.0.0.7"


def _pod_from(body: Mapping[str, Any]) -> dict[str, Any]:
    metadata = body["metadata"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata["namespace"],
            "uid": POD_UID,
            "labels": dict(metadata.get("labels", {})),
            "annotations": dict(metadata.get("annotations", {})),
        },
        "spec": body["spec"],
        "status": {"phase": "Pending", "conditions": []},
    }


def _token() -> str:
    payload = json.dumps(
        {"sub": f"system:serviceaccount:{NAMESPACE}:{SERVICE_ACCOUNT}"}, separators=(",", ":")
    ).encode()
    segment = __import__("base64").urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{segment}.signature"


def _api(tmp_path: Path, cluster: _FakeCluster) -> KubernetesApi:
    token_path = tmp_path / "token"
    token_path.write_text(_token(), encoding="ascii")
    return KubernetesApi(
        settings=KubernetesApiSettings(
            namespace=NAMESPACE,
            service_account_name=SERVICE_ACCOUNT,
            api_server_url=API_URL,
            token_path=token_path,
        ),
        transport=httpx.MockTransport(cluster.handler),
    )


def _runtime(
    tmp_path: Path,
    cluster: _FakeCluster,
    *,
    credentials: EngineCredentials | None = None,
    clock: _Clock | None = None,
    **settings: Any,
) -> tuple[KubernetesNodeRuntime, _Clock]:
    resolved = _Clock() if clock is None else clock
    api = _api(tmp_path, cluster)
    runtime = KubernetesNodeRuntime(
        api=api,
        node_id=NODE_ID,
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


def test_prepare_pins_the_image_by_digest_instead_of_its_tag(tmp_path: Path) -> None:
    cluster = _FakeCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    runtime.prepare(build_request(), build_template())

    container = cluster.created[0]["spec"]["containers"][0]
    assert container["image"] == f"local/synthetic-engine@{IMAGE_DIGEST}"
    assert SAMPLE_IMAGE_REFERENCE not in container["image"]


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
    assert container["command"] == ["/usr/bin/engine"]
    assert container["args"] == ["--model", "/models/asr", "--port", "9000"]
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


def test_stop_and_inspection_refuse_until_their_slices_land(tmp_path: Path) -> None:
    cluster = _FakeCluster()
    runtime, _clock = _runtime(tmp_path, cluster)
    request = build_request()
    template = build_template()

    with pytest.raises(NotImplementedError, match="S3"):
        runtime.stop(request, template)
    with pytest.raises(NotImplementedError, match="S4"):
        runtime.inspect(request, template)
    with pytest.raises(NotImplementedError, match="S3"):
        runtime.recover_stop(request, template)


_WARMUP_BODY = b"synthetic-warmup-body"


def _warmup_body(tmp_path: Path) -> Path:
    path = tmp_path / "warmup.wav"
    path.write_bytes(_WARMUP_BODY)
    return path


def _warmup_digest() -> str:
    return "sha256:" + hashlib.sha256(_WARMUP_BODY).hexdigest()
