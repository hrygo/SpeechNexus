"""A node's checkpoint-backed endpoint is the sole HTTP source of instance stop facts."""

import copy
import json
import ssl
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from typing import Any

import httpx
import pytest

from speechnexus_gateway.domain.recovery import whole_instance_stop_evidence
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionState,
    SubmissionIntent,
)
from speechnexus_gateway.infrastructure.node_stop_evidence import (
    NodeStopBinding,
    NodeStopEndpoint,
    NodeStopEvidenceSource,
)
from speechnexus_node_controller.domain.intents import (
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeInstanceIntent,
    RuntimeIntentState,
    RuntimeStopEvidence,
)
from speechnexus_node_controller.domain.payloads import intent_response_payload
from tests.fake.controller import FIXED_NOW, build_request
from tests.unit.gateway.test_execution_recovery_sweep import identity_for

NOW = FIXED_NOW
ENDPOINT = NodeStopEndpoint("node-local-1", "127.0.0.1", 9443, "synthetic-component-token")
BINDING = NodeStopBinding(
    "node-local-1",
    "gateway-1",
    "asr-batch",
    "binding-rev-1",
    "template-qwen3-asr-0p6b",
    "manifest-1",
    "sha256:" + "a" * 64,
)


def intent(**changes: Any) -> SubmissionIntent:
    identity = identity_for(
        pool_id=BINDING.pool_id,
        host_node_id=BINDING.node_id,
        gateway_id=BINDING.gateway_id,
        instance_id="instance-asr-1",
    )
    fields: dict[str, Any] = {
        "identity": identity,
        "recorded_at": NOW,
        "updated_at": NOW,
        "request_digest": "sha256:" + "b" * 64,
        "execution_state": ExecutionState.UNKNOWN_QUARANTINED,
        "cleanup_state": CleanupState.QUARANTINED,
    }
    fields.update(changes)
    return SubmissionIntent(**fields)


def response_body() -> dict[str, Any]:
    at = NOW + timedelta(seconds=30)
    original = RuntimeInstanceIntent(
        request=build_request(
            intent_id="original-stop", action=RuntimeActionKind.STOP, fencing_token=2
        ),
        created_at=NOW + timedelta(seconds=10),
        updated_at=NOW + timedelta(seconds=40),
        intent_state=RuntimeIntentState.SUCCEEDED,
        observed_instance_state=ObservedInstanceState.STOPPED,
        memory_hold=MemoryHold.NONE,
        stop_evidence=RuntimeStopEvidence(
            verified_at=at, exit_code=143, runtime_container_id="original-container"
        ),
    )
    return intent_response_payload(original)


class Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, delay: float = 0) -> None:
        self.data = data
        self.delay = delay
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        import asyncio

        for chunk in (
            [self.data] if not self.delay else [self.data[i : i + 1] for i in range(len(self.data))]
        ):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def source(
    handler: Callable[[httpx.Request], httpx.Response],
    **options: Any,
) -> NodeStopEvidenceSource:
    fields = {
        "bindings": (BINDING,),
        "ssl_context": ssl.create_default_context(),
        "_transport": httpx.MockTransport(handler),
        "now": lambda: NOW + timedelta(seconds=60),
        **options,
    }
    return NodeStopEvidenceSource(ENDPOINT, **fields)


def reply(request: httpx.Request, value: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "Content-Type": "application/json",
            "X-Request-ID": request.headers["X-Request-ID"],
        },
        stream=Body(json.dumps(value).encode()),
    )


def test_only_one_get_is_sent_and_source_fact_is_stable_across_repeated_queries() -> None:
    calls: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return reply(request, response_body())

    adapter = source(answer)
    original = intent()
    first = adapter.termination_evidence(original)
    again = adapter.termination_evidence(original)
    assert first is not None and first.is_complete and first == again
    assert len(calls) == 2
    assert all(
        request.method == "GET"
        and request.url.path.endswith("/instances/instance-asr-1/generations/1/verified-stop")
        and request.headers["Authorization"] == "Bearer synthetic-component-token"
        and request.headers["Accept-Encoding"] == "identity"
        and not request.content
        for request in calls
    )
    second = replace(original, identity=replace(original.identity, submit_intent_id="intent-2"))
    assert adapter.termination_evidence(second) == first
    one = whole_instance_stop_evidence(original, first)
    two = whole_instance_stop_evidence(second, first)
    assert one is not None and two is not None
    assert one.evidence_id != two.evidence_id and one.evidence_digest == two.evidence_digest
    assert "synthetic-component-token" not in repr(adapter)
    assert ENDPOINT.address not in repr(ENDPOINT)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_node_id": "other-node"},
        {"gateway_id": "other-gateway"},
        {"pool_id": "asr-realtime"},
        {"instance_id": "other-instance"},
        {"instance_generation": 2},
        {"binding_revision_id": "binding-other"},
        {"approved_template_id": "template-other"},
        {"manifest_id": "manifest-other"},
        {"config_digest": "sha256:" + "c" * 64},
        {"fencing_token": 0},
        {"action": "prepare"},
        {"intent_state": "reconciling"},
        {"observed_instance_state": "ready"},
        {"idempotent_replay": True},
        {"unrecognized": True},
    ],
)
def test_unrelated_or_non_terminal_observations_cannot_become_proof(
    changes: dict[str, Any],
) -> None:
    value = response_body()
    value.update(changes)
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return reply(request, value)

    assert source(answer).termination_evidence(intent()) is None
    assert calls == ["GET"]


