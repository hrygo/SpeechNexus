"""Fixed-origin complete-before-expose async input and private spool lifetime."""

import asyncio
import hashlib
import io
import ssl
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.asr import AsrCallError, AsrFailure
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputError,
    ExecutionInputLimits,
    ExecutionObjectReference,
)
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


class Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes = BODY) -> None:
        self.buffer = io.BytesIO(body)
        self.closed = False
        self.fail_close = False
        self.reads = 0
        self.after_read: Callable[[], None] = lambda: None
        self.after_close: Callable[[], None] = lambda: None
        self.partial_then_wait = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while chunk := self.buffer.read(1 if self.partial_then_wait else 65536):
            self.reads += 1
            self.after_read()
            yield chunk
            if self.partial_then_wait:
                await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True
        self.buffer.close()
        self.after_close()
        if self.fail_close:
            raise OSError("private-path-must-not-leak")


class HeadStream(Stream):
    def __aiter__(self) -> AsyncIterator[bytes]:
        self.reads += 1
        raise AssertionError("HEAD body must not be read")


class Harness:
    def __init__(self, value: MinioExecutionInputs) -> None:
        self.value = value
        self.calls: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []
        self.head = HeadStream(b"synthetic-unread-body")
        self.body = Stream()
        self.head_headers = httpx.Headers(
            {"Content-Length": str(len(BODY)), "x-amz-version-id": "v1"}
        )
        self.get_headers = httpx.Headers(self.head_headers)
        self.head_status = self.get_status = 200
        self.error: Exception | None = None
        self.cancelled = False

    def client(self) -> httpx.AsyncClient:
        async def respond(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if self.error is not None:
                raise self.error
            if request.method == "HEAD":
                return httpx.Response(self.head_status, headers=self.head_headers, stream=self.head)
            assert request.method == "GET" and request.url.params["versionId"] == "v1"
            return httpx.Response(self.get_status, headers=self.get_headers, stream=self.body)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(respond), trust_env=False, follow_redirects=False
        )
        self.clients.append(client)
        return client


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    tmp_path.chmod(0o700)
    value = MinioExecutionInputs(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-input-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        spool_directory=tmp_path,
    )
    harness = Harness(value)
    monkeypatch.setattr(value, "_new_client", harness.client)
    yield harness
    assert all(client.is_closed for client in harness.clients)
    value.close()


async def test_fixed_version_full_digest_eof_and_spool_lifetime(
    adapter: Harness, tmp_path: Path
) -> None:
    async with adapter.value.open_verified(
        reference(), limits=LIMITS, cancelled=lambda: False
    ) as obj:
        stream = obj.stream
        assert stream.read() == BODY and stream.seek(0) == 0
        assert obj.storage_version == "v1"
        assert adapter.head.closed and adapter.body.closed
        assert "private/assets" not in repr(obj) and "v1" not in repr(obj)
        assert all(client.is_closed for client in adapter.clients)
    assert stream.closed
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
    assert [request.method for request in adapter.calls] == ["HEAD", "GET"]
    assert adapter.calls[1].url.params["versionId"] == "v1"
    assert all(request.headers["Accept-Encoding"] == "identity" for request in adapter.calls)
    assert all(
        request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        for request in adapter.calls
    )
    assert adapter.head.reads == 0


async def test_partial_body_can_be_cancelled_before_a_full_chunk_arrives(adapter: Harness) -> None:
    adapter.body.partial_then_wait = True
    adapter.body.after_read = lambda: setattr(adapter, "cancelled", True)
    with pytest.raises(ExecutionInputError, match="execution_input_cancelled"):
        async with adapter.value.open_verified(
            reference(), limits=LIMITS, cancelled=lambda: adapter.cancelled
        ):
            pytest.fail("partial cancelled input reached consumer")
    assert adapter.body.reads == 1 and adapter.body.closed


async def test_downstream_failure_keeps_identity_and_closes_spool(adapter: Harness) -> None:
    failure = RuntimeError("synthetic consumer failure")
    with pytest.raises(RuntimeError) as caught:
        async with adapter.value.open_verified(
            reference(), limits=LIMITS, cancelled=lambda: False
        ) as obj:
            stream = obj.stream
            assert adapter.body.closed
            raise failure
    assert caught.value is failure and stream.closed


@pytest.mark.parametrize(
    "failure",
    [
        "sha",
        "short",
        "long",
        "version",
        "length",
        "encoding",
        "stat-size",
        "stat-null",
        "stat-control",
        "duplicate-version",
        "duplicate-length",
    ],
)
async def test_incomplete_or_wrong_object_never_reaches_consumer(
    adapter: Harness, failure: str
) -> None:
    ref = reference()
    if failure == "sha":
        ref = replace(ref, content_sha256="sha256:" + "0" * 64)
    if failure in {"short", "long"}:
        adapter.body = Stream(BODY[:-1] if failure == "short" else BODY + b"x")
    if failure in {"version", "length", "encoding"}:
        adapter.get_headers[
            {
                "version": "x-amz-version-id",
                "length": "Content-Length",
                "encoding": "Content-Encoding",
            }[failure]
        ] = "invalid"
    if failure == "stat-size":
        adapter.head_headers["Content-Length"] = str(len(BODY) + 1)
    if failure in {"stat-null", "stat-control"}:
        adapter.head_headers["x-amz-version-id"] = "null" if failure == "stat-null" else "v1\x7f"
    if failure in {"duplicate-version", "duplicate-length"}:
        key = "x-amz-version-id" if failure == "duplicate-version" else "Content-Length"
        adapter.head_headers = httpx.Headers(
            [*adapter.head_headers.multi_items(), (key, adapter.head_headers[key])]
        )
    with pytest.raises(ExecutionInputError, match="execution_input_identity_mismatch"):
        async with adapter.value.open_verified(ref, limits=LIMITS, cancelled=lambda: False):
            pytest.fail("unverified object exposed")
    assert adapter.head.closed
    if len(adapter.calls) == 2:
        assert adapter.body.closed


