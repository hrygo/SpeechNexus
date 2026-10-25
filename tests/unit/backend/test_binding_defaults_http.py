"""Producer HTTP boundaries; database authority is exercised by real PG tests."""

from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from speechnexus_contracts.binding_defaults_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.binding_defaults import (
    BindingDefaultsDraftResult,
    BindingDefaultsSnapshot,
)
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft, BindingDraftResult
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

PATH = "/v1/nexus/admin/model-bindings"
FIXED = PATH + "/asr-quality/revisions/1"
DEFAULTS = FIXED + "/parameter-defaults"
GET = "getModelBindingParameterDefaults"


def provider() -> Mock:
    port = Mock()

    def create(command: Any) -> BindingDefaultsDraftResult:
        draft = BindingDraft(command.binding_id, "1", "mbrev-original", command.definition, 1000000)
        snapshot = (
            BindingDefaultsSnapshot(
                "mbdefaults-original",
                draft.binding_id,
                "1",
                draft.revision_id,
                command.parameter_defaults,
                draft.created_at_us,
            )
            if command.parameter_defaults is not None
            else None
        )
        port.get.return_value = draft
        port.get_defaults.return_value = snapshot
        return BindingDefaultsDraftResult(BindingDraftResult("created", draft), snapshot)

    port.create.side_effect = create
    port.get.return_value = port.get_defaults.return_value = None
    return port


def client(port: Any, **settings: Any) -> AsyncClient:
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",), **settings}),
        binding_defaults=port,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("parameters", [{}, {"asr": {"max_segment_ms": 15000}}])
async def test_producer_create_fixed_parent_and_snapshot_use_frozen_wire(
    parameters: dict[str, Any],
) -> None:
    port = provider()
    async with client(port) as http:
        response = await http.post(
            PATH, json={**binding_request(), "parameter_defaults": parameters}
        )
        assert response.status_code == 201, response.text
        assert (await http.get(FIXED)).json() == response.json()
        snapshot = await http.get(DEFAULTS)
        assert snapshot.status_code == 200, snapshot.text
        validate_response(GET, 200, {"body": snapshot.json()})
        assert snapshot.json()["parameters"] == parameters
        assert snapshot.json()["binding_revision_id"] == response.json()["revision_id"]


async def test_absence_unreadability_and_forged_parent_do_not_become_empty_defaults() -> None:
    port = provider()
    async with client(port) as http:
        missing = await http.get(DEFAULTS)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "binding_defaults_not_found"
        await http.post(PATH, json={**binding_request(), "parameter_defaults": {}})
        original = port.get_defaults.return_value
        port.get_defaults.return_value = replace(original, revision_id="mbrev-foreign")
        assert (await http.get(DEFAULTS)).status_code == 503
        port.get_defaults.side_effect = RuntimeError("private DB details")
        unavailable = await http.get(DEFAULTS)
        assert unavailable.status_code == 503 and "private DB" not in unavailable.text
        assert unavailable.json()["error"]["retryable"] is True


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        ("GET", DEFAULTS, {"content": b"{}"}, 400),
        ("GET", DEFAULTS, {"headers": {"Host": "foreign"}}, 403),
        ("GET", DEFAULTS, {"headers": {"Origin": "https://foreign"}}, 403),
        ("POST", DEFAULTS, {}, 405),
        ("GET", DEFAULTS.replace("/1/", "/01/"), {}, 400),
    ],
)
async def test_snapshot_read_ingress_uses_read_contract(
    method: str, path: str, kwargs: dict[str, Any], status: int
) -> None:
    async with client(provider()) as http:
        response = await http.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    validate_response(GET, status, {"body": response.json(), "headers": dict(response.headers)})
    if status == 405:
        assert response.headers["Allow"] == "GET"


