"""The file command consumes the target guard before audio I/O or native dispatch."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from speechnexus_gateway.domain.asr import AsrCallError, AsrExecutionTarget, AsrFailure
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError, SubmitRequest
from speechnexus_gateway.infrastructure.vllm_asr import (
    VllmAsrEndpoint,
    VllmAsrRecognizer,
    load_controlled_asr_endpoint,
)
from tests.unit.gateway.test_file_asr_command import consumer
from tests.unit.gateway.test_file_asr_command import setup as setup
from tests.unit.gateway.test_vllm_asr import response_bytes


def target(request: SubmitRequest) -> AsrExecutionTarget:
    identity = request.identity
    return AsrExecutionTarget(
        identity.host_node_id,
        identity.gateway_id,
        identity.pool_id,
        identity.instance_id,
        identity.instance_generation,
        identity.binding_revision_id,
    )


def endpoint(request: SubmitRequest) -> VllmAsrEndpoint:
    return VllmAsrEndpoint(
        "127.0.0.1",
        8000,
        "synthetic-asr",
        request.identity.binding_revision_id,
        bearer_token="ab" * 32,
        execution_target=target(request),
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("host_node_id", "node-other"),
        ("gateway_id", "gateway-other"),
        ("pool_id", "asr-realtime"),
        ("instance_id", "instance-other"),
        ("instance_generation", 2),
        ("binding_revision_id", "binding-other"),
    ],
)
async def test_any_foreign_target_is_rejected_before_object_reads_or_native_calls(
    setup: Any,
    name: str,
    value: object,
) -> None:
    request, objects, _ = setup
    calls: list[httpx.Request] = []

    def unexpected(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        raise AssertionError("foreign target must not dispatch")

    recognizer = VllmAsrRecognizer(endpoint(request), transport=httpx.MockTransport(unexpected))
    changed = replace(request, identity=replace(request.identity, **{name: value}))
    command = consumer(objects, recognizer)
    with pytest.raises(InvalidExecutionRequestError):
        command.validate_request(changed)
    with pytest.raises(InvalidExecutionRequestError):
        async with command.open(changed, cancelled=lambda: False):
            pytest.fail("foreign target must not open")
    assert objects.reads == calls == []


async def test_controlled_target_reaches_native_adapter_with_the_generation_credential(
    setup: Any,
) -> None:
    request, objects, _ = setup
    calls: list[httpx.Request] = []
    configured = endpoint(request)

    def native(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        assert call.headers["Authorization"] == "Bearer " + "ab" * 32
        request_id = call.headers["X-Request-ID"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": request_id},
            content=response_bytes().replace(
                b"transcribe-request-1", ("transcribe-" + request_id).encode()
            ),
        )

    recognizer = VllmAsrRecognizer(configured, transport=httpx.MockTransport(native))
    async with consumer(objects, recognizer).open(request, cancelled=lambda: False) as run:
        results = [segment async for segment in run]
    assert len(calls) == len(results) == 2
    assert objects.reads


async def test_legacy_unscoped_native_endpoint_is_diagnostic_only_and_cannot_consume_commands(
    setup: Any,
) -> None:
    request, objects, _ = setup
    legacy = replace(endpoint(request), execution_target=None, bearer_token=None)
    recognizer = VllmAsrRecognizer(legacy)
    with pytest.raises(InvalidExecutionRequestError):
        consumer(objects, recognizer).validate_request(request)
    assert objects.reads == []


async def test_target_change_during_input_reading_is_caught_before_native_dispatch(
    setup: Any,
) -> None:
    request, objects, _ = setup
    recognizer = VllmAsrRecognizer(endpoint(request))
    objects.on_open = lambda: setattr(
        recognizer,
        "_endpoint",
        replace(
            endpoint(request), execution_target=replace(target(request), instance_generation=2)
        ),
    )
    with pytest.raises(AsrCallError) as failure:
        async with consumer(objects, recognizer).open(request, cancelled=lambda: False):
            pytest.fail("target drift must not open media")
    assert failure.value.code is AsrFailure.RESPONSE_INVALID
    assert failure.value.request_may_have_run is False


def descriptor(request: SubmitRequest) -> dict[str, Any]:
    return {
        "protocol": "vllm_api_key_v1",
        "target": asdict(target(request)),
        "address": "127.0.0.1",
        "port": 8000,
        "served_model": "synthetic-asr",
        "bearer_token": "ab" * 32,
    }


def write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


def test_private_deployment_descriptor_loads_a_frozen_target_without_exposing_its_key(
    tmp_path: Path,
    setup: Any,
) -> None:
    request, _, _ = setup
    configured = load_controlled_asr_endpoint(
        write(tmp_path / "endpoint.json", descriptor(request))
    )
    assert configured == endpoint(request)
    assert "ab" * 32 not in repr(configured)


@pytest.mark.parametrize(
    "drift", ["mode", "symlink", "size", "unknown", "protocol", "duplicate", "token"]
)
def test_bad_deployment_descriptor_is_refused_with_a_sanitized_error(
    tmp_path: Path,
    setup: Any,
    drift: str,
) -> None:
    request, _, _ = setup
    document = descriptor(request)
    path = write(tmp_path / "endpoint.json", document)
    if drift == "mode":
        path.chmod(0o644)
    elif drift == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif drift == "size":
        path.write_text(" " * 16385)
    elif drift == "unknown":
        document["arbitrary_endpoint"] = "https://not-a-configured-engine"
        write(path, document)
    elif drift == "protocol":
        document["protocol"] = "not-approved"
        write(path, document)
    elif drift == "duplicate":
        path.write_text(json.dumps(document)[:-1] + ', "port": 8001}')
    else:
        document["bearer_token"] = "private-invalid-token"
        write(path, document)
    with pytest.raises(ValueError) as failure:
        load_controlled_asr_endpoint(path)
    assert str(failure.value) == "invalid private controlled ASR deployment descriptor"


@pytest.mark.parametrize("token", [None, "short", "A" * 64])
def test_controlled_endpoint_cannot_omit_or_weaken_native_generation_auth(
    setup: Any, token: str | None
) -> None:
    request, _, _ = setup
    with pytest.raises(ValueError, match="generation credential"):
        replace(endpoint(request), bearer_token=token)
