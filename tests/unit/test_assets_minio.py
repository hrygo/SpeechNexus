"""Storage boundary checks: pinned identity, integrity, errors, resource release."""

from __future__ import annotations

import io
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from unittest.mock import Mock

import pytest
from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager

from speechnexus_backend.speech.domain.assets import AssetError, hash_bytes
from speechnexus_backend.speech.infrastructure.assets_minio import (
    MinioAssetObjects,
    _FixedOriginHTTP,
)
from tests.unit.test_assets import PROOF, WRITING

BODY = b"synthetic text"
ASSET = replace(WRITING, bytes=len(BODY), sha256=hash_bytes(BODY))
BINDING = replace(PROOF, bytes=len(BODY), sha256=ASSET.sha256)


def missing() -> S3Error:
    return S3Error(Mock(), "NoSuchKey", "synthetic missing", "", "", "")


def storage(monkeypatch: pytest.MonkeyPatch) -> tuple[MinioAssetObjects, Mock]:
    client = Mock(spec=Minio)
    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.assets_minio.Minio", Mock(return_value=client)
    )
    return MinioAssetObjects(
        "http://127.0.0.1:59000", "test-bucket", "test-key", "test-secret"
    ), client


def facts(client: Mock, body: bytes = BODY) -> Mock:
    client.stat_object.return_value = Mock(
        size=len(BODY),
        version_id=BINDING.version_id,
        metadata={
            "X-Amz-Meta-Nexus-Asset-Id": ASSET.asset_id,
            "X-Amz-Meta-Nexus-Upload-Digest": ASSET.request_digest,
            "X-Amz-Meta-Nexus-Writer-Id": ASSET.writer_id,
        },
    )
    response = Mock(wraps=io.BytesIO(body))
    response.release_conn = Mock()
    response.headers = {"x-amz-version-id": BINDING.version_id}
    client.get_object.return_value = response
    return response


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:pass@example.test",
        "file:///tmp/assets",
        "http://example.test/path",
        "http://example.test/?key=value",
        "http://example.test/#value",
        "http://",
    ],
)
def test_storage_endpoint_is_explicit_origin_only(endpoint: str) -> None:
    with pytest.raises(ValueError):
        MinioAssetObjects(endpoint, "bucket-test", "key", "secret")


def test_storage_requires_explicit_credentials() -> None:
    with pytest.raises(ValueError):
        MinioAssetObjects("https://example.test", "bucket-test", "", "")


def test_versioning_and_verified_single_original_put(monkeypatch: pytest.MonkeyPatch) -> None:
    objects, client = storage(monkeypatch)
    client.get_bucket_versioning.return_value = Mock(status="Enabled")
    objects.check_writes_available()
    response = facts(client)
    client.put_object.return_value = Mock(version_id=BINDING.version_id)
    assert objects.put_verified(ASSET, io.BytesIO(BODY)) == BINDING
    client.put_object.assert_called_once()
    assert client.put_object.call_args.kwargs["num_parallel_uploads"] == 1
    assert client.get_object.call_args.kwargs["version_id"] == BINDING.version_id
    response.close.assert_called_once()
    response.release_conn.assert_called_once()
    objects.close()


