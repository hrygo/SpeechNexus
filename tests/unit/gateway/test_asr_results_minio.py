"""Operation-scoped S3 writes/readback; failures never return publication or stop facts."""

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from speechnexus_gateway.domain.asr import AsrCallError, AsrFailure
from speechnexus_gateway.domain.asr_results import AsrResultError, PreparedAsrResult
from speechnexus_gateway.domain.execution_inputs import ExecutionInputError
from speechnexus_gateway.infrastructure.asr_results_minio import MinioAsrResults, _FixedS3Transport

RESULT = PreparedAsrResult("workspace-one", "sha256:" + "a" * 64, b'{"text":"synthetic"}')


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    value = MinioAsrResults(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-output-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        timeout_seconds=10,
    )
    calls: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []
    state: dict[str, Any] = {
        "put_version": "v1",
        "read_version": "v1",
        "body": RESULT.body,
        "put_error": None,
        "head_error": None,
        "put_status": 200,
        "put_close_error": False,
        "put_body_read": False,
        "after_put": lambda: None,
    }

    class PutStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            state["put_body_read"] = True
            yield b"private-vendor-body"

        async def aclose(self) -> None:
            if state["put_close_error"]:
                raise ExecutionInputError("execution_input_unavailable")

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "PUT":
            state["after_put"]()
            if state["put_error"] is not None:
                raise state["put_error"]
            headers = {"Content-Length": "0"}
            if state["put_version"] is not None:
                headers["x-amz-version-id"] = state["put_version"]
            return httpx.Response(state["put_status"], headers=headers, stream=PutStream())
        if request.method == "HEAD" and state["head_error"] is not None:
            raise state["head_error"]
        if request.method == "GET":
            assert request.url.params["versionId"] == "v1"
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(state["body"])),
                "x-amz-version-id": state["read_version"],
            },
            stream=httpx.ByteStream(state["body"] if request.method == "GET" else b""),
        )

    def client() -> httpx.AsyncClient:
        result = httpx.AsyncClient(
            transport=httpx.MockTransport(respond), trust_env=False, follow_redirects=False
        )
        clients.append(result)
        return result

    monkeypatch.setattr(value, "_new_client", client)
    yield value, calls, state
    assert all(client.is_closed for client in clients)
    value.close()


async def test_exactly_one_private_put_then_complete_versioned_readback(adapter: Any) -> None:
    value, calls, _ = adapter
    receipt = await value.stage(RESULT, cancelled=lambda: False)
    assert receipt.reference == RESULT.reference("sn-output-test")
    assert receipt.storage_version == "v1" and receipt.request_digest == RESULT.request_digest
    assert [request.method for request in calls] == ["PUT", "HEAD", "GET"]
    assert calls[0].content == RESULT.body
    assert calls[0].headers["Content-Type"] == "application/json"
    assert calls[0].headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert all(request.url.host == "127.0.0.1" and request.url.port == 59000 for request in calls)


@pytest.mark.parametrize("version", [None, "", "null"])
async def test_unversioned_write_does_not_read_or_return_a_receipt(
    adapter: Any, version: Any
) -> None:
    value, calls, state = adapter
    state["put_version"] = version
    with pytest.raises(AsrResultError, match="asr_result_versioning_required"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert [request.method for request in calls] == ["PUT"]


async def test_lost_put_ack_never_retries_deletes_or_exposes_vendor_error(adapter: Any) -> None:
    value, calls, state = adapter
    state["put_error"] = TimeoutError("private-credential-and-content")
    with pytest.raises(AsrResultError) as caught:
        await value.stage(RESULT, cancelled=lambda: False)
    assert str(caught.value) == "asr_result_storage_unknown"
    assert [request.method for request in calls] == ["PUT"]
    assert caught.value.__suppress_context__


async def test_put_transport_input_error_is_still_an_unknown_write(adapter: Any) -> None:
    value, calls, state = adapter
    state["put_error"] = ExecutionInputError("execution_input_unavailable")
    with pytest.raises(AsrResultError, match="asr_result_storage_unknown"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert [request.method for request in calls] == ["PUT"]


async def test_put_close_error_is_unknown_without_reading_vendor_body(adapter: Any) -> None:
    value, calls, state = adapter
    state["put_close_error"] = True
    with pytest.raises(AsrResultError, match="asr_result_storage_unknown"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert [request.method for request in calls] == ["PUT"]
    assert not state["put_body_read"]


@pytest.mark.parametrize("status", [302, 403, 500])
async def test_put_http_error_remains_unknown_and_never_starts_readback(
    adapter: Any, status: int
) -> None:
    value, calls, state = adapter
    state["put_status"] = status
    with pytest.raises(AsrResultError, match="asr_result_storage_unknown"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert [request.method for request in calls] == ["PUT"]


async def test_readback_failure_never_returns_a_receipt_or_removes_unknown_object(
    adapter: Any,
) -> None:
    value, calls, state = adapter
    state["head_error"] = ExecutionInputError("execution_input_identity_mismatch")
    with pytest.raises(AsrResultError, match="asr_result_readback_failed"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert [request.method for request in calls] == ["PUT", "HEAD"]


async def test_wrong_readback_digest_is_rejected_independently_of_size_and_version(
    adapter: Any,
) -> None:
    value, calls, state = adapter
    state["body"] = b"x" * len(RESULT.body)
    with pytest.raises(AsrResultError, match="asr_result_readback_failed"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert [request.method for request in calls] == ["PUT", "HEAD", "GET"]


async def test_overwrite_between_put_and_readback_is_not_adopted(adapter: Any) -> None:
    value, calls, state = adapter
    state["read_version"] = "v2"
    with pytest.raises(AsrResultError, match="asr_result_identity_mismatch"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert [request.method for request in calls] == ["PUT", "HEAD"]


async def test_cancellation_before_write_never_calls_storage(adapter: Any) -> None:
    value, calls, _ = adapter
    with pytest.raises(AsrResultError, match="asr_result_cancelled"):
        await value.stage(RESULT, cancelled=lambda: True)
    assert not calls


async def test_original_deadline_guard_is_not_replaced_with_a_storage_error(adapter: Any) -> None:
    value, calls, state = adapter
    expired = False

    def expire() -> None:
        nonlocal expired
        expired = True

    def check() -> bool:
        if expired:
            raise AsrCallError(AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=True)
        return False

    state["after_put"] = expire
    with pytest.raises(AsrCallError, match=AsrFailure.DEADLINE_EXCEEDED):
        await value.stage(RESULT, cancelled=check)
    assert [request.method for request in calls] == ["PUT"]


async def test_transport_has_no_cross_origin_or_extra_methods() -> None:
    transport = _FixedS3Transport("http://127.0.0.1:59000", None)
    inner = Mock()
    transport._transport = inner
    for method, url in [
        ("PUT", "http://127.0.0.1:59001/sn-output-test/private/result"),
        ("DELETE", "http://127.0.0.1:59000/sn-output-test/private/result"),
        ("POST", "http://127.0.0.1:59000/sn-output-test/private/result?uploads"),
    ]:
        with pytest.raises(AsrResultError):
            await transport.handle_async_request(httpx.Request(method, url))
    assert not inner.handle_async_request.called


async def test_each_operation_owns_a_non_proxy_non_redirect_client() -> None:
    value = MinioAsrResults(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-output-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        timeout_seconds=1,
    )
    async with value._new_client() as first, value._new_client() as second:
        assert first is not second and first._transport is not second._transport
        assert not first.trust_env and not first.follow_redirects
        assert not second.trust_env and not second.follow_redirects
