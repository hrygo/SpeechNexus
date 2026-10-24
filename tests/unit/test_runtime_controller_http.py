"""Transport seams verify bounds/correlation; real TLS and DB behavior are separate."""

import asyncio
import json
import ssl
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from speechnexus_backend.speech.domain.runtime_controller import (
    ControllerCallError,
    ControllerFailure,
)
from speechnexus_backend.speech.domain.runtime_intents import RuntimeDeadline
from speechnexus_backend.speech.infrastructure.runtime_codec import prepare_request
from speechnexus_backend.speech.infrastructure.runtime_controller_http import (
    HttpRuntimeController,
    RuntimeControllerEndpoint,
)
from tests.fake.runtime_controller import ResponseChunks, observation
from tests.fake.runtime_intents import REQUEST

TOKEN = "synthetic-component-token"
ENDPOINT = RuntimeControllerEndpoint("host-test", "127.0.0.1", 8443, TOKEN)


def response(
    request: httpx.Request,
    *,
    body: Any = None,
    status: int = 200,
    raw: bytes | None = None,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    stream: ResponseChunks | None = None,
) -> httpx.Response:
    data = (
        raw if raw is not None else json.dumps(body if body is not None else observation()).encode()
    )
    actual = (
        headers
        if headers is not None
        else {
            "content-type": "application/json",
            "x-request-id": request.headers["x-request-id"],
        }
    )
    return httpx.Response(status, headers=actual, stream=stream or ResponseChunks(data))


@pytest.mark.parametrize(
    "address", ["10.1.2.3", "172.31.0.1", "192.168.1.1", "127.0.0.1", "::1", "fd12::5"]
)
def test_fixed_private_literals_require_https(address: str) -> None:
    endpoint = replace(ENDPOINT, address=address)
    assert endpoint.base_url.startswith("https://")
    assert TOKEN not in repr(endpoint)
    assert address not in repr(endpoint)


@pytest.mark.parametrize(
    "address",
    [
        "controller.invalid",
        "https://10.0.0.1",
        "8.8.8.8",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "100.64.0.1",
        "192.0.2.1",
        "::",
        "ff02::1",
        "fe80::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "fd12::1%eth0",
    ],
)
def test_unapproved_address_forms_are_rejected(address: str) -> None:
    with pytest.raises(ValueError, match="IP literal"):
        replace(ENDPOINT, address=address)


@pytest.mark.parametrize(
    "changes",
    [
        {"port": True},
        {"port": 0},
        {"port": 65536},
        {"bearer_token": "short"},
        {"bearer_token": "a" * 4097},
        {"bearer_token": "synthetic token contains spaces"},
        {"bearer_token": TOKEN + "\n"},
        {"node_id": "../node"},
    ],
)
def test_invalid_config_fails_before_building_a_transport(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(ENDPOINT, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
        {"timeout_seconds": 1801},
        {"max_in_flight": 0},
        {"max_in_flight": True},
        {"max_in_flight": 17},
        {"max_waiting": 0},
        {"max_waiting": 65},
        {"max_response_bytes": 0},
        {"max_response_bytes": 65537},
    ],
)
def test_unbounded_transport_config_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="bounded"):
        HttpRuntimeController(ENDPOINT, **({"timeout_seconds": 1} | changes))


@pytest.mark.parametrize("change", ["certificate", "hostname", "version"])
def test_tls_verification_cannot_be_disabled(change: str) -> None:
    context = ssl.create_default_context()
    if change == "certificate":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif change == "hostname":
        context.check_hostname = False
    else:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    with pytest.raises(ValueError, match="verification"):
        HttpRuntimeController(ENDPOINT, timeout_seconds=1, ssl_context=context)


async def test_complete_original_command_and_correlated_observation_without_retries() -> None:
    calls: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "POST":
            assert request.content == prepare_request(REQUEST).document.encode()
            assert request.url.path.endswith("/host-test/instance-intents")
            return response(request, body=observation(replay=True))
        assert request.content == b""
        assert request.url.path.endswith("/host-test/instance-intents/runtime-new")
        return response(request, body=observation(ready=True, replay=None))

    async with HttpRuntimeController(
        ENDPOINT, timeout_seconds=1, _transport=httpx.MockTransport(handle)
    ) as client:
        warming = await client.submit(REQUEST)
        assert warming.observation.observed_instance_state == "warming_up"
        assert warming.observation.matches(REQUEST)
        assert '"idempotent_replay":true' in warming.original_document
        assert warming.original_document not in repr(warming)
        ready = await client.query(REQUEST)
        assert ready.observation.observed_instance_state == "ready"
        assert not hasattr(ready, "readiness")
    assert len(calls) == 2
    assert all(call.url.scheme == "https" and call.url.host == "127.0.0.1" for call in calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_node_id": "other-host"},
        {"pool_id": "asr-batch"},
        {"gateway_id": "other-gateway"},
        {"approved_template_id": "other-template"},
        {"binding_revision_id": "other-binding"},
        {"manifest_id": "other-manifest"},
        {"config_digest": "sha256:" + "f" * 64},
        {"instance_id": "other-instance"},
        {"instance_generation": 2},
        {"fencing_token": 8},
        {"intent_id": "other-intent"},
    ],
)
async def test_no_observation_from_another_identity_is_adopted(changes: dict[str, Any]) -> None:
    async with HttpRuntimeController(
        ENDPOINT,
        timeout_seconds=1,
        _transport=httpx.MockTransport(
            lambda request: response(request, body={**observation(), **changes})
        ),
    ) as client:
        with pytest.raises(ControllerCallError) as failure:
            await client.submit(REQUEST)
    assert failure.value.failure == ControllerFailure.INVALID_RESPONSE
    assert failure.value.request_may_have_run