def test_operational_surface_selects_registered_producer_operations_only() -> None:
    legacy = create_app(settings=_settings()).openapi()
    active = create_app(settings=_settings(), binding_defaults=provider()).openapi()
    template = PATH + "/{binding_id}/revisions/{revision}/parameter-defaults"
    assert template not in legacy["paths"] and template in active["paths"]
    assert active["paths"][template]["get"]["operationId"] == GET
    assert PATH + "/{binding_id}/revisions/{revision}" in active["paths"]

    def resolve(document: dict[str, Any], value: Any) -> Any:
        while isinstance(value, dict) and "$ref" in value:
            tokens = value["$ref"].split("/")
            assert tokens[0] == "#"
            value = document
            for token in tokens[1:]:
                value = value[token.replace("~1", "/").replace("~0", "~")]
        return value

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                resolve(active, value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(active)
    for document, expected in ((legacy, False), (active, True)):
        body = resolve(document, document["paths"][PATH]["post"]["requestBody"])
        schema = resolve(document, body["content"]["application/json"]["schema"])
        assert ("parameter_defaults" in schema["properties"]) is expected


async def test_parent_only_creation_cannot_confirm_a_declared_snapshot() -> None:
    port = provider()
    create = port.create.side_effect
    port.create.side_effect = lambda command: BindingDefaultsDraftResult(create(command).binding)
    async with client(port) as http:
        response = await http.post(PATH, json={**binding_request(), "parameter_defaults": {}})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "binding_result_unknown"


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"content": b"{", "headers": {"Content-Type": "application/json"}}, 400),
        ({"json": {**binding_request(), "parameter_defaults": None}}, 400),
        ({"json": {**binding_request(), "parameter_defaults": {"asr": {"language": "fr"}}}}, 422),
        ({"content": b"{}", "headers": {"Content-Type": "text/plain"}}, 415),
        (
            {
                "content": b'{"binding_id":"a","binding_id":"b"}',
                "headers": {"Content-Type": "application/json"},
            },
            400,
        ),
        (
            {
                "json": binding_request(),
                "headers": [
                    ("Idempotency-Key", "original-key-a"),
                    ("Idempotency-Key", "original-key-b"),
                ],
            },
            400,
        ),
    ],
)
async def test_producer_rejects_invalid_framing_or_definition_before_writing(
    kwargs: dict[str, Any],
    status: int,
) -> None:
    port = provider()
    async with client(port) as http:
        response = await http.post(PATH, **kwargs)
    assert response.status_code == status, response.text
    validate_response(
        "createModelBindingDraft",
        status,
        {"body": response.json(), "headers": dict(response.headers)},
    )
    port.create.assert_not_called()


async def test_producer_byte_limit_uses_declared_error_without_writing() -> None:
    port = provider()
    async with client(port, binding_receive_max_bytes=32) as http:
        response = await http.post(PATH, json={**binding_request(), "parameter_defaults": {}})
    assert response.status_code == 413
    validate_response("createModelBindingDraft", 413, {"body": response.json()})
    port.create.assert_not_called()


async def test_configured_but_unopened_writer_is_unavailable_not_absent() -> None:
    settings = Settings(
        **{
            **_settings().model_dump(),
            "binding_defaults_writer_database_url": "synthetic-unopened",
            "asset_allowed_hosts": ("test",),
        }
    )
    app = create_app(settings=settings)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as http:
        unknown = await http.post(PATH, json={**binding_request(), "parameter_defaults": {}})
        unreadable = await http.get(DEFAULTS)
    assert unknown.status_code == unreadable.status_code == 503
    assert unknown.json()["error"]["retryable"] is False
    assert unreadable.json()["error"]["retryable"] is True
    assert app.state.binding_defaults_runtime.provider is None
    assert app.state.binding_defaults_runtime._pool is None


def test_writer_mode_configuration_is_explicit_mutually_exclusive_and_redacted() -> None:
    base = _settings().model_dump()
    assert (
        Settings(
            **{**base, "binding_defaults_writer_database_url": " "}
        ).binding_defaults_writer_database_url
        is None
    )
    configured = Settings(
        **{**base, "binding_defaults_writer_database_url": "postgresql://private-secret"}
    )
    assert configured.binding_defaults_writer_database_url is not None
    assert "private-secret" not in repr(configured)
    with pytest.raises(ValidationError) as failure:
        Settings(
            **{
                **base,
                "binding_writer_database_url": "private-old",
                "binding_defaults_writer_database_url": "private-new",
            }
        )
    assert "private-old" not in str(failure.value) and "private-new" not in str(failure.value)
