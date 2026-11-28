"""#1282: a receipt is only signed for the generation the node actually prepared."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from speechnexus_backend.controller_head import (
    CONTROLLER_TOKEN_VARIABLE,
    CONTROLLER_URL_VARIABLE,
    ControllerConsultationError,
    ControllerEndpoint,
    NodeInstanceHead,
    configured_endpoint,
    generation_disagreement,
    read_head,
)


class _HeadHandler(BaseHTTPRequestHandler):
    document: ClassVar[dict[str, object]] = {}
    status = 200

    def do_GET(self) -> None:
        body = json.dumps(self.document).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def controller() -> Iterator[tuple[str, type[_HeadHandler]]]:
    handler = type("Handler", (_HeadHandler,), {"document": {}, "status": 200})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()


def test_a_half_configured_controller_refuses_instead_of_skipping_the_check() -> None:
    # A URL without the component credential would fail every call, and skipping it
    # quietly is how a deployment believes it cross-checks when it never does.
    with pytest.raises(ControllerConsultationError) as raised:
        configured_endpoint({CONTROLLER_URL_VARIABLE: "http://127.0.0.1:8080"})
    assert raised.value.code == "controller_not_configured"

    with pytest.raises(ControllerConsultationError) as credentials:
        configured_endpoint(
            {
                CONTROLLER_URL_VARIABLE: "http://user:pass@127.0.0.1:8080",
                CONTROLLER_TOKEN_VARIABLE: "token",
            }
        )
    assert credentials.value.code == "invalid_controller_url"


def test_an_unconfigured_deployment_is_the_only_silent_case() -> None:
    assert configured_endpoint({}) is None


def test_the_head_is_read_from_the_node_that_prepared_the_instance(
    controller: tuple[str, type[_HeadHandler]],
) -> None:
    url, handler = controller
    handler.document = {
        "instance_id": "instance-asr-1",
        "current_generation": 11,
        "generation_high_water": 11,
        "last_fencing_token": 20,
        "unresolved_prepare_intent_id": None,
        "unresolved_stop_intent_id": None,
    }
    head = read_head(
        ControllerEndpoint(url, "token"), node_id="node-megacube-1", instance_id="instance-asr-1"
    )

    assert head == NodeInstanceHead("instance-asr-1", 11, 11, last_fencing_token=20)
    # The disagreement names both numbers and both ways out, so the operator does not
    # have to guess which plane is right.
    message = generation_disagreement(1, head, node_id="node-megacube-1")
    assert message is not None
    for fact in (
        "generation 1",
        "generation 11",
        "previous_generation_active",
        "node-megacube-1",
        "restarts the engine",
    ):
        assert fact in message


def test_an_agreeing_node_produces_no_disagreement(
    controller: tuple[str, type[_HeadHandler]],
) -> None:
    url, handler = controller
    handler.document = {"instance_id": "instance-asr-1", "current_generation": 11}

    head = read_head(
        ControllerEndpoint(url, "token"), node_id="node-megacube-1", instance_id="instance-asr-1"
    )

    assert generation_disagreement(11, head, node_id="node-megacube-1") is None


def test_an_instance_the_node_never_prepared_is_not_a_generation_disagreement() -> None:
    # "nothing was prepared here" is a different problem with a different answer.
    head = NodeInstanceHead("instance-asr-1", None, None)
    assert generation_disagreement(7, head, node_id="node-megacube-1") is None


def test_an_unreachable_controller_is_an_error_not_an_agreement() -> None:
    endpoint = ControllerEndpoint("http://127.0.0.1:9", "token")
    with pytest.raises(ControllerConsultationError) as raised:
        read_head(endpoint, node_id="node-megacube-1", instance_id="instance-asr-1")
    assert raised.value.code == "controller_unreachable"


def test_a_refused_head_reports_the_status_rather_than_an_empty_head(
    controller: tuple[str, type[_HeadHandler]],
) -> None:
    url, handler = controller
    handler.status = 409
    with pytest.raises(ControllerConsultationError) as raised:
        read_head(
            ControllerEndpoint(url, "token"),
            node_id="node-other-1",
            instance_id="instance-asr-1",
        )
    assert raised.value.code == "controller_refused"


def test_a_head_with_a_bad_generation_is_rejected(
    controller: tuple[str, type[_HeadHandler]],
) -> None:
    url, handler = controller
    handler.document = {"instance_id": "instance-asr-1", "current_generation": "eleven"}
    with pytest.raises(ControllerConsultationError) as raised:
        read_head(
            ControllerEndpoint(url, "token"),
            node_id="node-megacube-1",
            instance_id="instance-asr-1",
        )
    assert raised.value.code == "controller_head_invalid"