@pytest.mark.parametrize("failure", ["disabled", "connection"])
def test_unversioned_or_unavailable_bucket_cannot_receive_content(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    objects, client = storage(monkeypatch)
    if failure == "disabled":
        client.get_bucket_versioning.return_value = Mock(status=None)
    else:
        client.get_bucket_versioning.side_effect = OSError("private diagnostic")
    with pytest.raises(AssetError, match="asset_storage_") as error:
        objects.check_writes_available()
    assert "private" not in str(error.value)
    client.put_object.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["identity", "size", "body", "long", "version", "null", "network"]
)
def test_verification_never_substitutes_unconfirmed_content(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    objects, client = storage(monkeypatch)
    response = facts(
        client,
        b"wrong content" if failure == "body" else BODY + b"x" if failure == "long" else BODY,
    )
    client.put_object.return_value = Mock(version_id=BINDING.version_id)
    if failure == "identity":
        client.stat_object.return_value.metadata = {}
    elif failure == "size":
        client.stat_object.return_value.size = 100
    elif failure == "version":
        response.headers["x-amz-version-id"] = "different-version"
    elif failure == "null":
        client.put_object.return_value.version_id = "null"
    elif failure == "network":
        client.put_object.side_effect = OSError("private diagnostic")
    with pytest.raises(AssetError) as error:
        objects.put_verified(ASSET, io.BytesIO(BODY))
    assert "private" not in str(error.value)
    assert client.put_object.call_count == 1
    if failure in ("body", "long", "version"):
        response.close.assert_called_once()
        response.release_conn.assert_called_once()


def test_recovery_is_read_only_and_absence_does_not_authorize_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects, client = storage(monkeypatch)
    facts(client)
    assert objects.inspect_original(ASSET) == BINDING
    client.put_object.assert_not_called()
    client.stat_object.side_effect = missing()
    assert objects.inspect_original(ASSET) is None
    client.stat_object.side_effect = OSError("private diagnostic")
    with pytest.raises(AssetError, match="asset_storage_unavailable"):
        objects.inspect_original(ASSET)
    with pytest.raises(AssetError, match="asset_write_not_authorized"):
        objects.put_verified(replace(ASSET, phase="reserved", writer_id=None), io.BytesIO(BODY))


@pytest.mark.parametrize("failure", [None, "status", "range", "length", "version", "connect"])
def test_download_requires_exact_version_range_and_closes(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    objects, client = storage(monkeypatch)
    response = Mock(
        status=206,
        headers={
            "x-amz-version-id": BINDING.version_id,
            "content-range": f"bytes 1-3/{len(BODY)}",
            "content-length": "3",
        },
    )
    client.get_object.return_value = response
    if failure == "connect":
        client.get_object.side_effect = OSError("private diagnostic")
    elif failure == "status":
        response.status = 200
    elif failure:
        response.headers[
            {"range": "content-range", "length": "content-length", "version": "x-amz-version-id"}[
                failure
            ]
        ] = "wrong"
    if failure:
        with pytest.raises(AssetError), objects.open(BINDING, 1, 3):
            pytest.fail("unverified response must not be yielded")
    else:
        with objects.open(BINDING, 1, 3) as reader:
            response.read.return_value = b"abc"
            assert reader.read(3) == b"abc"
            response.read.side_effect = OSError("private endpoint and key")
            with pytest.raises(AssetError, match="asset_content_incomplete") as caught:
                reader.read(3)
            assert "private" not in str(caught.value)
    if failure != "connect":
        response.close.assert_called_once()
        response.release_conn.assert_called_once()


def test_delete_is_confirmed_for_exact_version_and_unknown_does_not_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects, client = storage(monkeypatch)
    client.stat_object.side_effect = missing()
    objects.delete_version(BINDING)
    assert client.remove_object.call_args.kwargs["version_id"] == BINDING.version_id
    client.stat_object.side_effect = None
    with pytest.raises(AssetError, match="asset_cleanup_unconfirmed"):
        objects.delete_version(BINDING)
    client.remove_object.side_effect = OSError("private diagnostic")
    with pytest.raises(AssetError, match="asset_cleanup_unconfirmed"):
        objects.delete_version(BINDING)


def test_fixed_origin_transport_does_not_follow_redirects_or_retry_writes() -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.path)
            self.send_response(307 if self.path == "/redirect" else 500)
            self.send_header("Location", "/unapproved")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    pool = _FixedOriginHTTP(endpoint)
    try:
        assert pool.request("PUT", endpoint + "/redirect", body=b"synthetic").status == 307
        assert pool.request("PUT", endpoint + "/error", body=b"synthetic").status == 500
        with pytest.raises(AssetError, match="asset_storage_origin_rejected"):
            pool.request("PUT", "http://127.0.0.1:1/other", body=b"synthetic")
        assert requests == ["/redirect", "/error"]
        assert isinstance(pool, PoolManager)
    finally:
        pool.clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
