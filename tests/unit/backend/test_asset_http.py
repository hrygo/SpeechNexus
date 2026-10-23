"""Public Asset wire behavior and SDK recovery; storage is qualified separately."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from openai import APIStatusError, AsyncOpenAI
from speechnexus_contracts import parse

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.assets import AssetError, AssetPolicy, UploadResult
from tests.fake.readiness import StaticProbe
from tests.unit.backend.test_health import _settings
from tests.unit.test_assets import READY, RESERVED, WRITING, setup_service


@pytest.fixture
async def asset_api() -> AsyncIterator[tuple[AsyncClient, AssetService, Mock, Mock]]:
    service, metadata, objects = setup_service()
    objects.open.side_effect = lambda proof, offset, length: io.BytesIO(
        b"text"[offset : offset + length]
    )
    app = create_app(
        settings=_settings().model_copy(
            update={
                "asset_allowed_hosts": ("test",),
                "asset_default_ttl_seconds": 60,
            }
        ),
        readiness=ReadinessService([StaticProbe("synthetic")], timeout_seconds=1),
        assets=service,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, service, metadata, objects


async def test_upload_metadata_and_download_follow_source_contract(asset_api: Any) -> None:
    client, _, metadata, _ = asset_api
    response = await client.post(
        "/v1/nexus/assets",
        files={"file": ("../../private.txt", b"text")},
        data={"purpose": "text_input"},
        headers={"X-Request-ID": "trace-asset-1"},
    )
    assert response.status_code == 201
    parse("Asset", response.json())
    assert response.headers["Location"] == "/v1/nexus/assets/asset-test"
    assert response.headers["X-Request-ID"] == "trace-asset-1"
    assert response.json()["media_type"] == "text/plain; charset=utf-8"
    for private in ("private/assets", "request_digest", "writer_id", "object_key", "private.txt"):
        assert private not in response.text
    assert metadata.reserve.call_args.args[0].workspace_id == "workspace-default"
    received = await client.get(response.headers["Location"])
    assert received.json() == response.json()
    content = await client.get(response.headers["Location"] + "/content")
    assert content.content == b"text" and content.status_code == 200
    assert content.headers["Content-Length"] == "4"
    assert content.headers["Content-Type"] == "application/octet-stream"
    assert content.headers["X-Content-Type-Options"] == "nosniff"
    assert "attachment" in content.headers["Content-Disposition"]


@pytest.mark.parametrize(
    "header,expected",
    [("bytes=1-2", b"ex"), ("bytes=-2", b"xt"), ("bytes=1-", b"ext"), ("bytes=0-999", b"text")],
)
async def test_pinned_single_range(asset_api: Any, header: str, expected: bytes) -> None:
    response = await asset_api[0].get(
        "/v1/nexus/assets/asset-test/content", headers={"Range": header}
    )
    assert response.status_code == 206 and response.content == expected
    assert int(response.headers["Content-Length"]) == len(expected)
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Range"].endswith("/4")


@pytest.mark.parametrize(
    "header", ["bytes=4-", "bytes=3-1", "bytes=-0", "bytes=1-2,3-4", "bytes=-"]
)
async def test_unavailable_range_has_safe_416_and_actual_total(asset_api: Any, header: str) -> None:
    response = await asset_api[0].get(
        "/v1/nexus/assets/asset-test/content", headers={"Range": header}
    )
    assert response.status_code == 416 and response.headers["Content-Range"] == "bytes */4"
    error = parse("ErrorResponse", response.json())["error"]
    assert error["code"] == "range_not_satisfiable" and error["param"] == "Range"
    assert error["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "fields",
    [
        {"purpose": "unknown"},
        {},
        {"purpose": "text_input", "url": "http://unapproved.test/content"},
        {"purpose": "text_input", "object_key": "private/other"},
        {"purpose": "text_input", "ttl_seconds": "1.5"},
        {"purpose": "text_input", "ttl_seconds": "59"},
        {"purpose": "text_input", "ttl_seconds": "31536001"},
        {"purpose": "text_input", "sha256": "SHA256:" + "a" * 64},
        {"purpose": "text_input", "workspace_id": "bad/path"},
    ],
)
async def test_multipart_metadata_rejects_unapproved_or_invalid_values(
    asset_api: Any, fields: dict[str, str]
) -> None:
    client, _, metadata, objects = asset_api
    response = await client.post(
        "/v1/nexus/assets", files={"file": ("sample.txt", b"text")}, data=fields
    )
    assert response.status_code == 400
    parse("ErrorResponse", response.json())
    assert "unapproved.test" not in response.text
    metadata.reserve.assert_not_called()
    objects.put_verified.assert_not_called()


async def test_multipart_rejects_duplicate_fields_files_and_raw_paths(asset_api: Any) -> None:
    client, _, metadata, _ = asset_api
    for files in [
        [("file", ("a.txt", b"a")), ("file", ("b.txt", b"b"))],
        [
            ("file", ("a.txt", b"a")),
            ("purpose", (None, "text_input")),
            ("purpose", (None, "text_input")),
        ],
    ]:
        response = await client.post(
            "/v1/nexus/assets", files=files, data={"purpose": "text_input"}
        )
        assert response.status_code == 400
    response = await client.post(
        "/v1/nexus/assets", data={"file": "/private/path", "purpose": "text_input"}
    )
    assert response.status_code == 400
    metadata.reserve.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        [("Idempotency-Key", "short")],
        [("Idempotency-Key", "original-key"), ("Idempotency-Key", "another-key")],
    ],
)
async def test_idempotency_header_is_validated_before_reservation(
    asset_api: Any, headers: list[tuple[str, str]]
) -> None:
    client, _, metadata, _ = asset_api
    response = await client.post(
        "/v1/nexus/assets",
        files={"file": ("sample.txt", b"text")},
        data={"purpose": "text_input"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    metadata.reserve.assert_not_called()


@pytest.mark.parametrize(
    "outcome,code,status",
    [
        ("conflict", "idempotency_key_conflict", 409),
        ("in_progress", "operation_in_progress", 409),
        ("unknown", "asset_metadata_unknown", 503),
        ("rejected", "asset_digest_mismatch", 422),
    ],
)
async def test_original_identity_is_exposed_without_inventing_an_operation(
    asset_api: Any,
    outcome: Any,
    code: str,
    status: int,
) -> None:
    client, _, metadata, _ = asset_api
    if outcome == "in_progress":
        metadata.begin_write.return_value = UploadResult(outcome, WRITING, code)
    else:
        metadata.reserve.return_value = UploadResult(outcome, WRITING, code)
    response = await client.post(
        "/v1/nexus/assets", files={"file": ("sample.txt", b"text")}, data={"purpose": "text_input"}
    )
    assert response.status_code == status
    assert response.headers["Location"] == "/v1/nexus/assets/asset-test"
    error = parse("ErrorResponse", response.json())["error"]
    assert "nexus" not in error and error["retryable"] is False
    assert response.headers["X-Should-Retry"] == "false"


async def test_expired_replay_is_unavailable_and_unconfigured_routes_are_truthful(
    asset_api: Any,
) -> None:
    client, _, metadata, _ = asset_api
    metadata.reserve.return_value = UploadResult("replayed", READY)
    metadata.get.return_value = (READY, READY.expires_at_us)
    response = await client.post(
        "/v1/nexus/assets", files={"file": ("sample.txt", b"text")}, data={"purpose": "text_input"}
    )
    assert response.status_code == 410
    app = create_app(
        settings=_settings(),
        readiness=ReadinessService([StaticProbe("synthetic")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as disabled:
        for path in ("/v1/nexus/assets/asset-test", "/v1/nexus/assets/asset-test/content"):
            unavailable = await disabled.get(path)
            assert unavailable.status_code == 503
            parse("ErrorResponse", unavailable.json())


async def test_private_errors_and_missing_or_unpublished_assets_do_not_leak(asset_api: Any) -> None:
    client, _, metadata, objects = asset_api
    for asset, now, code in [
        (None, 3, 404),
        (RESERVED, 3, 410),
        (replace(READY, status="deleted"), 3, 410),
    ]:
        metadata.get.return_value = (asset, now)
        response = await client.get("/v1/nexus/assets/asset-test/content")
        assert response.status_code == code
    metadata.get.return_value = (READY, 3)
    objects.open.side_effect = AssetError("private_endpoint_and_secret")
    response = await client.get("/v1/nexus/assets/asset-test/content")
    assert response.status_code == 503 and "private_endpoint" not in response.text
    missing = await client.get("/v1/nexus/assets/ab")
    assert missing.status_code == 404


async def test_receive_and_file_size_limits_are_both_enforced(asset_api: Any) -> None:
    client, service, metadata, _ = asset_api
    service.policy = AssetPolicy(60, 2)
    too_large = await client.post(
        "/v1/nexus/assets", files={"file": ("sample.txt", b"text")}, data={"purpose": "text_input"}
    )
    assert too_large.status_code == 413
    metadata.reserve.assert_not_called()
    body_large = await client.post("/v1/nexus/assets", content=b"x" * (4 * 1024 * 1024 + 16385))
    assert body_large.status_code == 413


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Host": "unapproved.test"}, "asset_host_rejected"),
        ({"Host": "user@test"}, "asset_host_rejected"),
        ({"Origin": "http://unapproved.test"}, "asset_origin_rejected"),
        ({"Origin": "null"}, "asset_origin_rejected"),
    ],
)
async def test_host_and_origin_are_explicit_deployment_boundaries(
    asset_api: Any, headers: dict[str, str], code: str
) -> None:
    response = await asset_api[0].get("/v1/nexus/assets/asset-test", headers=headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == code
    assert "unapproved.test" not in response.text


async def test_openai_sdk_does_not_retry_an_uncertain_upload(asset_api: Any) -> None:
    client, _, _, objects = asset_api
    objects.put_verified.side_effect = AssetError("synthetic lost PUT reply")
    sdk = AsyncOpenAI(api_key="synthetic", base_url="http://test/v1", http_client=client)
    with pytest.raises(APIStatusError) as caught:
        await sdk.post(
            "/nexus/assets",
            cast_to=object,
            options={"headers": {"Content-Type": "multipart/form-data"}},
            body={"purpose": "text_input"},
            files={"file": ("sample.txt", b"text")},
        )
    assert caught.value.status_code == 503
    assert objects.put_verified.call_count == 1


async def test_unmatched_asset_paths_and_methods_keep_the_public_envelope(asset_api: Any) -> None:
    client, _, metadata, _ = asset_api
    for method, path, status in [
        ("GET", "/v1/nexus/assets/asset-test/unknown", 404),
        ("DELETE", "/v1/nexus/assets/asset-test", 405),
    ]:
        response = await client.request(method, path)
        assert response.status_code == status
        parse("ErrorResponse", response.json())
    metadata.get.assert_not_called()
