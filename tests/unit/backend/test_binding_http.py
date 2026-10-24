"""HTTP boundary refusals and draft projection; PG persistence is qualified separately."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from speechnexus_contracts.binding_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft, BindingDraftResult
from speechnexus_backend.speech.interfaces.http.binding_errors import CREATE, READ
from speechnexus_backend.speech.interfaces.http.binding_ingress import CREATE_PATH, BindingIngress
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

PATH = CREATE_PATH
GET = PATH + "/synthetic-binding/revisions/1"


def provider() -> Mock:
    port = Mock()

    def create(command: Any) -> BindingDraftResult:
        return BindingDraftResult(
            "created",
            BindingDraft(
                command.binding_id, "1", "binding-original-id", command.definition, 1_000_000
            ),
        )

    port.create.side_effect = create
    port.get.return_value = None
    return port


def client(port: Any = None, **settings: Any) -> AsyncClient:
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",), **settings}),
        bindings=port,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def error(response: Any, status: int, operation: str = CREATE) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    validate_response(operation, status, {"body": body, "headers": dict(response.headers)})
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    return cast(dict[str, Any], body["error"])


async def test_draft_create_and_pinned_read_validate_source_and_hide_internal_command() -> None:
    port = provider()
    body = binding_request()
    async with client(port) as http:
        created = await http.post(PATH, json=body, headers={"Idempotency-Key": "original-key"})
        assert created.status_code == 201, created.text
        validate_response(CREATE, 201, {"body": created.json()})
        assert created.json()["publication"]["status"] == "draft"
        assert "validation" not in created.json()
        command = port.create.call_args.args[0]
        port.get.return_value = BindingDraft(
            command.binding_id, "1", "binding-original-id", command.definition, 1_000_000
        )
        read = await http.get(PATH + f"/{command.binding_id}/revisions/1")
        assert read.status_code == 200 and read.json() == created.json()
        validate_response(READ, 200, {"body": read.json()})
        missing = await http.get(PATH + f"/{command.binding_id}/revisions/99")
        assert error(missing, 503, READ)["code"] == "binding_unavailable"
    assert "key_digest" not in created.text and "intent_json" not in created.text


async def test_unconfigured_and_unreadable_results_are_not_confirmed_absence() -> None:
    async with client() as http:
        assert error(await http.post(PATH, json=binding_request()), 503)["retryable"] is False
        assert error(await http.get(GET), 503, READ)["retryable"] is True
    port = provider()
    async with client(port) as http:
        assert error(await http.get(GET), 404, READ)["code"] == "binding_not_found"
        port.get.side_effect = RuntimeError("private-dsn-and-intent")
        result = await http.get(GET)
        assert error(result, 503, READ)["retryable"] is True
        assert "private-dsn-and-intent" not in result.text


@pytest.mark.parametrize(
    "raw", [b"{", b"{}", b'{"a":1,"a":2}', b'{"a":NaN}', b"\xff", b"[" * 2000 + b"]" * 2000]
)
async def test_invalid_json_or_schema_never_calls_writer(raw: bytes) -> None:
    port = provider()
    async with client(port) as http:
        error(await http.post(PATH, content=raw, headers={"Content-Type": "application/json"}), 400)
    port.create.assert_not_called()


async def test_complete_definition_semantics_and_ambiguous_headers_are_rejected() -> None:
    port = provider()
    body = binding_request()
    body["tasks"], body["engine"]["kind"] = ["voice_design"], "vllm"
    async with client(port) as http:
        assert error(await http.post(PATH, json=body), 422)["code"] == "invalid_binding_definition"
        for headers in (
            [("Idempotency-Key", "original-key"), ("Idempotency-Key", "other-key")],
            [("X-Request-ID", "first"), ("X-Request-ID", "second")],
            [("Content-Length", "1"), ("Content-Length", "1")],
        ):
            error(await http.post(PATH, json=binding_request(), headers=headers), 400)
        error(await http.post(PATH + "?x=1&x=2", json=binding_request()), 400)
    port.create.assert_not_called()


@pytest.mark.parametrize(
    "method,path,status,headers",
    [
        ("POST", PATH, 403, {"Host": "foreign.test"}),
        ("GET", GET, 403, {"Origin": "null"}),
        ("GET", PATH, 405, {}),
        ("POST", GET, 405, {}),
        ("POST", PATH + "/", 400, {}),
        ("POST", PATH + "/publish", 400, {}),
        ("POST", PATH, 415, {"Content-Type": "text/plain"}),
        ("POST", PATH, 413, {"Content-Length": "1048577"}),
        ("GET", GET, 400, {"Content-Length": "1"}),
        ("GET", PATH + "/synthetic-binding/revisions/current", 400, {}),
    ],
)
async def test_ingress_refusals_are_source_declared_without_writes(
    method: str, path: str, status: int, headers: dict[str, str]
) -> None:
    port = provider()
    async with client(port) as http:
        response = await http.request(method, path, headers=headers)
        operation = READ if "/revisions/" in path else CREATE
        error(response, status, operation)
        if status == 405:
            assert response.headers["Allow"] == ("GET" if operation == READ else "POST")
    port.create.assert_not_called()
    port.get.assert_not_called()


@pytest.mark.parametrize(
    "outcome,code,status",
    [
        ("conflict", "idempotency_conflict", 409),
        ("unknown", None, 503),
    ],
)
async def test_original_result_has_no_automatic_retry_or_fabricated_operation(
    outcome: Any, code: Any, status: int
) -> None:
    port = provider()
    port.create.side_effect = None
    port.create.return_value = BindingDraftResult(outcome, error_code=code)
    async with client(port) as http:
        response = await http.post(PATH, json=binding_request())
        body = error(response, status)
        assert body["retryable"] is False and "nexus" not in body
        assert "location" not in response.headers and "retry-after" not in response.headers
    assert port.create.call_count == 1


async def test_invalid_provider_projection_does_not_fabricate_success() -> None:
    port = provider()
    port.create.side_effect = RuntimeError("private-secret")
    async with client(port) as http:
        response = await http.post(PATH, json=binding_request())
        assert error(response, 503)["code"] == "binding_result_unknown"
        assert "private-secret" not in response.text


async def test_only_source_binding_paths_are_exposed_with_namespaced_schemas() -> None:
    async with client(provider()) as http:
        document = (await http.get("/openapi.json")).json()
    paths = {p: v for p, v in document["paths"].items() if p.startswith(PATH)}
    assert set(paths) == {PATH, PATH + "/{binding_id}/revisions/{revision}"}
    assert set(paths[PATH]) == {"post"}
    assert set(paths[PATH + "/{binding_id}/revisions/{revision}"]) == {"get"}
    assert "BindingDraft_ModelBindingDraftRevision" in document["components"]["schemas"]
    from speechnexus_backend.speech.interfaces.http.binding_contract import binding_http_contract

    assert (
        document["components"]["schemas"]["BindingDraft_ModelBindingDraftRevision"]
        == (
            binding_http_contract()["components"]["schemas"][
                "BindingDraft_ModelBindingDraftRevision"
            ]
        )
    )


def scope() -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": PATH,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
        "state": {"request_id": "test"},
    }


def ingress(app: Any, *, timeout: float = 0.03, limit: int = 262144) -> BindingIngress:
    return BindingIngress(
        app,
        max_bytes=limit,
        concurrency=1,
        receive_timeout_seconds=timeout,
        is_enabled=lambda: True,
        allowed_hosts=("test",),
        allowed_origins=(),
    )


async def test_receive_timeout_capacity_frame_limit_and_disconnect_do_not_write() -> None:
    called = Mock()
    messages: list[Any] = []

    async def app(*_: Any) -> None:
        called()

    async def send(message: Any) -> None:
        messages.append(message)

    async def stalled() -> Any:
        await asyncio.Event().wait()

    boundary = ingress(app)
    first = asyncio.create_task(boundary(scope(), stalled, send))
    await asyncio.sleep(0)
    await boundary(scope(), stalled, send)
    await first
    assert [m["status"] for m in messages if m["type"] == "http.response.start"] == [429, 408]
    for incoming, expected in (
        ({"type": "http.disconnect"}, None),
        ({"type": "http.request", "more_body": True}, 400),
        ({"type": "http.request", "body": b"1234"}, 413),
    ):
        messages.clear()

        async def receive(message: Any = incoming) -> Any:
            return message

        await ingress(app, timeout=1, limit=3)(scope(), receive, send)
        assert [m["status"] for m in messages if m["type"] == "http.response.start"] == (
            [] if expected is None else [expected]
        )
    called.assert_not_called()


async def test_complete_cancelled_request_holds_receive_permit_until_handler_quiesces() -> None:
    started, finish = asyncio.Event(), asyncio.Event()
    messages: list[Any] = []

    async def app(_: Any, receive: Any, send: Any) -> None:
        assert (await receive())["body"] == b"{}"
        started.set()
        await finish.wait()

    async def receive() -> Any:
        return {"type": "http.request", "body": b"{}"}

    async def send(message: Any) -> None:
        messages.append(message)

    boundary = ingress(app)
    task = asyncio.create_task(boundary(scope(), receive, send))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    await boundary(scope(), receive, send)
    assert messages[0]["status"] == 429
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not boundary._slots.locked()


def test_opt_in_writer_settings_are_secret_and_bounded() -> None:
    from speechnexus_backend.settings import Settings

    base = _settings().model_dump()
    assert (
        Settings(**{**base, "binding_writer_database_url": " "}).binding_writer_database_url is None
    )
    configured = Settings(**{**base, "binding_writer_database_url": "postgresql://private-secret"})
    assert "private-secret" not in repr(configured)
    for name, value in (
        ("binding_receive_max_bytes", 1048577),
        ("binding_receive_concurrency", 5),
        ("binding_receive_timeout_seconds", 0),
        ("binding_transaction_timeout_seconds", 31),
    ):
        with pytest.raises(ValidationError):
            Settings(**{**base, name: value})


def test_writer_role_and_private_diagnostics_are_explicit() -> None:
    import logging

    from pydantic import SecretStr

    from speechnexus_backend.binding_runtime import BindingRuntime, _PoolDiagnostics

    with pytest.raises(ValueError, match="explicit API binding writer identity"):
        BindingRuntime(_settings())
    with pytest.raises(ValueError, match="explicit API binding writer identity"):
        BindingRuntime(
            _settings().model_copy(
                update={
                    "process_role": "scheduler",
                    "binding_writer_database_url": SecretStr("synthetic"),
                }
            )
        )
    record = logging.LogRecord(
        "psycopg.pool",
        logging.WARNING,
        "",
        0,
        "pool %s: password=synthetic_private",
        ("owned-pool",),
        None,
    )
    _PoolDiagnostics("owned-pool").filter(record)
    assert record.getMessage() == "Binding database connection unavailable"
    unrelated = logging.LogRecord(
        "psycopg.pool", logging.WARNING, "", 0, "unrelated pool", (), None
    )
    _PoolDiagnostics("owned-pool").filter(unrelated)
    assert unrelated.getMessage() == "unrelated pool"


async def test_source_large_integer_round_trips_through_real_json_http_without_float_loss() -> None:
    import json
    from decimal import Decimal

    from speechnexus_backend.speech.application.binding_documents import encode_json

    body = binding_request()
    body["artifact_set"][0]["size_bytes"] = 10**5000
    port = provider()
    async with client(port) as http:
        response = await http.post(
            PATH,
            content=encode_json(body, max_bytes=262144),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 201
        wire = json.loads(response.content, parse_int=Decimal)
        assert wire["artifact_set"][0]["size_bytes"] == body["artifact_set"][0]["size_bytes"]
        assert len(str(wire["artifact_set"][0]["size_bytes"])) == 5001
