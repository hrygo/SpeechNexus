"""Fixed private S3 reads, version pinning and verified process-private spooling."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import ssl
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from time import monotonic
from typing import Any, BinaryIO, cast
from urllib.parse import urlsplit

import urllib3
from minio import Minio
from minio.helpers import check_bucket_name
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry, Timeout

from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputError,
    ExecutionInputLimits,
    ExecutionObjectReference,
    VerifiedExecutionObject,
)

_CHUNK_BYTES = 64 * 1024


def _origin(url: str) -> tuple[str, str | None, int]:
    parsed = urlsplit(url)
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port


class _FixedObjectHTTP(urllib3.PoolManager):
    def __init__(
        self,
        endpoint: str,
        context: ssl.SSLContext | None,
        *,
        allowed_methods: frozenset[str] = frozenset({"HEAD", "GET"}),
    ) -> None:
        self._origin = _origin(endpoint)
        self._allowed_methods = allowed_methods
        super().__init__(
            num_pools=1,
            maxsize=4,
            block=True,
            timeout=Timeout(connect=3, read=3),
            retries=Retry(total=0, connect=0, read=0, redirect=0, status=0),
            ssl_context=context,
        )

    def urlopen(self, method: str, url: str, *args: Any, **kwargs: Any) -> BaseHTTPResponse:
        if _origin(url) != self._origin or method not in self._allowed_methods:
            raise ExecutionInputError("execution_input_origin_rejected")
        kwargs["pool_timeout"] = 3
        kwargs["redirect"] = False
        # The SDK preloads HEAD by default and unconditionally reads error bodies,
        # even for get_object(preload_content=False). Intercept before either path.
        kwargs["preload_content"] = False
        kwargs["decode_content"] = False
        response = super().urlopen(method, url, *args, **kwargs)
        rejected = response.status != 200
        if rejected or method in {"HEAD", "PUT"}:
            # HEAD consumers use headers only; they must not retain a connection
            # after preloading is disabled. Never drain/decode an error response.
            try:
                response.close()
            except Exception:
                rejected = True
            try:
                response.release_conn()
            except Exception:
                rejected = True
            if rejected:
                raise ExecutionInputError("execution_input_unavailable") from None
        return response


def _endpoint(endpoint: str, context: ssl.SSLContext | None) -> str:
    try:
        if not isinstance(endpoint, str):
            raise ValueError
        value = urlsplit(endpoint)
        address = ipaddress.ip_address(value.hostname or "")
        port = value.port if value.port is not None else (443 if value.scheme == "https" else 80)
        if (
            value.scheme not in {"http", "https"}
            or value.username is not None
            or value.password is not None
            or value.query
            or value.fragment
            or value.path not in {"", "/"}
            or "%" in str(value.hostname)
            or not (
                address.is_loopback
                or (
                    address.is_private
                    and not address.is_link_local
                    and not address.is_unspecified
                    and not address.is_multicast
                    and not address.is_reserved
                )
            )
            or not 1 <= port <= 65535
        ):
            raise ValueError
        if context is not None and (
            context.protocol != ssl.PROTOCOL_TLS_CLIENT
            or context.verify_mode != ssl.CERT_REQUIRED
            or not context.check_hostname
            or context.minimum_version < ssl.TLSVersion.TLSv1_2
        ):
            raise ValueError
    except TypeError, ValueError:
        raise ValueError(
            "configure a fixed private object-storage origin with verified TLS"
        ) from None
    return value.netloc


class MinioExecutionInputs:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        spool_directory: Path,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        netloc = _endpoint(endpoint, ssl_context)
        try:
            check_bucket_name(bucket, strict=True)
            info = spool_directory.stat()
            if (
                not spool_directory.is_absolute()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or not access_key
                or not secret_key
            ):
                raise ValueError
            self._http = _FixedObjectHTTP(endpoint, ssl_context)
            self._client = Minio(
                netloc,
                access_key=access_key,
                secret_key=secret_key,
                secure=endpoint.startswith("https://"),
                region="us-east-1",
                http_client=self._http,
            )
        except Exception:
            raise ValueError(
                "configure an explicit private object input and spool directory"
            ) from None
        self._bucket = bucket
        self._directory = spool_directory

    def close(self) -> None:
        self._http.clear()

    @contextmanager
    def open_verified(
        self,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> Iterator[VerifiedExecutionObject]:
        if reference.storage_bucket != self._bucket:
            raise ExecutionInputError("execution_input_scope_mismatch")
        # Keys are opaque S3 names, never filesystem paths or URLs. Exclude path
        # normalization/control ambiguity before asking the SDK to construct a URL.
        if (
            not reference.object_key.startswith("private/")
            or any(part in {"", ".", ".."} for part in reference.object_key.split("/"))
            or any(char in reference.object_key for char in "\x00\\?#")
            or any(ord(char) < 32 or ord(char) == 127 for char in reference.object_key)
        ):
            raise ExecutionInputError("execution_input_scope_mismatch")
        if reference.byte_length > limits.max_bytes:
            raise ExecutionInputError("execution_input_limit_exceeded")
        deadline = monotonic() + limits.timeout_seconds

        def check() -> None:
            if cancelled():
                raise ExecutionInputError("execution_input_cancelled")
            if monotonic() >= deadline:
                raise ExecutionInputError("execution_input_deadline_exceeded")

        with ExitStack() as stack:
            response = None
            try:
                check()
                original = self._client.stat_object(self._bucket, reference.object_key)
                check()
                version = original.version_id
                if (
                    not isinstance(version, str)
                    or not 1 <= len(version) <= 1024
                    or version == "null"
                    or original.size != reference.byte_length
                ):
                    raise ExecutionInputError("execution_input_identity_mismatch")
                response = self._client.get_object(
                    self._bucket,
                    reference.object_key,
                    version_id=version,
                    request_headers={"Accept-Encoding": "identity"},
                )
                check()
                if (
                    response.status != 200
                    or response.headers.getlist("x-amz-version-id") != [version]
                    or response.headers.getlist("Content-Encoding") not in ([], ["identity"])
                    or response.headers.getlist("Content-Length") != [str(reference.byte_length)]
                ):
                    raise ExecutionInputError("execution_input_identity_mismatch")
                spool = stack.enter_context(
                    tempfile.SpooledTemporaryFile(
                        mode="w+b", max_size=limits.memory_bytes, dir=str(self._directory)
                    )
                )
                digest = hashlib.sha256()
                total = 0
                while True:
                    check()
                    # Return available bytes after one underlying read, rather
                    # than waiting to fill 64 KiB while a slow peer keeps the
                    # socket alive. Recheck cancel/deadline between reads.
                    chunk = response.read1(
                        min(_CHUNK_BYTES, reference.byte_length - total + 1),
                        decode_content=False,
                    )
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > reference.byte_length:
                        raise ExecutionInputError("execution_input_identity_mismatch")
                    digest.update(chunk)
                    spool.write(chunk)
                check()
                if (
                    total != reference.byte_length
                    or "sha256:" + digest.hexdigest() != reference.content_sha256
                ):
                    raise ExecutionInputError("execution_input_identity_mismatch")
                completed = response
                response = None
                try:
                    completed.close()
                finally:
                    completed.release_conn()
                spool.seek(0)
                check()
                verified = VerifiedExecutionObject(reference, cast(BinaryIO, spool), version)
            except ExecutionInputError:
                raise
            except Exception:
                raise ExecutionInputError("execution_input_unavailable") from None
            finally:
                if response is not None:
                    # Preserve the preparation error, including cancel/deadline.
                    with suppress(Exception):
                        response.close()
                    with suppress(Exception):
                        response.release_conn()
            # Remote cleanup is finished. Consumer errors retain their meaning,
            # while the process-private spool always closes on context exit.
            yield verified
