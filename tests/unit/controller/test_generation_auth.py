"""Native auth must be effective before warmup/ready, including reused containers."""

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from tests.fake.controller import InMemoryStopCheckpoints, build_request, build_template
from tests.fake.docker_api import FakeDockerEngine

from speechnexus_node_controller.domain.intents import (
    ObservedInstanceState,
    RuntimeActionKind,
    UnapprovedRuntimeTemplateError,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    EngineAuthKind,
    ReadinessKind,
    ReadinessRule,
    WarmupRequest,
)
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.infrastructure.engine_credentials import EngineCredentials

CREDENTIALS = EngineCredentials("00" * 32)


@pytest.fixture
def native_server() -> Iterator[tuple[ThreadingHTTPServer, list[str], list[str]]]:
    # This HTTP double checks the same bearer value, not the actual vLLM middleware.
    expected = [CREDENTIALS.for_instance(build_request())]
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            header = self.headers.get("Authorization", "")
            seen.append(header)
            self.send_response(200 if header == "Bearer " + expected[0] else 401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, seen, expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def template(
    tmp_path: Path, port: int, path: str = "/v1/audio/transcriptions"
) -> ApprovedRuntimeTemplate:
    body = tmp_path / "synthetic-warmup"
    body.write_bytes(b"synthetic bounded warmup")
    return replace(
        build_template(
            command=("--model", "/artifacts/synthetic"),
            readiness=ReadinessRule(
                ReadinessKind.HTTP_WARMUP_OK,
                port=port,
                warmup=WarmupRequest(
                    path=path,
                    content_type="application/octet-stream",
                    body_path=str(body),
                    body_sha256="sha256:" + hashlib.sha256(body.read_bytes()).hexdigest(),
                    attempts=1,
                ),
            ),
        ),
        engine_auth=EngineAuthKind.VLLM_API_KEY_V1,
    )


def runtime(
    engine: FakeDockerEngine, credentials: EngineCredentials | None = CREDENTIALS
) -> DockerNodeRuntime:
    return DockerNodeRuntime(
        api=engine,
        node_id="node-local-1",
        stop_checkpoints=InMemoryStopCheckpoints(),
        engine_credentials=credentials,
    )


@pytest.mark.parametrize("path", ["/v1/audio/transcriptions", "/v1/audio/speech"])
def test_prepare_installs_native_key_and_authenticates_actual_warmup(
    tmp_path: Path,
    native_server: Any,
    path: str,
) -> None:
    server, seen, _ = native_server
    approved = template(tmp_path, server.server_port, path)
    engine = FakeDockerEngine(
        images={approved.image_reference: approved.image_digest}, addresses=("127.0.0.1",)
    )
    adapter = runtime(engine)
    observed = adapter.prepare(build_request(), approved)
    key = CREDENTIALS.for_instance(build_request())
    assert observed.observed_instance_state is ObservedInstanceState.READY
    assert engine.created[0].environment == ("VLLM_API_KEY=" + key,)
    assert seen == ["Bearer " + key]
    assert key not in repr(engine.inspect_container(engine.created[0].container_id))
    # A restarted controller rechecks and rewarms the same container, not a new one.
    assert (
        runtime(engine).prepare(build_request(), approved).observed_instance_state
        is ObservedInstanceState.READY
    )
    assert len(engine.created) == 1
    assert seen == ["Bearer " + key] * 2


@pytest.mark.parametrize(
    "drift", ["missing", "other_key", "duplicate", "argv", "image", "gateway_label"]
)
@pytest.mark.parametrize("path", ["/v1/audio/transcriptions", "/v1/audio/speech"])
def test_drift_or_unprotected_container_cannot_be_reused_as_ready_and_still_can_stop(
    tmp_path: Path,
    native_server: Any,
    drift: str,
    path: str,
) -> None:
    server, seen, _ = native_server
    approved = template(tmp_path, server.server_port, path)
    engine = FakeDockerEngine(
        images={approved.image_reference: approved.image_digest}, addresses=("127.0.0.1",)
    )
    adapter = runtime(engine)
    adapter.prepare(build_request(), approved)
    container = engine.created[0]
    if drift == "missing":
        container.environment = ()
    elif drift == "other_key":
        container.environment = ("VLLM_API_KEY=" + "11" * 32,)
    elif drift == "duplicate":
        container.environment *= 2
    elif drift == "argv":
        container.command = ("--api-key", "synthetic-override")
    elif drift == "image":
        container.image_id = "sha256:" + "f" * 64
    else:
        container.labels["nexus.gateway-id"] = "gateway-other"
    observed = runtime(engine).prepare(build_request(), approved)
    assert observed.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observed.stop_evidence is None
    assert len(seen) == len(engine.created) == 1
    if drift != "gateway_label":
        stopped = adapter.stop(build_request(action=RuntimeActionKind.STOP), approved)
        assert stopped.stop_evidence is not None


def test_no_root_key_rejects_creation_before_any_daemon_mutation(tmp_path: Path) -> None:
    approved = template(tmp_path, 8000)
    engine = FakeDockerEngine(images={approved.image_reference: approved.image_digest})
    with pytest.raises(UnapprovedRuntimeTemplateError):
        runtime(engine, None).prepare(build_request(), approved)
    assert not engine.created


@pytest.mark.parametrize("path", ["/v1/audio/transcriptions", "/v1/audio/speech"])
def test_reused_ip_with_next_generation_native_key_cannot_complete_old_warmup(
    tmp_path: Path,
    native_server: Any,
    path: str,
) -> None:
    server, seen, expected = native_server
    approved = template(tmp_path, server.server_port, path)
    expected[0] = CREDENTIALS.for_instance(build_request(instance_generation=2))
    engine = FakeDockerEngine(
        images={approved.image_reference: approved.image_digest}, addresses=("127.0.0.1",)
    )
    observed = runtime(engine).prepare(build_request(), approved)
    assert observed.observed_instance_state is ObservedInstanceState.WARMING_UP
    assert observed.stop_evidence is None
    assert seen == ["Bearer " + CREDENTIALS.for_instance(build_request())]


@pytest.mark.parametrize(
    "argument",
    [
        "--api-key=override",
        "--api_key",
        "--api",
        "--api-key+",
        "--conf=x",
        "--config=x",
        "--root-path",
        "--middleware",
    ],
)
def test_template_cannot_override_or_bypass_native_authentication(
    tmp_path: Path,
    argument: str,
) -> None:
    with pytest.raises(ValueError, match="override native"):
        replace(template(tmp_path, 8000), command=(argument,))


@pytest.mark.parametrize("path", ["/v1/models", "/health", "/v1/audio/speech/"])
def test_native_auth_rejects_a_warmup_outside_the_supported_speech_paths(
    tmp_path: Path, path: str
) -> None:
    with pytest.raises(ValueError, match="warmup"):
        template(tmp_path, 8000, path)


def test_native_auth_still_requires_model_warmup_instead_of_health_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="warmup"):
        replace(
            template(tmp_path, 8000),
            readiness=ReadinessRule(ReadinessKind.HTTP_STATUS_OK, port=8000, path="/health"),
        )


def test_native_auth_keeps_approved_api_server_concurrency_configurable(tmp_path: Path) -> None:
    approved = replace(template(tmp_path, 8000), command=("--api-server-count", "2"))
    assert approved.engine_auth is EngineAuthKind.VLLM_API_KEY_V1
