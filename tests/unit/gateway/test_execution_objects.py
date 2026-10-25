"""Fixed-origin, complete-before-expose object reads and private spool lifetime."""

import hashlib
import io
import ssl
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import urllib3
from urllib3 import HTTPResponse
from urllib3._collections import HTTPHeaderDict

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputError,
    ExecutionInputLimits,
    ExecutionObjectReference,
)
from speechnexus_gateway.infrastructure import execution_objects_minio as objects
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from tests.fake.execution_command import command_example, original_request

BODY = b"synthetic-only" * 10000
LIMITS = ExecutionInputLimits(1000000, 8, 10)


def reference(body: bytes = BODY) -> ExecutionObjectReference:
    return ExecutionObjectReference(
        "asset-input",
        "ws-demo",
        "sn-input-test",
        "private/assets/asset-input/1234",
        "sha256:" + hashlib.sha256(body).hexdigest(),
        len(body),
    )


class Response:
    def __init__(self, body: bytes = BODY) -> None:
        self.status = 200
        self.headers = HTTPHeaderDict({"Content-Length": str(len(body)), "x-amz-version-id": "v1"})
        self.stream = io.BytesIO(body)
        self.closed = self.released = False
        self.fail_close = False

    def read1(self, amount: int, *, decode_content: bool) -> bytes:
        assert not decode_content
        return self.stream.read(amount)

    def close(self) -> None:
        self.closed = True
        self.stream.close()
        if self.fail_close:
            raise OSError("private-path-must-not-leak")

    def release_conn(self) -> None:
        self.released = True


@pytest.fixture
def adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[MinioExecutionInputs, Mock, Response]:
    tmp_path.chmod(0o700)
    value = MinioExecutionInputs(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-input-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        spool_directory=tmp_path,
    )
    response = Response()
    client = Mock()
    client.stat_object.return_value = Mock(version_id="v1", size=len(BODY))
    client.get_object.return_value = response
    monkeypatch.setattr(value, "_client", client)
    return value, client, response


def test_fixed_version_full_digest_eof_and_spool_lifetime(adapter: Any, tmp_path: Path) -> None:
    value, client, response = adapter
    with value.open_verified(reference(), limits=LIMITS, cancelled=lambda: False) as result:
        stream = result.stream
        assert stream.read() == BODY and stream.seek(0) == 0
        assert result.storage_version == "v1"
        assert response.closed and response.released and not stream.closed
        assert "private/assets" not in repr(result) and "v1" not in repr(result)
    assert stream.closed and not list(tmp_path.iterdir())
    client.get_object.assert_called_once_with(
        "sn-input-test",
        reference().object_key,
        version_id="v1",
        request_headers={"Accept-Encoding": "identity"},
    )


def test_partial_body_returns_to_cancellation_check_without_filling_a_chunk(
    adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, _, response = adapter
    stopped = False
    reads = 0

    def read1(amount: int, *, decode_content: bool) -> bytes:
        nonlocal stopped, reads
        assert not decode_content and amount > 0
        reads += 1
        stopped = True
        return BODY[:1]

    monkeypatch.setattr(response, "read1", read1)
    with (
        pytest.raises(ExecutionInputError, match="execution_input_cancelled"),
        value.open_verified(reference(), limits=LIMITS, cancelled=lambda: stopped),
    ):
        pytest.fail("partial cancelled input must not reach the consumer")
    assert reads == 1 and response.closed and response.released


def test_downstream_failure_keeps_its_identity_and_closes_spool(adapter: Any) -> None:
    value, _, response = adapter
    failure = RuntimeError("synthetic consumer failure")
    with (
        pytest.raises(RuntimeError) as caught,
        value.open_verified(reference(), limits=LIMITS, cancelled=lambda: False) as result,
    ):
        stream = result.stream
        assert response.closed and response.released
        raise failure
    assert caught.value is failure and stream.closed


@pytest.mark.parametrize(
    "failure",
    ["sha", "short", "long", "version", "length", "encoding", "status", "stat-size", "stat-null"],
)
def test_incomplete_or_wrong_object_never_reaches_consumer(adapter: Any, failure: str) -> None:
    value, client, response = adapter
    ref = reference()
    if failure == "sha":
        ref = replace(ref, content_sha256="sha256:" + "0" * 64)
    if failure == "short":
        response.stream = io.BytesIO(BODY[:-1])
    if failure == "long":
        response.stream = io.BytesIO(BODY + b"x")
    if failure in {"version", "length", "encoding"}:
        header = {
            "version": "x-amz-version-id",
            "length": "Content-Length",
            "encoding": "Content-Encoding",
        }[failure]
        response.headers[header] = "invalid"
    if failure == "status":
        response.status = 206
    if failure == "stat-size":
        client.stat_object.return_value.size += 1
    if failure == "stat-null":
        client.stat_object.return_value.version_id = "null"
    with (
        pytest.raises(ExecutionInputError, match="execution_input_identity_mismatch"),
        value.open_verified(ref, limits=LIMITS, cancelled=lambda: False),
    ):
        pytest.fail("unverified object was exposed")
    if client.get_object.called:
        assert response.closed and response.released


@pytest.mark.parametrize(
    "key",
    [
        "outside/file",
        "private/../file",
        "private//file",
        "private/a?b",
        "private/a#b",
        "private/a\\b",
        "private/a\n",
        "private/a\x7f",
    ],
)
def test_ambiguous_key_is_rejected_before_network(adapter: Any, key: str) -> None:
    value, client, _ = adapter
    with (
        pytest.raises(ExecutionInputError),
        value.open_verified(
            replace(reference(), object_key=key), limits=LIMITS, cancelled=lambda: False
        ),
    ):
        pytest.fail("bad key exposed")
    client.stat_object.assert_not_called()


@pytest.mark.parametrize("failure", ["scope", "limit", "cancel", "deadline", "transport", "close"])
def test_scope_budget_cancel_deadline_and_cleanup_are_safe(
    adapter: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    value, client, response = adapter
    ref, limits = reference(), LIMITS
    if failure == "scope":
        ref = replace(ref, storage_bucket="another-bucket")
    if failure == "limit":
        limits = replace(LIMITS, max_bytes=len(BODY) - 1)
    if failure == "deadline":
        instants = iter([0.0, 11.0])
        monkeypatch.setattr(objects, "monotonic", lambda: next(instants))
    if failure == "transport":
        client.stat_object.side_effect = OSError("private-path-must-not-leak")
    if failure == "close":
        response.fail_close = True
    with (
        pytest.raises(ExecutionInputError) as caught,
        value.open_verified(ref, limits=limits, cancelled=lambda: failure == "cancel"),
    ):
        pytest.fail("failed input exposed")
    assert "private-path" not in str(caught.value)
    if failure in {"scope", "limit", "cancel", "deadline"}:
        client.stat_object.assert_not_called()
    if failure == "close":
        assert response.closed and response.released


def test_cancel_mid_read_survives_remote_cleanup_error(adapter: Any) -> None:
    value, _, response = adapter
    response.fail_close = True
    with (
        pytest.raises(ExecutionInputError, match="execution_input_cancelled"),
        value.open_verified(
            reference(), limits=LIMITS, cancelled=lambda: response.stream.tell() > 0
        ),
    ):
        pytest.fail("cancelled input exposed")
    assert response.closed and response.released


def test_cancel_during_remote_close_never_exposes_prepared_content(adapter: Any) -> None:
    value, _, response = adapter
    with (
        pytest.raises(ExecutionInputError, match="execution_input_cancelled"),
        value.open_verified(reference(), limits=LIMITS, cancelled=lambda: response.closed),
    ):
        pytest.fail("cancelled input exposed after close")
    assert response.closed and response.released


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "http://example.com",
        "http://8.8.8.8",
        "http://169.254.1.1",
        "http://0.0.0.0",
        "http://127.0.0.1:0",
        "http://127.0.0.1/path",
        "http://user:secret@127.0.0.1",
        "http://@127.0.0.1",
        "http://127.0.0.1?x=1",
    ],
)
def test_invalid_origin_is_config_error_without_credentials(tmp_path: Path, endpoint: Any) -> None:
    with pytest.raises(ValueError) as caught:
        MinioExecutionInputs(
            endpoint=endpoint,
            bucket="sn-input-test",
            access_key="synthetic",
            secret_key="synthetic",
            spool_directory=tmp_path,
        )
    assert "secret@" not in str(caught.value)


