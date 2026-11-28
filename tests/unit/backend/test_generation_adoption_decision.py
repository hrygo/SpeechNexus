"""#1282: the audited adoption decides once, for both the writer and the report.

The decision that lets the ledger follow the node's generation is the one place where
"is this repair allowed" is answered. ``speechnexus-adopt-generation`` executes it and the
readiness report calls it to tell an operator whether the repair is available, so a
second copy of these conditions would drift exactly like the two generation counters did.
These cases pin the branches and the testimony each branch demands.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from speechnexus_backend.controller_head import (
    ADOPTION_ADOPTABLE,
    ADOPTION_ALIGNED,
    ADOPTION_REFUSED,
    PREVIOUS_STOP_MISSING,
    PREVIOUS_STOPPED,
    PREVIOUS_VOIDED,
    ControllerConsultationError,
    ControllerEndpoint,
    NodeInstanceHead,
    decide_generation_adoption,
    read_head,
)

NODE = "node-megacube-1"
INSTANCE = "instance-asr-1"


def _head(
    *,
    current: int | None = 11,
    high_water: int | None = 11,
    fencing: int | None = 20,
    stopped: tuple[int, ...] = (1,),
    voided: tuple[int, ...] = (),
) -> NodeInstanceHead:
    return NodeInstanceHead(
        INSTANCE,
        current,
        high_water,
        stopped_generations=stopped,
        voided_generations=voided,
        last_fencing_token=fencing,
    )


def test_an_agreeing_pair_is_aligned_and_writes_nothing() -> None:
    decision = decide_generation_adoption(11, _head(), node_id=NODE)
    assert decision.outcome == ADOPTION_ALIGNED
    assert decision.adopted_generation is None
    assert decision.previous_stop_evidence is None


def test_a_node_that_prepared_and_stopped_the_ledger_generation_is_adoptable() -> None:
    decision = decide_generation_adoption(1, _head(), node_id=NODE)
    assert decision.outcome == ADOPTION_ADOPTABLE
    assert decision.adopted_generation == 11
    # The node's own testimony, not the operator's, is what the audit row cites.
    assert decision.previous_stop_evidence == PREVIOUS_STOPPED


def test_a_voided_prepare_counts_as_not_serving() -> None:
    # Generation 1's prepare never ran on the node, so the ledger holds a generation the
    # node cannot stop; the node says so itself and that is testimony, not a guess.
    decision = decide_generation_adoption(1, _head(stopped=(), voided=(1,)), node_id=NODE)
    assert decision.outcome == ADOPTION_ADOPTABLE
    assert decision.previous_stop_evidence == PREVIOUS_VOIDED


def test_absent_testimony_refuses_unless_the_operator_accepts_it() -> None:
    refused = decide_generation_adoption(1, _head(stopped=(), voided=()), node_id=NODE)
    assert refused.outcome == ADOPTION_REFUSED
    assert refused.code == "previous_generation_stop_unconfirmed"
    # The refusal names the flag and what the audit row will then say, so the operator
    # cannot read it as "the platform thinks this is safe".
    assert "--accept-missing-stop-evidence" in (refused.reason or "")
    assert PREVIOUS_STOP_MISSING in (refused.reason or "")

    accepted = decide_generation_adoption(
        1, _head(stopped=(), voided=()), node_id=NODE, accept_missing_stop_evidence=True
    )
    assert accepted.outcome == ADOPTION_ADOPTABLE
    assert accepted.previous_stop_evidence == PREVIOUS_STOP_MISSING


def test_the_ledger_is_never_moved_backwards() -> None:
    decision = decide_generation_adoption(20, _head(current=11), node_id=NODE)
    assert decision.outcome == ADOPTION_REFUSED
    assert decision.code == "ledger_ahead_of_node"


def test_a_node_without_any_intent_has_nothing_to_adopt() -> None:
    decision = decide_generation_adoption(1, NodeInstanceHead(INSTANCE, None, None), node_id=NODE)
    assert decision.outcome == ADOPTION_REFUSED
    assert decision.code == "node_has_no_intent"


def test_a_self_contradictory_or_tokenless_head_cannot_be_cited() -> None:
    contradictory = decide_generation_adoption(1, _head(high_water=7), node_id=NODE)
    assert contradictory.outcome == ADOPTION_REFUSED
    assert contradictory.code == "node_head_incomplete"

    tokenless = decide_generation_adoption(1, _head(fencing=None), node_id=NODE)
    assert tokenless.outcome == ADOPTION_REFUSED
    assert tokenless.code == "node_head_incomplete"
    # An invented token would attest an epoch nobody holds; the refusal says so.
    assert "fencing token" in (tokenless.reason or "")


def test_the_decision_names_the_numbers_an_operator_has_to_compare() -> None:
    decision = decide_generation_adoption(1, _head(), node_id=NODE)
    reason = decision.reason or ""
    for fact in ("generation 11", "generation 1", NODE, "fencing token 20"):
        assert fact in reason


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


def test_the_head_response_carries_the_testimony_the_decision_needs(
    controller: tuple[str, type[_HeadHandler]],
) -> None:
    url, handler = controller
    handler.document = {
        "instance_id": INSTANCE,
        "current_generation": 11,
        "generation_high_water": 11,
        "last_fencing_token": 20,
        "stopped_generations": [3, 1],
        "voided_generations": [2],
    }

    head = read_head(ControllerEndpoint(url, "token"), node_id=NODE, instance_id=INSTANCE)

    assert head.stopped_generations == (1, 3)
    assert head.voided_generations == (2,)
    assert head.last_fencing_token == 20
    assert decide_generation_adoption(1, head, node_id=NODE).outcome == ADOPTION_ADOPTABLE


def test_a_head_with_a_malformed_generation_list_is_rejected(
    controller: tuple[str, type[_HeadHandler]],
) -> None:
    url, handler = controller
    handler.document = {
        "instance_id": INSTANCE,
        "current_generation": 11,
        "stopped_generations": [0],
    }
    with pytest.raises(ControllerConsultationError) as raised:
        read_head(ControllerEndpoint(url, "token"), node_id=NODE, instance_id=INSTANCE)
    assert raised.value.code == "controller_head_invalid"

    handler.document = {
        "instance_id": INSTANCE,
        "current_generation": 11,
        "voided_generations": "2",
    }
    with pytest.raises(ControllerConsultationError) as names:
        read_head(ControllerEndpoint(url, "token"), node_id=NODE, instance_id=INSTANCE)
    assert names.value.code == "controller_head_invalid"
