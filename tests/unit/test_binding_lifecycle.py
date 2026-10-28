"""Lifecycle commands and their HTTP boundary; PostgreSQL behaviour is qualified separately."""

from __future__ import annotations

from typing import Any, Literal, TypedDict, cast

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.binding_documents import encode_document
from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingActivationCommand,
    BindingLifecycleConflictError,
    BindingLifecycleMode,
    BindingLifecycleNotFoundError,
    BindingLifecycleReceipt,
    BindingLifecycleUnavailableError,
    BindingRetirementCommand,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    ACTIVATE_OPERATION,
    ACTIVATE_PATH,
    RETIRE_OPERATION,
    RETIRE_PATH,
)
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

RETIRE_URL = RETIRE_PATH.format(binding_id="asr-quality", revision="1")
ACTIVATE_URL = ACTIVATE_PATH.format(binding_id="asr-quality", revision="1")
DIGEST = "sha256:" + "a" * 64


class LifecycleFields(TypedDict):
    binding_id: str
    revision: str
    request_id: str
    actor_type: Literal["anonymous", "shared_credential"]
    credential_label: str | None
    key_digest: str | None
    intent_digest: str


def receipt(
    *,
    status: str = "retired",
    retired_at_us: int | None = 1_700_000_000_000_002,
    disabled_reason: str | None = None,
    replayed: bool = False,
) -> BindingLifecycleReceipt:
    return BindingLifecycleReceipt(
        binding_id="asr-quality",
        revision="1",
        revision_id="binding-rev-1",
        definition=encode_document(binding_request()),
        created_at_us=1_700_000_000_000_000,
        published_at_us=1_700_000_000_000_001,
        capacity_manifest_id="cap-asr-quality-7",
        qualification_report_id="qrep-asr-quality-1",
        status=status,  # type: ignore[arg-type]
        retired_at_us=retired_at_us,
        disabled_reason=disabled_reason,
        replayed=replayed,
    )


class Provider:
    def __init__(self, result: BindingLifecycleReceipt | None = None) -> None:
        self.result = result or receipt()
        self.commands: list[Any] = []
        self.error: BaseException | None = None

    def retire(self, command: BindingRetirementCommand) -> BindingLifecycleReceipt:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.result

    def activate(self, command: BindingActivationCommand) -> BindingLifecycleReceipt:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.result


def client(provider: Provider | None = None) -> AsyncClient:
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",)}),
        binding_lifecycle=provider,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_retirement_command_is_strict_about_mode_and_reason() -> None:
    common: LifecycleFields = {
        "binding_id": "asr-quality",
        "revision": "1",
        "request_id": "request-1",
        "actor_type": "anonymous",
        "credential_label": None,
        "key_digest": DIGEST,
        "intent_digest": DIGEST,
    }
    assert BindingRetirementCommand(**common).mode == "retire"
    assert BindingRetirementCommand(**common, mode="disable", reason="capacity") == (
        BindingRetirementCommand(**common, mode="disable", reason="capacity")
    )
    with pytest.raises(ValueError):
        BindingRetirementCommand(**common, mode="disable")
    with pytest.raises(ValueError):
        BindingRetirementCommand(**common, mode=cast(BindingLifecycleMode, "delete"))
    with pytest.raises(ValueError):
        BindingRetirementCommand(**common, reason="")
    invalid_digest: LifecycleFields = {**common, "key_digest": "not-a-digest"}
    with pytest.raises(ValueError):
        BindingRetirementCommand(**invalid_digest)


def test_activation_command_allows_a_cleared_pointer_but_not_a_bad_revision() -> None:
    common: LifecycleFields = {
        "binding_id": "asr-quality",
        "revision": "1",
        "request_id": "request-1",
        "actor_type": "anonymous",
        "credential_label": None,
        "key_digest": None,
        "intent_digest": DIGEST,
    }
    assert BindingActivationCommand(**common).expected_current_revision is None
    assert (
        BindingActivationCommand(**common, expected_current_revision="3").expected_current_revision
        == "3"
    )
    for bad in ("0", "01", "-1", "1.0", ""):
        with pytest.raises(ValueError):
            BindingActivationCommand(**common, expected_current_revision=bad)


def test_receipt_refuses_inconsistent_lifecycle_facts() -> None:
    with pytest.raises(ValueError):
        receipt(status="published", retired_at_us=1)
    with pytest.raises(ValueError):
        receipt(status="retired", retired_at_us=None)
    with pytest.raises(ValueError):
        receipt(status="disabled", retired_at_us=None, disabled_reason="x")
    with pytest.raises(ValueError):
        receipt(status="disabled", disabled_reason=None)


