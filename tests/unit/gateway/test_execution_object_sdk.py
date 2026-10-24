"""Real minio-py calls must never buffer an unexpected response body.

Only urllib3's underlying request is controlled. The SDK, its get/stat paths and
our fixed-origin transport run unchanged; the body guard fails on any read.
"""

import io
from pathlib import Path
from typing import Any

import pytest
import urllib3
from urllib3 import HTTPResponse

from speechnexus_gateway.domain.execution_inputs import ExecutionInputError
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs


class ReadForbiddenResponse(HTTPResponse):
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.read_calls = self.close_calls = self.release_calls = 0
        self.fail_close = self.fail_release = False
        super().__init__(
            body=io.BytesIO(b"synthetic-body-must-not-be-read"),
            status=status,
            headers={"Content-Type": "application/xml", **headers},
            preload_content=False,
        )

    def read(
        self,
        amt: int | None = None,
        decode_content: bool | None = None,
        cache_content: bool = False,
    ) -> bytes:
        self.read_calls += 1
        raise AssertionError("SDK attempted to consume an unexpected body")

    def close(self) -> None:
        self.close_calls += 1
        super().close()
        if self.fail_close:
            raise OSError("synthetic-private-detail")

    def release_conn(self) -> None:
        self.release_calls += 1
        super().release_conn()
        if self.fail_release:
            raise OSError("synthetic-private-detail")


def sdk_with_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: ReadForbiddenResponse,
) -> tuple[MinioExecutionInputs, list[dict[str, Any]]]:
    tmp_path.chmod(0o700)
    client = MinioExecutionInputs(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-sdk-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        spool_directory=tmp_path,
    )
    calls: list[dict[str, Any]] = []

    def request(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> HTTPResponse:
        calls.append({"method": method, **kwargs})
        return response

    monkeypatch.setattr(urllib3.PoolManager, "urlopen", request)
    return client, calls


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
def test_actual_sdk_rejects_status_before_any_error_body_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    status: int,
    headers: dict[str, str],
) -> None:
    response = ReadForbiddenResponse(status, headers)
    adapter, calls = sdk_with_response(tmp_path, monkeypatch, response)
    try:
        with pytest.raises(ExecutionInputError, match="execution_input_unavailable"):
            if method == "HEAD":
                adapter._client.stat_object("sn-sdk-test", "private/assets/synthetic/file")
            else:
                adapter._client.get_object(
                    "sn-sdk-test", "private/assets/synthetic/file", version_id="v1"
                )
        assert response.read_calls == 0
        assert response.close_calls == response.release_calls == 1
        assert len(calls) == 1 and calls[0]["method"] == method
        assert calls[0]["preload_content"] is False and calls[0]["decode_content"] is False
        assert calls[0]["redirect"] is False and calls[0]["pool_timeout"] == 3
    finally:
        adapter.close()


@pytest.mark.parametrize("failure", ["close", "release"])
def test_error_cleanup_attempts_both_steps_and_keeps_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    response = ReadForbiddenResponse(500, {"Transfer-Encoding": "chunked"})
    response.fail_close, response.fail_release = failure == "close", failure == "release"
    adapter, _ = sdk_with_response(tmp_path, monkeypatch, response)
    try:
        with pytest.raises(ExecutionInputError, match="execution_input_unavailable") as caught:
            adapter._client.get_object("sn-sdk-test", "private/assets/synthetic/file")
        assert str(caught.value) == "execution_input_unavailable"
        assert response.close_calls == response.release_calls == 1
        assert response.read_calls == 0
    finally:
        adapter.close()


def test_successful_sdk_head_only_consumes_headers_and_releases_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = ReadForbiddenResponse(200, {"Content-Length": "123", "x-amz-version-id": "v1"})
    adapter, calls = sdk_with_response(tmp_path, monkeypatch, response)
    try:
        info = adapter._client.stat_object("sn-sdk-test", "private/assets/synthetic/file")
        assert info.size == 123 and info.version_id == "v1"
        assert response.read_calls == 0
        assert response.close_calls == response.release_calls == 1
        assert calls[0]["preload_content"] is False
    finally:
        adapter.close()


def test_successful_sdk_get_remains_unread_for_the_bounded_object_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = ReadForbiddenResponse(200, {"Content-Length": "123", "x-amz-version-id": "v1"})
    adapter, calls = sdk_with_response(tmp_path, monkeypatch, response)
    try:
        actual = adapter._client.get_object(
            "sn-sdk-test", "private/assets/synthetic/file", version_id="v1"
        )
        assert actual is response
        assert response.read_calls == response.close_calls == response.release_calls == 0
        assert calls[0]["preload_content"] is False and calls[0]["decode_content"] is False
    finally:
        response.close()
        response.release_conn()
        adapter.close()
