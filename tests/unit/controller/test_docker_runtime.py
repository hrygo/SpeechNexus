"""Restricted container adapter: approved image, one container per generation, verified stop."""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from tests.fake.controller import (
    IMAGE_DIGEST,
    SAMPLE_IMAGE_REFERENCE,
    build_request,
    build_template,
)
from tests.fake.docker_api import FakeDockerEngine

from speechnexus_node_controller.domain.intents import (
    IntentConflictError,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    UnapprovedRuntimeTemplateError,
)
from speechnexus_node_controller.domain.templates import (
    ReadinessKind,
    ReadinessRule,
    ReadOnlyBind,
    WarmupRequest,
)
from speechnexus_node_controller.infrastructure.docker_runtime import (
    CONFIG_DIGEST_LABEL,
    GENERATION_LABEL,
    INSTANCE_ID_LABEL,
    MANAGED_BY_LABEL,
    NODE_ID_LABEL,
    TEMPLATE_ID_LABEL,
    DockerNodeRuntime,
    DockerRuntimeSettings,
)

_NODE = "node-local-1"


def _engine(**overrides: Any) -> FakeDockerEngine:
    images = {"local/other-engine:1": "sha256:" + "d" * 64}
    images.update(overrides.pop("images", {SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST}))
    return FakeDockerEngine(images=images, **overrides)


def _runtime(engine: FakeDockerEngine, **settings: Any) -> DockerNodeRuntime:
    return DockerNodeRuntime(
        api=engine,
        node_id=_NODE,
        settings=DockerRuntimeSettings(**settings),
    )


def test_prepare_verifies_the_approved_image_before_creating_anything() -> None:
    engine = _engine(images={"local/other-engine:1": "sha256:" + "d" * 64})
    runtime = _runtime(engine)
    with pytest.raises(UnapprovedRuntimeTemplateError):
        runtime.prepare(build_request(), build_template())
    assert engine.created == []


def test_prepare_creates_one_container_for_the_generation() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    observation = runtime.prepare(build_request(), build_template())
    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert len(engine.created) == 1


def test_prepare_reuses_the_container_instead_of_creating_a_second_one() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    runtime.prepare(build_request(), build_template())
    runtime.prepare(build_request(), build_template())
    assert len(engine.created) == 1


def test_prepare_refuses_a_generation_that_already_carries_another_digest() -> None:
    engine = _engine()
    request = build_request()
    engine.create_container(
        name="speechnexus-instance-instance-asr-1-g1",
        image_reference=SAMPLE_IMAGE_REFERENCE,
        command=None,
        labels={
            MANAGED_BY_LABEL: "speechnexus-node-controller",
            NODE_ID_LABEL: _NODE,
            INSTANCE_ID_LABEL: request.instance_id,
            GENERATION_LABEL: str(request.instance_generation),
            TEMPLATE_ID_LABEL: request.approved_template_id,
            CONFIG_DIGEST_LABEL: "sha256:" + "e" * 64,
        },
        host_config={},
    )
    runtime = _runtime(engine)
    with pytest.raises(IntentConflictError):
        runtime.prepare(request, build_template())
    assert len(engine.created) == 1


