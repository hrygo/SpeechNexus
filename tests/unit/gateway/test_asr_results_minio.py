"""Private upload/readback failures and coroutine cancellation never publish a receipt."""

import asyncio
import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import urllib3
from urllib3 import HTTPResponse

from speechnexus_gateway.domain.asr import AsrCallError, AsrFailure
from speechnexus_gateway.domain.asr_results import (
    AsrResultError,
    PreparedAsrResult,
)
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputError,
    VerifiedExecutionObject,
)
from speechnexus_gateway.infrastructure.asr_results_minio import MinioAsrResults
from speechnexus_gateway.infrastructure.execution_objects_minio import _FixedObjectHTTP

RESULT = PreparedAsrResult("workspace-one", "sha256:" + "a" * 64, b'{"text":"synthetic"}')


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    tmp_path.chmod(0o700)
    value = MinioAsrResults(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-output-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        spool_directory=tmp_path,
        timeout_seconds=10,
    )
    writer = Mock()
    writer.put_object.return_value = Mock(version_id="v1")
    reader = Mock()

    @contextmanager
    def readback(*args: Any, **kwargs: Any) -> Iterator[VerifiedExecutionObject]:
        assert not kwargs["cancelled"]()
        with io.BytesIO(RESULT.body) as stream:
            yield VerifiedExecutionObject(args[0], stream, "v1")

    reader.open_verified.side_effect = readback
    monkeypatch.setattr(value, "_client", writer)
    original_reader = value._reader
    monkeypatch.setattr(value, "_reader", reader)
    try:
        yield value, writer, reader
    finally:
        value.close()
        original_reader.close()


async def test_exactly_one_private_put_then_complete_versioned_readback(adapter: Any) -> None:
    value, writer, reader = adapter
    receipt = await value.stage(RESULT, cancelled=lambda: False)
    assert receipt.reference == RESULT.reference("sn-output-test")
    assert receipt.storage_version == "v1"
    assert receipt.request_digest == RESULT.request_digest
    args, kwargs = writer.put_object.call_args
    assert args[:2] == ("sn-output-test", receipt.reference.object_key)
    assert args[2].closed and args[3] == len(RESULT.body)
    assert kwargs == {"content_type": "application/json", "num_parallel_uploads": 1}
    assert writer.put_object.call_count == reader.open_verified.call_count == 1
    writer.remove_object.assert_not_called()


@pytest.mark.parametrize("version", [None, "", "null"])
async def test_unversioned_write_does_not_read_or_return_a_receipt(
    adapter: Any, version: Any
) -> None:
    value, writer, reader = adapter
    writer.put_object.return_value = Mock(version_id=version)
    with pytest.raises(AsrResultError, match="asr_result_versioning_required"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert writer.put_object.call_count == 1 and not reader.open_verified.called
    writer.remove_object.assert_not_called()


async def test_lost_put_ack_never_retries_deletes_or_exposes_vendor_error(adapter: Any) -> None:
    value, writer, reader = adapter
    writer.put_object.side_effect = TimeoutError("private-credential-and-content")
    with pytest.raises(AsrResultError) as caught:
        await value.stage(RESULT, cancelled=lambda: False)
    assert str(caught.value) == "asr_result_storage_unknown"
    assert writer.put_object.call_count == 1 and not reader.open_verified.called
    assert caught.value.__suppress_context__
    writer.remove_object.assert_not_called()


async def test_readback_failure_never_returns_a_receipt_or_removes_unknown_object(
    adapter: Any,
) -> None:
    value, writer, reader = adapter
    reader.open_verified.side_effect = ExecutionInputError("execution_input_identity_mismatch")
    with pytest.raises(AsrResultError, match="asr_result_readback_failed"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert writer.put_object.call_count == 1
    writer.remove_object.assert_not_called()


async def test_overwrite_between_put_and_readback_is_not_adopted(adapter: Any) -> None:
    value, writer, reader = adapter

    @contextmanager
    def overwritten(*args: Any, **kwargs: Any) -> Iterator[VerifiedExecutionObject]:
        with io.BytesIO(RESULT.body) as stream:
            yield VerifiedExecutionObject(args[0], stream, "v2")

    reader.open_verified.side_effect = overwritten
    with pytest.raises(AsrResultError, match="asr_result_identity_mismatch"):
        await value.stage(RESULT, cancelled=lambda: False)
    writer.remove_object.assert_not_called()


async def test_cancellation_before_write_never_calls_storage(adapter: Any) -> None:
    value, writer, reader = adapter
    with pytest.raises(AsrResultError, match="asr_result_cancelled"):
        await value.stage(RESULT, cancelled=lambda: True)
    assert not writer.put_object.called and not reader.open_verified.called


async def test_original_deadline_guard_is_not_replaced_with_a_storage_error(adapter: Any) -> None:
    value, writer, _ = adapter
    calls = 0

    def check() -> bool:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AsrCallError(AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=True)
        return False

    with pytest.raises(AsrCallError, match=AsrFailure.DEADLINE_EXCEEDED):
        await value.stage(RESULT, cancelled=check)
    assert writer.put_object.call_count == 1


async def test_local_storage_deadline_after_put_does_not_start_readback(
    adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from speechnexus_gateway.infrastructure import asr_results_minio

    value, writer, reader = adapter
    clock = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr(asr_results_minio, "monotonic", lambda: next(clock))
    with pytest.raises(AsrResultError, match="asr_result_deadline_exceeded"):
        await value.stage(RESULT, cancelled=lambda: False)
    assert writer.put_object.call_count == 1 and not reader.open_verified.called


async def test_task_cancellation_waits_for_private_writer_cleanup(adapter: Any) -> None:
    value, writer, reader = adapter
    entered, release = threading.Event(), threading.Event()

    def put(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(3)
        return Mock(version_id="v1")

    writer.put_object.side_effect = put
    task = asyncio.create_task(value.stage(RESULT, cancelled=lambda: False))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not reader.open_verified.called
    assert writer.put_object.call_count == 1


def test_put_transport_has_no_redirect_body_read_or_extra_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = HTTPResponse(body=io.BytesIO(b"private-content"), status=200, preload_content=False)
    parent = Mock(return_value=response)
    monkeypatch.setattr(urllib3.PoolManager, "urlopen", parent)
    http = _FixedObjectHTTP("http://127.0.0.1:59000", None, allowed_methods=frozenset({"PUT"}))
    http.urlopen("PUT", "http://127.0.0.1:59000/sn-output-test/private/result")
    assert response.closed
    kwargs = parent.call_args.kwargs
    assert not kwargs["redirect"] and not kwargs["preload_content"]
    assert not kwargs["decode_content"]
    for method, url in [
        ("PUT", "http://127.0.0.1:59001/sn-output-test/private/result"),
        ("DELETE", "http://127.0.0.1:59000/sn-output-test/private/result"),
        ("POST", "http://127.0.0.1:59000/sn-output-test/private/result?uploads"),
        ("GET", "http://127.0.0.1:59000/sn-output-test/private/result"),
    ]:
        with pytest.raises(ExecutionInputError):
            http.urlopen(method, url)
    assert parent.call_count == 1
    http.clear()