@pytest.mark.parametrize(
    "kind",
    ["memory", "stop-without-evidence", "failed-without-error", "get-replay-flag", "extra-field"],
)
async def test_incomplete_or_inconsistent_facts_do_not_return_an_observation(kind: str) -> None:
    original = replace(REQUEST, action="stop") if kind == "stop-without-evidence" else REQUEST
    value = observation(original, ready=True, replay=None)
    if kind == "memory":
        value["reserved_memory"]["peak"]["unified_memory_bytes"] += 1
    elif kind == "stop-without-evidence":
        value.pop("stop_evidence")
    elif kind == "failed-without-error":
        value.update(intent_state="failed", observed_instance_state="failed")
    elif kind == "get-replay-flag":
        value["idempotent_replay"] = False
    else:
        value["private_field"] = True
    async with HttpRuntimeController(
        ENDPOINT,
        timeout_seconds=1,
        _transport=httpx.MockTransport(lambda request: response(request, body=value)),
    ) as client:
        with pytest.raises(ControllerCallError) as failure:
            await client.query(original)
    assert failure.value.failure == ControllerFailure.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (401, "component_credential_invalid", ControllerFailure.REJECTED),
        (409, "stale_fencing_token", ControllerFailure.REJECTED),
        (422, "invalid_runtime_request", ControllerFailure.REJECTED),
        (503, "runtime_controller_unavailable", ControllerFailure.UNAVAILABLE),
        (404, "runtime_intent_not_found", ControllerFailure.NOT_FOUND),
    ],
)
async def test_declared_errors_never_imply_absence_or_physical_release(
    status: int, code: str, kind: ControllerFailure
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(
            request,
            status=status,
            body={
                "error": {"code": code, "message": "synthetic-private-marker", "retryable": True}
            },
        )

    async with HttpRuntimeController(
        ENDPOINT, timeout_seconds=1, _transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(ControllerCallError) as failure:
            await (client.query(REQUEST) if status == 404 else client.submit(REQUEST))
    assert calls == 1
    assert failure.value.failure == kind
    assert failure.value.controller_code == code
    assert failure.value.request_may_have_run
    assert "synthetic-private-marker" not in str(failure.value)
    assert "synthetic-private-marker" not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize(
    "kind",
    [
        "redirect",
        "wrong-correlation",
        "missing-correlation",
        "wrong-media",
        "compressed",
        "large-length",
        "duplicate-header",
        "bad-json",
        "duplicate-json",
        "nonfinite",
        "bad-utf8",
        "over-budget",
    ],
)
async def test_bad_or_unbounded_responses_close_the_stream_and_never_retry(kind: str) -> None:
    streams: list[ResponseChunks] = []
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        value = json.dumps(observation()).encode()
        headers = {
            "x-request-id": request.headers["x-request-id"],
            "content-type": "application/json",
        }
        status = 200
        if kind == "redirect":
            status = 307
            headers["location"] = "https://unapproved.invalid/private"
        elif kind == "wrong-correlation":
            headers["x-request-id"] = "other-request"
        elif kind == "missing-correlation":
            headers.pop("x-request-id")
        elif kind == "wrong-media":
            headers["content-type"] = "text/html"
        elif kind == "compressed":
            headers["content-encoding"] = "gzip"
        elif kind == "large-length":
            headers["content-length"] = "9999999"
        elif kind == "bad-json":
            value = b"not json"
        elif kind == "duplicate-json":
            value = value[:-1] + b',"intent_id":"runtime-new"}'
        elif kind == "nonfinite":
            value = value[:-1] + b',"observed_peak_memory":NaN}'
        elif kind == "bad-utf8":
            value = b"\xff"
        elif kind == "over-budget":
            value = b" " * 65537
        stream = ResponseChunks(value)
        streams.append(stream)
        header_values = list(headers.items())
        if kind == "duplicate-header":
            header_values.append(("x-request-id", headers["x-request-id"]))
        return response(request, status=status, headers=header_values, stream=stream)

    async with HttpRuntimeController(
        ENDPOINT, timeout_seconds=1, _transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(ControllerCallError) as failure:
            await client.submit(REQUEST)
    assert calls == 1
    assert streams[0].closed
    assert failure.value.failure == ControllerFailure.INVALID_RESPONSE
    assert failure.value.request_may_have_run


async def test_slow_drip_is_bounded_by_the_entire_call_deadline() -> None:
    stream = ResponseChunks(*(b" " for _ in range(1000)), delay=0.001)
    async with HttpRuntimeController(
        ENDPOINT,
        timeout_seconds=0.03,
        _transport=httpx.MockTransport(lambda request: response(request, stream=stream)),
    ) as client:
        with pytest.raises(ControllerCallError) as failure:
            await client.submit(REQUEST)
    assert failure.value.failure == ControllerFailure.DEADLINE_EXCEEDED
    assert failure.value.request_may_have_run
    assert stream.yielded < 1000 and stream.closed


async def test_queue_and_concurrency_are_bounded_and_external_cancellation_propagates() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return response(request, body=observation(replay=None))

    async with HttpRuntimeController(
        ENDPOINT,
        timeout_seconds=2,
        max_in_flight=1,
        max_waiting=1,
        _transport=httpx.MockTransport(handle),
    ) as client:
        first = asyncio.create_task(client.query(REQUEST))
        await entered.wait()
        queued = asyncio.create_task(client.query(REQUEST))
        await asyncio.sleep(0)
        with pytest.raises(ControllerCallError) as full:
            await client.query(REQUEST)
        assert full.value.failure == ControllerFailure.BUSY
        assert not full.value.request_may_have_run
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await client.query(REQUEST)
        assert result.observation.matches(REQUEST)
    assert calls == 2


async def test_expired_submit_never_sends_but_the_original_query_remains_available() -> None:
    original = replace(REQUEST, deadline=RuntimeDeadline("2020-01-01T00:00:00Z"))
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        return response(request, body=observation(original, replay=None))

    async with HttpRuntimeController(
        ENDPOINT, timeout_seconds=1, _transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(ControllerCallError) as failure:
            await client.submit(original)
        assert failure.value.failure == ControllerFailure.DEADLINE_EXCEEDED
        assert not failure.value.request_may_have_run
        assert (await client.query(original)).observation.matches(original)
    assert calls == 1


async def test_expiry_while_queued_is_not_a_dispatch_permission() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return response(request, body=observation(replay=None))

    expires = datetime.now(UTC) + timedelta(milliseconds=50)
    original = replace(REQUEST, deadline=RuntimeDeadline(expires.isoformat()))
    async with HttpRuntimeController(
        ENDPOINT, timeout_seconds=2, max_in_flight=1, _transport=httpx.MockTransport(handle)
    ) as client:
        first = asyncio.create_task(client.query(REQUEST))
        await entered.wait()
        with pytest.raises(ControllerCallError) as failure:
            await client.submit(original)
        assert failure.value.failure == ControllerFailure.DEADLINE_EXCEEDED
        assert not failure.value.request_may_have_run
        release.set()
        await first
    assert calls == 1


async def test_changed_tls_context_wrong_node_and_closed_client_send_nothing() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(request)

    context = ssl.create_default_context()
    client = HttpRuntimeController(
        ENDPOINT, timeout_seconds=1, ssl_context=context, _transport=httpx.MockTransport(handle)
    )
    other = replace(REQUEST, binding=replace(REQUEST.binding, target_node_id="other-node"))
    for original in [other, REQUEST]:
        if original is REQUEST:
            context.check_hostname = False
        with pytest.raises(ControllerCallError) as failure:
            await client.submit(original)
        assert failure.value.failure == ControllerFailure.INVALID_REQUEST
        assert not failure.value.request_may_have_run
    await client.aclose()
    with pytest.raises(ControllerCallError) as closed:
        await client.query(REQUEST)
    assert closed.value.failure == ControllerFailure.CLOSED
    assert calls == 0


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("synthetic-private-marker"), httpx.ReadTimeout("synthetic-private-marker")],
)
async def test_transport_failures_do_not_leak_diagnostics_or_retry(error: httpx.HTTPError) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error

    async with HttpRuntimeController(
        ENDPOINT, timeout_seconds=1, _transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(ControllerCallError) as failure:
            await client.submit(REQUEST)
    assert failure.value.request_may_have_run
    assert "synthetic-private-marker" not in "".join(traceback.format_exception(failure.value))
    assert calls == 1