def test_prepare_carries_identity_labels_and_hardening() -> None:
    engine = _engine()
    request = build_request()
    runtime = _runtime(engine)
    runtime.prepare(
        request,
        build_template(
            command=("serve", "--port", "9000"),
            gpu_device_count=1,
            read_only_binds=(ReadOnlyBind(source="/models/asr", target="/models/asr"),),
            network_name="synthetic-internal-network",
        ),
    )
    container = engine.created[0]
    assert container.labels[MANAGED_BY_LABEL] == "speechnexus-node-controller"
    assert container.labels[NODE_ID_LABEL] == _NODE
    assert container.labels[INSTANCE_ID_LABEL] == request.instance_id
    assert container.labels[GENERATION_LABEL] == str(request.instance_generation)
    assert container.labels[TEMPLATE_ID_LABEL] == request.approved_template_id
    assert container.labels[CONFIG_DIGEST_LABEL] == request.config_digest
    assert container.command == ("serve", "--port", "9000")
    assert container.host_config["Privileged"] is False
    assert container.host_config["Init"] is True
    assert container.host_config["CapDrop"] == ["ALL"]
    assert container.host_config["SecurityOpt"] == ["no-new-privileges"]
    assert container.host_config["RestartPolicy"] == {"Name": "no"}
    assert container.host_config["Memory"] == 1024**3
    assert container.host_config["MemorySwap"] == 1024**3
    assert "PortBindings" not in container.host_config
    assert container.host_config["NetworkMode"] == "synthetic-internal-network"
    assert container.host_config["Binds"] == ["/models/asr:/models/asr:ro"]
    assert container.host_config["DeviceRequests"] == [
        {"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]]}
    ]


def test_container_name_is_derived_from_the_instance_identity() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    runtime.prepare(build_request(instance_id="instance:asr-1"), build_template())
    assert engine.created[0].name == "speechnexus-instance-instance-asr-1-g1"


@pytest.mark.parametrize("field", ["Memory", "MemorySwap"])
def test_legacy_or_drifted_unbounded_container_is_quarantined_but_can_be_stopped(
    field: str,
) -> None:
    engine = _engine()
    template = build_template()
    runtime = _runtime(engine)
    runtime.prepare(build_request(), template)
    engine.created[0].host_config[field] = 0

    observation = runtime.inspect(build_request(), template)
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observation.stop_evidence is None
    replay = runtime.prepare(build_request(), template)
    assert replay.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert len(engine.created) == 1

    stopped = runtime.stop(build_request(action=RuntimeActionKind.STOP), template)
    assert stopped.stop_evidence is not None
    assert engine.containers == {}


def test_restarted_controller_cannot_claim_an_unapproved_container_network_is_ready() -> None:
    engine = _engine()
    template = build_template(network_name="approved-network")
    runtime = _runtime(engine)
    runtime.prepare(build_request(), template)
    engine.created[0].host_config["NetworkMode"] = "bridge"
    observed = _runtime(engine).inspect(build_request(), template)
    assert observed is not None
    assert observed.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observed.stop_evidence is None


def test_tcp_readiness_reports_loading_until_the_port_accepts() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        template = build_template(
            readiness=ReadinessRule(ReadinessKind.TCP_PORT_ACCEPTING, port=port)
        )
        engine = _engine(addresses=("127.0.0.1",))
        runtime = _runtime(engine)
        observation = runtime.prepare(build_request(), template)
        assert observation.observed_instance_state is ObservedInstanceState.READY
    finally:
        listener.close()

    engine = _engine(addresses=("127.0.0.1",))
    runtime = _runtime(engine)
    closed_port_template = build_template(
        readiness=ReadinessRule(ReadinessKind.TCP_PORT_ACCEPTING, port=port)
    )
    observation = runtime.prepare(build_request(), closed_port_template)
    assert observation.observed_instance_state is ObservedInstanceState.LOADING


@pytest.fixture
def health_server() -> Iterator[tuple[ThreadingHTTPServer, list[str], list[int]]]:
    """The TCP listener remains up while the engine changes health status."""
    paths: list[str] = []
    statuses = [503]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            self.send_response(statuses[0])
            self.send_header("Content-Length", "0")
            self.send_header("Location", "/must-not-follow")
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, paths, statuses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("initial_status", [204, 301, 401, 404, 503])
def test_http_readiness_keeps_loading_until_exactly_200(
    health_server: tuple[ThreadingHTTPServer, list[str], list[int]],
    initial_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, paths, statuses = health_server
    statuses[0] = initial_status
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    template = build_template(
        readiness=ReadinessRule(
            ReadinessKind.HTTP_STATUS_OK, port=server.server_port, path="/health"
        )
    )
    engine = _engine(addresses=("127.0.0.1",))
    runtime = _runtime(engine)
    observation = runtime.prepare(build_request(), template)
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    assert paths == ["/health"]

    statuses[0] = 200
    ready_observation = runtime.inspect(build_request(), template)
    assert ready_observation is not None
    assert ready_observation.observed_instance_state is ObservedInstanceState.READY
    assert paths == ["/health", "/health"]


def test_http_readiness_with_no_answer_retains_loading() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        template = build_template(
            readiness=ReadinessRule(
                ReadinessKind.HTTP_STATUS_OK,
                port=listener.getsockname()[1],
                path="/health",
            )
        )
        observation = _runtime(
            _engine(addresses=("127.0.0.1",)), readiness_timeout_seconds=0.01
        ).prepare(build_request(), template)
    assert observation.observed_instance_state is ObservedInstanceState.LOADING
    assert observation.stop_evidence is None


def test_http_readiness_refuses_hostnames_from_container_inspection() -> None:
    template = build_template(
        readiness=ReadinessRule(ReadinessKind.HTTP_STATUS_OK, port=8000, path="/health")
    )
    observation = _runtime(_engine(addresses=("untrusted.invalid",))).prepare(
        build_request(), template
    )
    assert observation.observed_instance_state is ObservedInstanceState.LOADING


def test_stop_cannot_target_another_nodes_container() -> None:
    engine = _engine()
    foreign_runtime = DockerNodeRuntime(api=engine, node_id="node-other")
    foreign_runtime.prepare(build_request(), build_template())
    foreign_id = engine.created[0].container_id

    observation = _runtime(engine).stop(
        build_request(action=RuntimeActionKind.STOP), build_template()
    )
    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    assert observation.stop_evidence is None
    assert engine.containers[foreign_id].running
    assert engine.removed == []


def test_prepare_reports_the_observed_peak_memory() -> None:
    engine = _engine(memory_peak_bytes=3 * 1024**3)
    runtime = _runtime(engine)
    observation = runtime.prepare(build_request(), build_template())
    assert observation.observed_peak_memory is not None
    assert observation.observed_peak_memory.unified_memory_bytes == 3 * 1024**3


def test_stop_without_a_container_reports_absent_and_no_evidence() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    assert observation.stop_evidence is None


def test_stop_verifies_exit_and_removal_before_reporting_evidence() -> None:
    engine = _engine(exit_code=0)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.observed_instance_state is ObservedInstanceState.STOPPED
    assert observation.stop_evidence is not None
    assert observation.stop_evidence.exit_code == 0
    assert observation.stop_evidence.runtime_container_id in engine.removed
    assert engine.containers == {}


def test_stop_without_confirmed_removal_reports_no_evidence() -> None:
    engine = _engine(fail_remove_and_keep=True)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.stop_evidence is None
    assert observation.observed_instance_state is ObservedInstanceState.STOPPED


def test_stop_that_cannot_wait_for_exit_keeps_reporting_the_live_instance() -> None:
    engine = _engine(fail_wait=True)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.stop_evidence is None
    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert engine.containers != {}


def test_stop_that_times_out_keeps_reporting_the_live_instance() -> None:
    engine = _engine(fail_stop=True)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.stop_evidence is None
    assert observation.observed_instance_state is ObservedInstanceState.READY


def test_inspect_reports_absent_without_fabricating_evidence() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    observation = runtime.inspect(build_request(), build_template())
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    assert observation.stop_evidence is None


def test_inspect_quarantines_a_container_that_carries_another_digest() -> None:
    engine = _engine()
    request = build_request()
    engine.create_container(
        name="speechnexus-instance-instance-asr-1-g1",
        image_reference=SAMPLE_IMAGE_REFERENCE,
        command=None,
        labels={
            MANAGED_BY_LABEL: "speechnexus-node-controller",
            NODE_ID_LABEL: _NODE,
            INSTANCE_ID_LABEL: request.instance_id,
            GENERATION_LABEL: str(request.instance_generation),
            TEMPLATE_ID_LABEL: request.approved_template_id,
            CONFIG_DIGEST_LABEL: "sha256:" + "e" * 64,
        },
        host_config={},
    )
    runtime = _runtime(engine)
    observation = runtime.inspect(request, build_template())
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observation.quarantine_reason is not None


def test_inspect_of_a_running_container_reports_the_ready_state() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.inspect(request, build_template())
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert observation.observed_peak_memory is not None


def test_a_silent_daemon_never_becomes_a_prepared_instance() -> None:
    engine = _engine(daemon_unavailable=True)

    with pytest.raises(RuntimeControllerUnavailableError):
        _runtime(engine).prepare(build_request(), build_template())

    assert engine.created == []


def test_a_silent_daemon_never_becomes_stop_evidence() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    runtime.prepare(build_request(), build_template())
    prepared = engine.created[0]
    assert prepared.running is True

    engine.daemon_unavailable = True
    with pytest.raises(RuntimeControllerUnavailableError):
        runtime.stop(
            build_request(
                intent_id="intent-stop-1",
                action=RuntimeActionKind.STOP,
                fencing_token=2,
            ),
            build_template(),
        )

    assert prepared.running is True
    assert engine.removed == []


@pytest.fixture()
def warmup_server() -> Iterator[
    tuple[ThreadingHTTPServer, list[tuple[str, str, bytes, str]], list[int]]
]:
    """Record every warmup POST; the engine answers with the queued status."""
    requests: list[tuple[str, str, bytes, str]] = []
    statuses = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append((self.command, self.path, body, self.headers.get("Content-Type", "")))
            payload = b"data: [DONE]\n\n" if statuses[0] == 200 else b""
            self.send_response(statuses[0])
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, requests, statuses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _warmup_body(tmp_path: Path, content: bytes) -> tuple[Path, str]:
    body = tmp_path / "warmup-body.bin"
    body.write_bytes(content)
    return body, "sha256:" + hashlib.sha256(content).hexdigest()


def _warmup_rule(
    server: ThreadingHTTPServer, body: Path, digest: str, *, attempts: int = 2
) -> ReadinessRule:
    return ReadinessRule(
        ReadinessKind.HTTP_WARMUP_OK,
        port=server.server_port,
        warmup=WarmupRequest(
            path="/v1/audio/transcriptions",
            content_type="application/octet-stream",
            body_path=str(body),
            body_sha256=digest,
            attempts=attempts,
            timeout_seconds=5,
            max_response_bytes=4096,
        ),
    )


def test_warmup_readiness_stays_warming_up_until_every_attempt_completes(
    warmup_server: tuple[ThreadingHTTPServer, list[tuple[str, str, bytes, str]], list[int]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, requests, _ = warmup_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    content = b"\x00\x01warm-body"
    body, digest = _warmup_body(tmp_path, content)
    template = build_template(readiness=_warmup_rule(server, body, digest, attempts=2))
    runtime = _runtime(_engine(addresses=("127.0.0.1",)))
    request = build_request()

    assert runtime.prepare(request, template).observed_instance_state is (
        ObservedInstanceState.WARMING_UP
    )
    second = runtime.inspect(request, template)
    assert second is not None
    assert second.observed_instance_state is ObservedInstanceState.READY
    third = runtime.inspect(request, template)
    assert third is not None
    assert third.observed_instance_state is ObservedInstanceState.READY

    assert requests == [
        ("POST", "/v1/audio/transcriptions", content, "application/octet-stream"),
        ("POST", "/v1/audio/transcriptions", content, "application/octet-stream"),
    ]


@pytest.mark.parametrize("rejected_status", [301, 401, 503])
def test_warmup_readiness_never_reports_ready_for_a_rejected_attempt(
    warmup_server: tuple[ThreadingHTTPServer, list[tuple[str, str, bytes, str]], list[int]],
    tmp_path: Path,
    rejected_status: int,
) -> None:
    server, requests, statuses = warmup_server
    statuses[0] = rejected_status
    body, digest = _warmup_body(tmp_path, b"\x00warm")
    template = build_template(readiness=_warmup_rule(server, body, digest, attempts=1))
    runtime = _runtime(_engine(addresses=("127.0.0.1",)))
    request = build_request()

    first = runtime.prepare(request, template)
    second = runtime.inspect(request, template)
    assert first.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert second is not None
    assert second.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert len(requests) == 2

    statuses[0] = 200
    ready = runtime.inspect(request, template)
    assert ready is not None
    assert ready.observed_instance_state is ObservedInstanceState.READY
    assert len(requests) == 3


def test_warmup_readiness_sends_nothing_until_the_body_matches_its_digest(
    warmup_server: tuple[ThreadingHTTPServer, list[tuple[str, str, bytes, str]], list[int]],
    tmp_path: Path,
) -> None:
    server, requests, _ = warmup_server
    body, _ = _warmup_body(tmp_path, b"\x00warm")
    template = build_template(
        readiness=_warmup_rule(server, body, "sha256:" + "0" * 64, attempts=1)
    )
    runtime = _runtime(_engine(addresses=("127.0.0.1",)))

    observation = runtime.prepare(build_request(), template)

    assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert requests == []


def test_warmup_readiness_stays_loading_while_the_port_is_closed(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        body, digest = _warmup_body(tmp_path, b"\x00warm")
        template = build_template(
            readiness=ReadinessRule(
                ReadinessKind.HTTP_WARMUP_OK,
                port=port,
                warmup=WarmupRequest(
                    path="/v1/audio/transcriptions",
                    content_type="application/octet-stream",
                    body_path=str(body),
                    body_sha256=digest,
                    attempts=1,
                    timeout_seconds=1,
                    max_response_bytes=4096,
                ),
            )
        )
        observation = _runtime(
            _engine(addresses=("127.0.0.1",)), readiness_timeout_seconds=0.01
        ).prepare(build_request(), template)
    assert observation.observed_instance_state is ObservedInstanceState.LOADING


def test_a_stop_observation_does_not_spend_warmup_time(
    warmup_server: tuple[ThreadingHTTPServer, list[tuple[str, str, bytes, str]], list[int]],
    tmp_path: Path,
) -> None:
    server, requests, _ = warmup_server
    body, digest = _warmup_body(tmp_path, b"\x00warm")
    template = build_template(readiness=_warmup_rule(server, body, digest, attempts=2))
    runtime = _runtime(_engine(addresses=("127.0.0.1",)))
    prepared = runtime.prepare(build_request(), template)
    assert prepared.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert len(requests) == 1

    observation = runtime.inspect(
        build_request(
            intent_id="intent-stop-warmup",
            action=RuntimeActionKind.STOP,
            fencing_token=2,
        ),
        template,
    )

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert len(requests) == 1