@pytest.mark.parametrize(
    "changes",
    [
        {"process_or_container_exited": False},
        {"resources_released_confirmed": False},
        {"exit_code": None},
        {"exit_code": True},
        {"runtime_container_id": None},
        {"runtime_container_id": ""},
        {"verified_at": (NOW - timedelta(seconds=1)).isoformat()},
        {"verified_at": (NOW + timedelta(days=1)).isoformat()},
    ],
)
def test_missing_incomplete_or_unrelated_time_does_not_cover_an_execution(
    changes: dict[str, Any],
) -> None:
    value = response_body()
    value["stop_evidence"].update(changes)
    assert source(lambda request: reply(request, value)).termination_evidence(intent()) is None


def test_stop_token_must_cover_execution_in_the_same_pool() -> None:
    execution = intent(identity=replace(intent().identity, fencing_token=3))
    assert (
        source(lambda request: reply(request, response_body())).termination_evidence(execution)
        is None
    )
    execution.dispatched_at = NOW + timedelta(seconds=31)
    execution.identity = replace(execution.identity, fencing_token=1)
    assert (
        source(lambda request: reply(request, response_body())).termination_evidence(execution)
        is None
    )


@pytest.mark.parametrize("status", [301, 404, 401, 409, 422, 500, 503])
def test_no_http_failure_or_redirect_is_stop_proof_or_a_retry(status: int) -> None:
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            status,
            headers={"Location": "https://public.invalid"},
            stream=Body(b"not a stop fact"),
        )

    assert source(answer).termination_evidence(intent()) is None
    assert calls == ["GET"]


@pytest.mark.parametrize(
    "bad", ["duplicate", "oversize", "compression", "correlation", "truncated"]
)
def test_unbounded_or_ambiguous_transport_data_is_rejected(bad: str) -> None:
    body = Body(json.dumps(response_body()).encode())

    def answer(request: httpx.Request) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": request.headers["X-Request-ID"],
        }
        if bad == "duplicate":
            body.data = body.data.replace(b'{"', b'{"intent_id":"duplicate","', 1)
        elif bad == "oversize":
            body.data += b" " * 65536
        elif bad == "compression":
            headers["Content-Encoding"] = "gzip"
        elif bad == "correlation":
            headers["X-Request-ID"] = "another-call"
        elif bad == "truncated":
            headers["Content-Length"] = str(len(body.data) + 1)
        return httpx.Response(200, headers=headers, stream=body)

    assert source(answer).termination_evidence(intent()) is None
    assert body.closed


def test_a_slow_drip_is_bounded_by_the_entire_call_and_closes_the_response() -> None:
    body = Body(json.dumps(response_body()).encode(), delay=0.01)

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": request.headers["X-Request-ID"],
            },
            stream=body,
        )

    adapter = source(answer, timeout_seconds=0.03)
    start = time.monotonic()
    assert adapter.termination_evidence(intent()) is None
    assert time.monotonic() - start < 0.3
    assert body.closed


def test_wrong_scope_and_unapproved_binding_never_fetch() -> None:
    calls: list[str] = []

    def unexpected(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        raise AssertionError("an unrelated or invalid execution must not issue a request")

    adapter = source(unexpected)
    mutations: list[dict[str, Any]] = [
        {"host_node_id": "other-node"},
        {"gateway_id": "other-gateway"},
        {"pool_id": "asr-realtime"},
        {"binding_revision_id": "unapproved-binding"},
        {"instance_id": "../escape"},
    ]
    for changes in mutations:
        value = intent(identity=replace(intent().identity, **changes))
        assert adapter.termination_evidence(value) is None
    assert calls == []


@pytest.mark.parametrize("address", ["8.8.8.8", "169.254.0.1", "0.0.0.0", "::", "example.com"])
def test_non_private_or_ambiguous_endpoints_are_rejected(address: str) -> None:
    with pytest.raises(ValueError):
        replace(ENDPOINT, address=address)


def test_tls_and_immutable_binding_configuration_cannot_be_weakened() -> None:
    context = ssl.create_default_context()
    context.check_hostname = False
    with pytest.raises(ValueError):
        source(lambda request: reply(request, response_body()), ssl_context=context)
    with pytest.raises(ValueError):
        source(lambda request: reply(request, response_body()), bindings=(BINDING, BINDING))
    context.check_hostname = True
    adapter = source(lambda request: reply(request, response_body()), ssl_context=context)
    context.check_hostname = False
    assert adapter.termination_evidence(intent()) is None


def test_canonical_source_digest_normalizes_equivalent_utc_timestamps() -> None:
    value = response_body()
    first = source(lambda request: reply(request, copy.deepcopy(value))).termination_evidence(
        intent()
    )
    for name in ("created_at", "updated_at"):
        value[name] = value[name].replace("+00:00", "Z")
    value["stop_evidence"]["verified_at"] = value["stop_evidence"]["verified_at"].replace(
        "+00:00", "Z"
    )
    again = source(lambda request: reply(request, copy.deepcopy(value))).termination_evidence(
        intent()
    )
    assert first is not None and first == again


def test_concurrent_queries_are_bounded_without_an_unbounded_waiting_queue() -> None:
    entered, release = Event(), Event()
    calls: list[str] = []

    class WaitingBody(Body):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            import asyncio

            entered.set()
            assert await asyncio.to_thread(release.wait, 0.5)
            yield self.data

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": request.headers["X-Request-ID"],
            },
            stream=WaitingBody(json.dumps(response_body()).encode()),
        )

    adapter = source(answer, timeout_seconds=1, max_in_flight=1)
    with ThreadPoolExecutor(max_workers=1) as worker:
        running = worker.submit(adapter.termination_evidence, intent())
        try:
            assert entered.wait(0.5)
            assert adapter.termination_evidence(intent()) is None
            assert calls == ["GET"]
        finally:
            release.set()
        assert running.result(timeout=1) is not None
    assert adapter.termination_evidence(intent()) is not None
    assert calls == ["GET", "GET"]
