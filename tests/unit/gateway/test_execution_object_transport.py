"""Run the actual fixed-origin HTTPX client; control only its network transport."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from speechnexus_gateway.domain.execution_inputs import ExecutionInputError
from speechnexus_gateway.infrastructure.execution_objects_minio import (
    MinioExecutionInputs,
    _FixedObjectTransport,
)
from tests.unit.gateway.test_execution_objects import BODY, LIMITS, Stream, reference


class ReadForbiddenStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read_calls = self.close_calls = 0
        self.fail_close = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        self.read_calls += 1
        raise AssertionError("unexpected body must not be read")

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise OSError("synthetic-private-detail")


def adapter(tmp_path: Path) -> MinioExecutionInputs:
    tmp_path.chmod(0o700)
    return MinioExecutionInputs(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-input-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        spool_directory=tmp_path,
    )


@pytest.mark.parametrize("method", ["HEAD", "GET"])
@pytest.mark.parametrize(
    "status,headers",
    [
        (403, {"Content-Length": str(1024**3)}),
        (500, {"Transfer-Encoding": "chunked"}),
        (403, {"Content-Encoding": "gzip", "Content-Length": str(1024**3)}),
        (307, {"Location": "http://8.8.8.8/forbidden"}),
        (204, {}),
        (206, {}),
    ],
)
async def test_actual_client_rejects_status_before_error_body_read_or_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    status: int,
    headers: dict[str, str],
) -> None:
    stream = ReadForbiddenStream()
    calls: list[httpx.Request] = []

    async def respond(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        calls.append(request)
        if request.method == method:
            return httpx.Response(status, headers=headers, stream=stream)
        assert method == "GET" and request.method == "HEAD"
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(BODY)), "x-amz-version-id": "v1"},
            stream=ReadForbiddenStream(),
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    value = adapter(tmp_path)
    with pytest.raises(ExecutionInputError, match="execution_input_unavailable"):
        async with value.open_verified(reference(), limits=LIMITS, cancelled=lambda: False):
            pytest.fail("unexpected body reached consumer")
    assert stream.read_calls == 0 and stream.close_calls == 1
    assert [request.method for request in calls] == (
        ["HEAD"] if method == "HEAD" else ["HEAD", "GET"]
    )
    assert all(request.url.host == "127.0.0.1" for request in calls)
    assert all(request.headers["Accept-Encoding"] == "identity" for request in calls)
    assert all(
        request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ") for request in calls
    )


async def test_actual_client_closes_transport_after_response_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = ReadForbiddenStream()
    response.fail_close = True
    closed: list[httpx.AsyncHTTPTransport] = []
    original_close = httpx.AsyncHTTPTransport.aclose

    async def respond(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return httpx.Response(500, headers={"Transfer-Encoding": "chunked"}, stream=response)

    async def close(transport: httpx.AsyncHTTPTransport) -> None:
        closed.append(transport)
        await original_close(transport)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    with pytest.raises(ExecutionInputError) as caught:
        async with adapter(tmp_path).open_verified(
            reference(), limits=LIMITS, cancelled=lambda: False
        ):
            pytest.fail("unexpected body reached consumer")
    assert str(caught.value) == "execution_input_unavailable"
    assert response.close_calls == 1 and response.read_calls == 0 and len(closed) == 1


async def test_actual_client_never_reads_head_and_closes_get_before_exposing_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, body = ReadForbiddenStream(), Stream()
    calls: list[httpx.Request] = []

    async def respond(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(BODY)), "x-amz-version-id": "v1"},
            stream=head if request.method == "HEAD" else body,
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    # Hostile proxy settings must not become part of either actual client/transport.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    async with adapter(tmp_path).open_verified(
        reference(), limits=LIMITS, cancelled=lambda: False
    ) as verified:
        assert verified.stream.read() == BODY
        assert body.closed and head.close_calls == 1 and head.read_calls == 0
    assert [request.method for request in calls] == ["HEAD", "GET"]
    assert calls[1].url.params["versionId"] == "v1"


@pytest.mark.parametrize(
    "method,url",
    [
        ("HEAD", "http://8.8.8.8/private/file"),
        ("GET", "http://127.0.0.1:59001/private/file"),
        ("HEAD", "https://127.0.0.1:59000/private/file"),
        ("PUT", "http://127.0.0.1:59000/private/file"),
        ("DELETE", "http://127.0.0.1:59000/private/file"),
    ],
)
async def test_fixed_transport_refuses_other_origins_and_writes_before_network(
    method: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        pytest.fail("rejected request reached network transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    async with httpx.AsyncClient(
        transport=_FixedObjectTransport("http://127.0.0.1:59000", None), trust_env=False
    ) as client:
        with pytest.raises(ExecutionInputError, match="execution_input_origin_rejected"):
            await client.request(method, url)
