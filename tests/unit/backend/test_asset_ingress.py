"""Bounded ASGI allocation and disconnect ownership before durable writes."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import Mock

import pytest
from starlette.requests import ClientDisconnect
from starlette.responses import Response
from starlette.types import Message, Scope

from speechnexus_backend.speech.interfaces.http.asset_ingress import AssetIngress
from speechnexus_backend.speech.interfaces.http.assets import _AssetContentResponse


def scope(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    root: str = "",
    path: str = "/v1/nexus/assets",
) -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "root_path": root,
            "headers": [(b"host", b"test"), *(headers or [])],
            "state": {"request_id": "trace-ingress"},
            "asgi": {"spec_version": "2.4"},
        },
    )


def ingress(app: Any, **kwargs: Any) -> AssetIngress:
    return AssetIngress(
        app,
        max_bytes=8,
        concurrent_uploads=1,
        receive_timeout_seconds=0.1,
        allowed_hosts=("test",),
        **kwargs,
    )


async def execute(
    middleware: AssetIngress, messages: list[Message], request_scope: Scope | None = None
) -> list[Message]:
    inputs, sent = iter(messages), []

    async def receive() -> Message:
        return next(inputs)

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(request_scope or scope(), receive, send)
    return sent


def error(sent: list[Message]) -> tuple[int, str]:
    return sent[0]["status"], json.loads(sent[1]["body"])["error"]["code"]


@pytest.mark.parametrize(
    "headers,status",
    [
        ([(b"content-length", b"-1")], 400),
        ([(b"content-length", b"1"), (b"content-length", b"2")], 400),
        ([(b"content-length", b"9" * 21)], 400),
        ([(b"content-length", b"16393")], 413),
    ],
)
async def test_invalid_declared_lengths_allocate_no_parser_or_durable_state(
    headers: Any, status: int
) -> None:
    app = Mock()
    sent = await execute(ingress(app), [], scope(headers=headers))
    assert error(sent)[0] == status
    app.assert_not_called()


async def test_chunked_limit_and_disconnect_close_every_private_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile

    created: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def tracked(*args: Any, **kwargs: Any) -> Any:
        temporary = original(*args, **kwargs)
        created.append(temporary)
        return temporary

    monkeypatch.setattr(
        "speechnexus_backend.speech.interfaces.http.asset_ingress.SpooledTemporaryFile", tracked
    )
    app = Mock()
    middleware = ingress(app)
    sent = await execute(
        middleware,
        [
            {"type": "http.request", "body": b"x" * 16000, "more_body": True},
            {"type": "http.request", "body": b"x" * 400, "more_body": False},
        ],
    )
    assert error(sent) == (413, "asset_too_large")
    assert all(item.closed for item in created) and len(created) == 1
    disconnected = await execute(
        middleware,
        [
            {"type": "http.request", "body": b"x", "more_body": True},
            {"type": "http.disconnect"},
        ],
    )
    assert disconnected == [] and all(item.closed for item in created)
    app.assert_not_called()


async def test_declared_length_mismatch_is_not_treated_as_complete_upload() -> None:
    app = Mock()
    sent = await execute(
        ingress(app),
        [{"type": "http.request", "body": b"xx", "more_body": False}],
        scope(headers=[(b"content-length", b"1")]),
    )
    assert error(sent) == (400, "invalid_asset_request")
    app.assert_not_called()


async def test_receive_deadline_cleans_up_and_releases_capacity() -> None:
    app = Mock()
    middleware = ingress(app)
    sent: list[Message] = []

    async def never() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope(), never, send)
    assert error(sent) == (408, "asset_receive_timeout")
    assert not middleware._slots.locked()
    app.assert_not_called()


async def test_parallel_receivers_have_no_unbounded_wait_queue() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def app(request_scope: Scope, receive: Any, send: Any) -> None:
        nonlocal calls
        calls += 1
        assert (await receive())["body"] == b"x"
        await Response(status_code=204)(request_scope, receive, send)

    middleware = ingress(app)

    async def waiting() -> Message:
        entered.set()
        await release.wait()
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(message: Message) -> None:
        pass

    first = asyncio.create_task(middleware(scope(), waiting, send))
    await entered.wait()
    try:
        rejected = await execute(middleware, [])
        assert error(rejected) == (429, "asset_ingress_full")
    finally:
        release.set()
        await first
    third = await execute(middleware, [{"type": "http.request", "body": b"x", "more_body": False}])
    assert third[0]["status"] == 204 and calls == 2


@pytest.mark.parametrize("path", ["/v1/nexus/assets", "/prefix/v1/nexus/assets"])
async def test_mount_prefix_cannot_bypass_ingress_bound(path: str) -> None:
    app = Mock()
    sent = await execute(
        ingress(app), [], scope(root="/prefix", path=path, headers=[(b"content-length", b"16393")])
    )
    assert error(sent)[0] == 413
    app.assert_not_called()


@pytest.mark.parametrize("fail_on_start", [True, False])
async def test_prefetched_content_always_closes_on_failed_send(fail_on_start: bool) -> None:
    close = Mock()
    response = _AssetContentResponse(iter([b"synthetic"]), close)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if fail_on_start or message["type"] == "http.response.body":
            raise OSError("synthetic disconnect")

    with pytest.raises(ClientDisconnect):
        await response(scope(), receive, send)
    close.assert_called_once()
