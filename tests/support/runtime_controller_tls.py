"""Real loopback TLS and the native controller/SQLite; only its daemon is synthetic."""

from __future__ import annotations

import socket
import ssl
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import uvicorn
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from speechnexus_backend.speech.infrastructure.runtime_controller_http import (
    RuntimeControllerEndpoint,
)
from speechnexus_node_controller.app import create_app
from speechnexus_node_controller.application.readiness import ReadinessService
from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.ports import NodeRuntimePort
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore
from speechnexus_node_controller.settings import Settings
from tests.fake.controller import ScriptedNodeRuntime, build_template
from tests.fake.readiness import StaticProbe
from tests.fake.runtime_controller import MIB
from tests.fake.runtime_controller import TLS_REQUEST as REQUEST

TOKEN = "synthetic-tls-controller-component-token"


@dataclass(frozen=True)
class TLSFiles:
    certificate: Path = field(repr=False)
    key: Path = field(repr=False)

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.certificate))


def tls_files(directory: Path, *, address: str = "127.0.0.1") -> TLSFiles:
    directory.mkdir()
    certificate, key = directory / "certificate.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-noenc",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=synthetic-controller",
            "-addext",
            "subjectAltName=IP:" + address,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    key.chmod(0o600)
    return TLSFiles(certificate, key)


@pytest.fixture(scope="module")
def controller_tls(tmp_path_factory: pytest.TempPathFactory) -> TLSFiles:
    return tls_files(tmp_path_factory.mktemp("controller-pki") / "ephemeral")


@dataclass
class LiveController[Runtime: NodeRuntimePort]:
    endpoint: RuntimeControllerEndpoint
    tls: TLSFiles = field(repr=False)
    store: SqliteIntentStore = field(repr=False)
    runtime: Runtime = field(repr=False)
    service: NodeRuntimeControlService = field(repr=False)
    methods: list[str] = field(repr=False)
    request_bodies: list[bytes] = field(repr=False)


class _InterruptedJsonResponse(Response):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", b"100")],
            }
        )
        await send({"type": "http.response.body", "body": b"{", "more_body": True})
        raise ConnectionAbortedError("Synthetic reply loss after the native controller accepted")


@contextmanager
def controller_server[Runtime: NodeRuntimePort](
    directory: Path,
    tls: TLSFiles,
    *,
    runtime: Runtime | None = None,
    runtime_factory: Callable[[SqliteIntentStore], Runtime] | None = None,
    lose_first_post_reply: bool = False,
    local_observer_enabled: bool = False,
) -> Iterator[LiveController[Runtime | ScriptedNodeRuntime]]:
    directory.mkdir(exist_ok=True)
    store = SqliteIntentStore(directory / "intents.sqlite3")
    daemon = (
        runtime_factory(store)
        if runtime_factory is not None
        else runtime
        if runtime is not None
        else ScriptedNodeRuntime()
    )
    binding = REQUEST.binding
    service = NodeRuntimeControlService(
        node_id=binding.target_node_id,
        templates=ApprovedTemplateRegistry(
            (
                build_template(
                    template_id=binding.approved_template_id,
                    pools=frozenset({binding.pool_id}),
                    max_peak_memory_bytes=100 * MIB,
                    container_memory_limit_bytes=6 * MIB,
                ),
            )
        ),
        store=store,
        runtime=daemon,
    )
    settings = Settings(
        node_id=binding.target_node_id,
        component_token=SecretStr(TOKEN),
        approved_templates_file=directory / "unused-templates.json",
        intent_store_path=directory / "intents.sqlite3",
        docker_socket_path=directory / "unused-socket",
        # These scenarios explicitly script/fold each observation. Loop coverage
        # has its own lifespan and real-process tests.
        local_observer_enabled=local_observer_enabled,
        observation_interval_seconds=0.02,
    )
    app = create_app(
        settings=settings,
        service=service,
        readiness=ReadinessService([StaticProbe("synthetic_daemon")], timeout_seconds=1),
    )
    methods: list[str] = []
    request_bodies: list[bytes] = []

    @app.middleware("http")
    async def record_call(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonlocal lose_first_post_reply
        methods.append(request.method)
        if request.method == "POST":
            request_bodies.append(await request.body())
        response = await call_next(request)
        if request.method == "POST" and lose_first_post_reply:
            lose_first_post_reply = False
            return _InterruptedJsonResponse()
        return response

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            access_log=False,
            ssl_certfile=str(tls.certificate),
            ssl_keyfile=str(tls.key),
            timeout_graceful_shutdown=2,
        )
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            serving = workers.submit(server.run, sockets=[sock])
            try:
                deadline = time.monotonic() + 5
                while not server.started and not serving.done() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert server.started, "The isolated controller did not start TLS"
                yield LiveController(
                    RuntimeControllerEndpoint(binding.target_node_id, "127.0.0.1", port, TOKEN),
                    tls,
                    store,
                    daemon,
                    service,
                    methods,
                    request_bodies,
                )
            finally:
                server.should_exit = True
                serving.result(timeout=5)
    finally:
        sock.close()
        store.close()