async def test_retirement_returns_the_stored_projection_and_forwards_the_command() -> None:
    provider = Provider()
    async with client(provider) as http:
        response = await http.post(
            RETIRE_URL,
            json={"mode": "retire", "reason": "superseded by revision 2"},
            headers={"Idempotency-Key": "retire-key"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["publication"]["status"] == "retired"
    assert body["publication"]["retired_at"] is not None
    assert body["publication"]["disabled_reason"] is None
    assert "replayed" not in body
    assert response.headers["Cache-Control"] == "no-store"
    command = provider.commands[0]
    assert isinstance(command, BindingRetirementCommand)
    assert (command.binding_id, command.revision, command.mode) == ("asr-quality", "1", "retire")
    assert command.reason == "superseded by revision 2"
    assert command.key_digest is not None and command.key_digest.startswith("sha256:")
    assert command.intent_digest.startswith("sha256:")


async def test_disable_reports_its_reason_and_activation_reaches_the_provider() -> None:
    disabled = Provider(receipt(status="disabled", disabled_reason="emergency capacity hold"))
    async with client(disabled) as http:
        response = await http.post(
            RETIRE_URL, json={"mode": "disable", "reason": "emergency capacity hold"}
        )
    assert response.status_code == 200, response.text
    assert response.headers["X-Binding-Disable-Cleanup"] == "pending_or_unknown"
    assert response.json()["publication"] == {
        "status": "disabled",
        "published_at": "2023-11-14T22:13:20.000001Z",
        "retired_at": "2023-11-14T22:13:20.000002Z",
        "capacity_manifest_id": "cap-asr-quality-7",
        "qualification_report_id": "qrep-asr-quality-1",
        "disabled_reason": "emergency capacity hold",
    }

    activation = Provider(receipt(status="published", retired_at_us=None, replayed=True))
    async with client(activation) as http:
        response = await http.post(ACTIVATE_URL, json={"expected_current_revision": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["publication"]["status"] == "published"
    command = activation.commands[0]
    assert isinstance(command, BindingActivationCommand)
    assert command.expected_current_revision == "1"


@pytest.mark.parametrize(
    "url,body",
    [
        (RETIRE_URL, {"mode": "retire"}),
        (ACTIVATE_URL, {"expected_current_revision": "1"}),
    ],
)
@pytest.mark.parametrize("authorization", [None, "Bearer forged", "Basic Zm9yZ2Vk"])
async def test_unverified_authorization_does_not_claim_an_authenticated_actor(
    url: str, body: dict[str, Any], authorization: str | None
) -> None:
    provider = Provider()
    headers = {"Authorization": authorization} if authorization is not None else {}
    async with client(provider) as http:
        response = await http.post(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert len(provider.commands) == 1
    assert provider.commands[0].actor_type == "anonymous"
    assert provider.commands[0].credential_label is None


@pytest.mark.parametrize(
    "url,body",
    [
        (RETIRE_URL, {"mode": "retire"}),
        (ACTIVATE_URL, {"expected_current_revision": "1"}),
    ],
)
async def test_duplicate_authorization_is_rejected_before_the_writer(
    url: str, body: dict[str, Any]
) -> None:
    provider = Provider()
    async with client(provider) as http:
        response = await http.post(
            url,
            json=body,
            headers=[("Authorization", "Bearer one"), ("Authorization", "Basic two")],
        )
    assert response.status_code == 400, response.text
    assert provider.commands == []


@pytest.mark.parametrize(
    "error,status,code",
    [
        (
            BindingLifecycleConflictError("revision_already_retired"),
            409,
            "revision_already_retired",
        ),
        (BindingLifecycleConflictError("revision_not_current"), 409, "revision_not_current"),
        (BindingLifecycleConflictError("configuration_changed"), 409, "configuration_changed"),
        (BindingLifecycleConflictError("idempotency_conflict"), 409, "idempotency_conflict"),
        (BindingLifecycleNotFoundError(), 404, "binding_not_found"),
        (BindingLifecycleUnavailableError(), 503, "binding_result_unknown"),
        (RuntimeError("private-dsn"), 503, "binding_result_unknown"),
    ],
)
async def test_lifecycle_errors_are_explicit_and_never_leak_details(
    error: BaseException, status: int, code: str
) -> None:
    provider = Provider()
    provider.error = error
    async with client(provider) as http:
        response = await http.post(RETIRE_URL, json={"mode": "retire"})
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is False
    assert "private-dsn" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "delete"},
        {"mode": "disable"},
        {"mode": "disable", "reason": ""},
        {"mode": "retire", "extra": True},
        {"mode": "retire", "reason": "x" * 2001},
    ],
)
async def test_invalid_retirement_bodies_never_reach_the_writer(body: dict[str, Any]) -> None:
    provider = Provider()
    async with client(provider) as http:
        response = await http.post(RETIRE_URL, json=body)
    assert response.status_code == 400, response.text
    assert provider.commands == []


@pytest.mark.parametrize(
    "body",
    [{}, {"expected_current_revision": "01"}, {"expected_current_revision": "1", "x": 1}],
)
async def test_invalid_activation_bodies_never_reach_the_writer(body: dict[str, Any]) -> None:
    provider = Provider()
    async with client(provider) as http:
        response = await http.post(ACTIVATE_URL, json=body)
    assert response.status_code == 400, response.text
    assert provider.commands == []


async def test_duplicate_headers_wrong_methods_and_missing_provider_are_refused() -> None:
    provider = Provider()
    async with client(provider) as http:
        duplicate = await http.post(
            RETIRE_URL,
            json={"mode": "retire"},
            headers=[("Idempotency-Key", "a"), ("Idempotency-Key", "b")],
        )
        assert duplicate.status_code == 400
        wrong = await http.get(RETIRE_URL)
        assert wrong.status_code == 405
        query = await http.post(RETIRE_URL + "?x=1", json={"mode": "retire"})
        assert query.status_code == 400
    assert provider.commands == []

    async with client(None) as http:
        response = await http.post(RETIRE_URL, json={"mode": "retire"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "binding_result_unknown"


def test_served_contract_declares_both_lifecycle_operations() -> None:
    from speechnexus_backend.speech.interfaces.http.binding_contract import binding_http_contract

    paths = binding_http_contract()["paths"]
    assert paths[RETIRE_PATH]["post"]["operationId"] == RETIRE_OPERATION
    assert paths[ACTIVATE_PATH]["post"]["operationId"] == ACTIVATE_OPERATION
