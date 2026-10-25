"""Fixed private S3 origin, HTTP policy and the pinned SDK's SigV4 signer."""

import hashlib
import ipaddress
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from minio.credentials.credentials import Credentials
from minio.helpers import DictType
from minio.signer import sign_v4_s3


def private_endpoint(endpoint: str, context: ssl.SSLContext | None) -> str:
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


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


class FixedS3Transport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        endpoint: str,
        context: ssl.SSLContext | None,
        *,
        methods: frozenset[str],
        scope_error: Callable[[], Exception],
    ) -> None:
        self._origin = _origin(httpx.URL(endpoint))
        self._methods = methods
        self._scope_error = scope_error
        self._transport = httpx.AsyncHTTPTransport(
            verify=context or True,
            trust_env=False,
            retries=0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _origin(request.url) != self._origin or request.method not in self._methods:
            raise self._scope_error()
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def signed_headers(
    method: str,
    url: str,
    *,
    netloc: str,
    credentials: Credentials,
    body: bytes = b"",
    media_type: str | None = None,
) -> dict[str, str]:
    date = datetime.now(UTC)
    digest = hashlib.sha256(body).hexdigest()
    headers: DictType = {
        "Host": netloc,
        "X-Amz-Date": date.strftime("%Y%m%dT%H%M%SZ"),
        "X-Amz-Content-Sha256": digest,
        "Accept-Encoding": "identity",
    }
    if method == "PUT":
        if media_type is None:
            raise ValueError("a private S3 write needs its fixed media type")
        headers.update({"Content-Type": media_type, "Content-Length": str(len(body))})
    sign_v4_s3(
        method=method,
        url=urlsplit(url),
        region="us-east-1",
        headers=headers,
        credentials=credentials,
        content_sha256=digest,
        date=date,
    )
    signed: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(value, str):
            raise ValueError("invalid private S3 header")
        signed[name] = value
    return signed