@pytest.mark.parametrize("method", ["HEAD", "GET"])
@pytest.mark.parametrize("status", [204, 206, 302, 307, 403, 500])
async def test_status_is_rejected_before_any_body_read(
    adapter: Harness, method: str, status: int
) -> None:
    if method == "HEAD":
        adapter.head_status = status
    else:
        adapter.get_status = status
    with pytest.raises(ExecutionInputError, match="execution_input_unavailable"):
        async with adapter.value.open_verified(reference(), limits=LIMITS, cancelled=lambda: False):
            pytest.fail("error body exposed")
    assert adapter.head.reads == adapter.body.reads == 0
    assert len(adapter.calls) == (1 if method == "HEAD" else 2)


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
async def test_ambiguous_key_is_rejected_before_network(adapter: Harness, key: str) -> None:
    with pytest.raises(ExecutionInputError):
        async with adapter.value.open_verified(
            replace(reference(), object_key=key), limits=LIMITS, cancelled=lambda: False
        ):
            pytest.fail("bad key exposed")
    assert not adapter.calls and not adapter.clients


@pytest.mark.parametrize("failure", ["scope", "limit", "cancel", "deadline", "transport", "close"])
async def test_scope_budget_cancel_deadline_and_cleanup_are_safe(
    adapter: Harness, failure: str
) -> None:
    ref, limits = reference(), LIMITS
    if failure == "scope":
        ref = replace(ref, storage_bucket="another-bucket")
    if failure == "limit":
        limits = replace(limits, max_bytes=len(BODY) - 1)
    if failure == "deadline":
        limits = replace(limits, timeout_seconds=1e-12)
    if failure == "transport":
        adapter.error = OSError("private-path-must-not-leak")
    if failure == "close":
        adapter.body.fail_close = True
    with pytest.raises(ExecutionInputError) as caught:
        async with adapter.value.open_verified(
            ref, limits=limits, cancelled=lambda: failure == "cancel"
        ):
            pytest.fail("failed input exposed")
    assert "private-path" not in str(caught.value)
    if failure in {"scope", "limit", "cancel", "deadline"}:
        assert not adapter.calls
    if failure == "close":
        assert adapter.body.closed


async def test_cancel_mid_read_survives_remote_cleanup_error(adapter: Harness) -> None:
    adapter.body.fail_close = True
    adapter.body.partial_then_wait = True
    adapter.body.after_read = lambda: setattr(adapter, "cancelled", True)
    with pytest.raises(ExecutionInputError, match="execution_input_cancelled"):
        async with adapter.value.open_verified(
            reference(), limits=LIMITS, cancelled=lambda: adapter.cancelled
        ):
            pytest.fail("cancelled input exposed")
    assert adapter.body.closed


async def test_cancel_during_remote_close_never_exposes_prepared_content(adapter: Harness) -> None:
    adapter.body.after_close = lambda: setattr(adapter, "cancelled", True)
    with pytest.raises(ExecutionInputError, match="execution_input_cancelled"):
        async with adapter.value.open_verified(
            reference(), limits=LIMITS, cancelled=lambda: adapter.cancelled
        ):
            pytest.fail("cancelled input exposed after close")
    assert adapter.body.closed


async def test_execution_deadline_error_keeps_its_domain_identity(adapter: Harness) -> None:
    failure = AsrCallError(AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=False)

    def check() -> bool:
        if adapter.calls:
            raise failure
        return False

    with pytest.raises(AsrCallError) as caught:
        async with adapter.value.open_verified(reference(), limits=LIMITS, cancelled=check):
            pytest.fail("expired execution exposed input")
    assert caught.value is failure and not failure.request_may_have_run


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


async def test_file_input_keeps_same_command_and_format_and_scope(adapter: Harness) -> None:
    command = command_example()
    command["audio"]["storage"] = {
        field: getattr(reference(), field) for field in reference().__dataclass_fields__
    }
    request = original_request(command)
    async with FileExecutionInputs(adapter.value).open_asr(
        request, limits=LIMITS, cancelled=lambda: False
    ) as prepared:
        assert prepared.request is request
        assert prepared.source_format.media_type == "audio/wav"
        assert prepared.object_input.stream.read() == BODY
    assert prepared.object_input.stream.closed


async def test_workspace_mismatch_prevents_network(adapter: Harness) -> None:
    command = command_example()
    command["audio"]["storage"]["workspace_id"] = "another-workspace"
    with pytest.raises(ExecutionInputError, match="scope_mismatch"):
        async with FileExecutionInputs(adapter.value).open_asr(
            original_request(command), limits=LIMITS, cancelled=lambda: False
        ):
            pytest.fail("wrong workspace")
    assert not adapter.calls