@pytest.mark.parametrize("failure", ["bucket", "directory", "tls"])
def test_bad_bucket_directory_and_unverified_tls_fail_startup(tmp_path: Path, failure: str) -> None:
    tmp_path.chmod(0o755 if failure == "directory" else 0o700)
    context = ssl._create_unverified_context() if failure == "tls" else None
    with pytest.raises(ValueError):
        MinioExecutionInputs(
            endpoint="http://127.0.0.1",
            bucket="INVALID_BUCKET" if failure == "bucket" else "sn-input-test",
            access_key="synthetic",
            secret_key="synthetic",
            spool_directory=tmp_path,
            ssl_context=context,
        )


@pytest.mark.parametrize(
    "url,method", [("http://127.0.0.2:59000/x", "GET"), ("http://127.0.0.1:59000/x", "POST")]
)
def test_fixed_http_blocks_alternate_origin_and_mutations(url: str, method: str) -> None:
    http = objects._FixedObjectHTTP("http://127.0.0.1:59000", None)
    with pytest.raises(ExecutionInputError, match="origin_rejected"):
        http.urlopen(method, url)


def test_transport_explicitly_disables_redirects_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def urlopen(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> HTTPResponse:
        captured.update(kwargs)
        return HTTPResponse(body=b"", status=307)

    monkeypatch.setattr(urllib3.PoolManager, "urlopen", urlopen)
    http = objects._FixedObjectHTTP("http://127.0.0.1:59000", None)
    with pytest.raises(ExecutionInputError, match="execution_input_unavailable"):
        http.urlopen("GET", "http://127.0.0.1:59000/x")
    assert captured["redirect"] is False and captured["pool_timeout"] == 3
    assert http.connection_pool_kw["retries"].total == 0


def test_file_input_keeps_same_command_and_format_and_scope(adapter: Any) -> None:
    value, _, _ = adapter
    command = command_example()
    command["audio"]["storage"] = {
        field: getattr(reference(), field) for field in reference().__dataclass_fields__
    }
    request = original_request(command)
    with FileExecutionInputs(value).open_asr(
        request, limits=LIMITS, cancelled=lambda: False
    ) as prepared:
        assert prepared.request is request
        assert prepared.source_format.media_type == "audio/wav"
        assert prepared.object_input.stream.read() == BODY
    assert prepared.object_input.stream.closed


def test_workspace_mismatch_prevents_network(adapter: Any) -> None:
    value, client, _ = adapter
    command = command_example()
    command["audio"]["storage"]["workspace_id"] = "another-workspace"
    with (
        pytest.raises(ExecutionInputError, match="scope_mismatch"),
        FileExecutionInputs(value).open_asr(
            original_request(command), limits=LIMITS, cancelled=lambda: False
        ),
    ):
        pytest.fail("wrong workspace")
    client.stat_object.assert_not_called()
