"""The publication command's HTTP projection and its exact failure surface."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.binding_documents import encode_document
from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingLifecycleConflictError,
    BindingLifecycleNotFoundError,
    BindingLifecycleReceipt,
    BindingLifecycleUnavailableError,
    BindingPublicationCommand,
    BindingPublicationEvidenceError,
    BindingPublicationHeadConflictError,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    PUBLISH_OPERATION,
    PUBLISH_PATH,
)
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

URL = PUBLISH_PATH.format(binding_id="asr-quality")
KEY = {"Idempotency-Key": "publish-asr-quality-1-20261113"}
BODY = {
    "expected_revision": "1",
    "expected_current_revision": None,
    "capacity_manifest_id": "cap-asr-quality-7",
    "qualification_report_id": "qrep-asr-quality-1",
}
DIGEST = "sha256:" + "a" * 64


class CommandFields(TypedDict):
    binding_id: str
    revision: str
    request_id: str
    actor_type: Literal["anonymous", "shared_credential"]
    credential_label: str | None
    key_digest: str | None
    intent_digest: str


def definition() -> bytes:
    """A stored create definition plus the write-only defaults the wire must drop."""

    body = binding_request()
    body.pop("binding_id")
    body["execution_defaults"] = {"temperature": 0.25}
    return encode_document(body)


def receipt(*, replayed: bool = False) -> BindingLifecycleReceipt:
    return BindingLifecycleReceipt(
        binding_id="asr-quality",
        revision="1",
        revision_id="binding-rev-1",
        definition=definition(),
        created_at_us=1_700_000_000_000_000,
        published_at_us=1_700_000_000_000_001,
        capacity_manifest_id="cap-asr-quality-7",
        qualification_report_id="qrep-asr-quality-1",
        status="published",
        retired_at_us=None,
        disabled_reason=None,
        replayed=replayed,
    )


class Provider:
    def __init__(self, result: BindingLifecycleReceipt | None = None) -> None:
        self.result = result or receipt()
        self.commands: list[BindingPublicationCommand] = []
        self.error: BaseException | None = None

    def publish(self, command: BindingPublicationCommand) -> BindingLifecycleReceipt:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.result

    def retire(self, command: Any) -> BindingLifecycleReceipt:
        raise AssertionError("publication must not reach the retirement port")

    def activate(self, command: Any) -> BindingLifecycleReceipt:
        raise AssertionError("publication must not reach the activation port")


def client(provider: Provider | None = None) -> AsyncClient:
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",)}),
        binding_lifecycle=provider,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_publication_command_is_strict_about_evidence_and_revisions() -> None:
    common: CommandFields = {
        "binding_id": "asr-quality",
        "revision": "1",
        "request_id": "request-1",
        "actor_type": "anonymous",
        "credential_label": None,
        "key_digest": DIGEST,
        "intent_digest": DIGEST,
    }
    assert (
        BindingPublicationCommand(
            **common,
            expected_current_revision=None,
            capacity_manifest_id="cap-asr-quality-7",
            qualification_report_id="qrep-asr-quality-1",
        ).expected_current_revision
        is None
    )
    for bad_revision in ("0", "01", "-1", "1.0", ""):
        with pytest.raises(ValueError):
            BindingPublicationCommand(
                **common,
                expected_current_revision=bad_revision,
                capacity_manifest_id="cap-asr-quality-7",
                qualification_report_id="qrep-asr-quality-1",
            )
    for bad_identity in ("", "Cap Upper", "-cap", "ca"):
        with pytest.raises(ValueError):
            BindingPublicationCommand(
                **common,
                expected_current_revision=None,
                capacity_manifest_id=bad_identity,
                qualification_report_id="qrep-asr-quality-1",
            )
    factory: Any = BindingPublicationCommand
    with pytest.raises(TypeError):
        factory(**common)  # evidence is not optional


async def test_publication_creates_201_and_replays_200_with_the_stored_document() -> None:
    provider = Provider()
    async with client(provider) as http:
        created = await http.post(URL, json=BODY, headers=KEY)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["binding_id"] == "asr-quality"
    assert body["revision"] == "1"
    assert body["publication"] == {
        "status": "published",
        "published_at": "2023-11-14T22:13:20.000001Z",
        "retired_at": None,
        "capacity_manifest_id": "cap-asr-quality-7",
        "qualification_report_id": "qrep-asr-quality-1",
        "disabled_reason": None,
    }
    # Write-only draft input and internal receipt facts never enter the response.
    assert "execution_defaults" not in body
    assert "replayed" not in body
    assert "key_digest" not in body
    assert "intent_digest" not in body
    assert created.headers["Cache-Control"] == "no-store"
    command = provider.commands[0]
    assert isinstance(command, BindingPublicationCommand)
    assert (command.binding_id, command.revision) == ("asr-quality", "1")
    assert command.expected_current_revision is None
    assert command.capacity_manifest_id == "cap-asr-quality-7"
    assert command.qualification_report_id == "qrep-asr-quality-1"
    assert command.key_digest is not None and command.key_digest.startswith("sha256:")
    assert command.intent_digest.startswith("sha256:")

    replay = Provider(receipt(replayed=True))
    async with client(replay) as http:
        replayed = await http.post(URL, json=BODY, headers=KEY)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == body


async def test_publication_replay_only_depends_on_the_canonical_intent() -> None:
    first, second = Provider(), Provider()
    async with client(first) as http:
        await http.post(URL, json=BODY, headers=KEY)
    async with client(second) as http:
        await http.post(URL, json=BODY, headers={"Idempotency-Key": "another-key"})
    # The key is the replay selector, never part of the intent: only the body
    # and the addressed resource determine whether two calls mean the same thing.
    assert first.commands[0].key_digest != second.commands[0].key_digest
    assert first.commands[0].intent_digest == second.commands[0].intent_digest

    changed = Provider()
    async with client(changed) as http:
        await http.post(URL, json={**BODY, "capacity_manifest_id": "cap-other"}, headers=KEY)
    assert changed.commands[0].intent_digest != first.commands[0].intent_digest


@pytest.mark.parametrize(
    "error,status,code",
    [
        (BindingLifecycleConflictError("binding_not_draft"), 409, "binding_not_draft"),
        (BindingLifecycleConflictError("idempotency_conflict"), 409, "idempotency_conflict"),
        (
            BindingPublicationHeadConflictError("2"),
            409,
            "configuration_changed",
        ),
        (BindingLifecycleNotFoundError(), 404, "binding_not_found"),
        (
            BindingPublicationEvidenceError(),
            422,
            "capacity_manifest_mismatch",
        ),
        (BindingLifecycleUnavailableError(), 503, "binding_result_unknown"),
        (RuntimeError("private-dsn"), 503, "binding_result_unknown"),
    ],
)
async def test_publication_errors_are_explicit_and_never_leak_details(
    error: BaseException, status: int, code: str
) -> None:
    provider = Provider()
    provider.error = error
    async with client(provider) as http:
        response = await http.post(URL, json=BODY, headers=KEY)
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is False
    assert "private-dsn" not in response.text
    if code == "configuration_changed":
        assert 'expected_current_revision="2"' in body["error"]["message"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {**BODY, "expected_revision": "01"},
        {**BODY, "expected_revision": None},
        {**BODY, "capacity_manifest_id": ""},
        {**BODY, "capacity_manifest_id": None},
        {**BODY, "qualification_report_id": 1},
        {**BODY, "validation_json": {}},
        {k: v for k, v in BODY.items() if k != "expected_current_revision"},
    ],
)
async def test_invalid_publication_bodies_never_reach_the_writer(body: dict[str, Any]) -> None:
    provider = Provider()
    async with client(provider) as http:
        response = await http.post(URL, json=body, headers=KEY)
    assert response.status_code == 400, response.text
    assert provider.commands == []


async def test_a_publication_without_a_configured_writer_is_unknown() -> None:
    async with client(None) as http:
        response = await http.post(URL, json=BODY, headers=KEY)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "binding_result_unknown"


async def test_publication_rejects_wrong_methods_query_duplicate_keys_and_forged_identity() -> None:
    provider = Provider()
    async with client(provider) as http:
        assert (await http.get(URL)).status_code == 405
        assert (await http.post(URL + "?x=1", json=BODY, headers=KEY)).status_code == 400
        duplicate = await http.post(
            URL, json=BODY, headers=[("Idempotency-Key", "a"), ("Idempotency-Key", "b")]
        )
        assert duplicate.status_code == 400
        assert provider.commands == []
        forged = await http.post(
            URL,
            json=BODY,
            headers={**KEY, "Authorization": "Bearer forged"},
        )
    assert forged.status_code == 201, forged.text
    command = provider.commands[0]
    assert command.actor_type == "anonymous"
    assert command.credential_label is None


def test_served_contract_declares_the_publication_operation_and_ingress_responses() -> None:
    from speechnexus_backend.speech.interfaces.http.binding_contract import binding_http_contract

    post = binding_http_contract()["paths"][PUBLISH_PATH]["post"]
    assert post["operationId"] == PUBLISH_OPERATION
    assert set(post["responses"]) >= {
        "200",
        "201",
        "400",
        "401",
        "403",
        "404",
        "405",
        "408",
        "409",
        "413",
        "415",
        "422",
        "429",
        "503",
    }
    # The request names the draft revision in the body; the path has no revision.
    assert "{" not in PUBLISH_PATH.rsplit("/", 1)[1]
    schema = binding_http_contract()["components"]["schemas"][
        "BindingDraft_ModelBindingPublicationRequest"
    ]
    assert schema["additionalProperties"] is False
    assert "validation_json" not in schema["properties"]


def test_publication_response_cannot_claim_an_unpublished_revision() -> None:
    from speechnexus_contracts import ContractValidationError
    from speechnexus_contracts.binding_http import validate_response

    body = {"schema_version": "1.0.0"}
    with pytest.raises(ContractValidationError):
        validate_response(PUBLISH_OPERATION, 201, {"body": body})
    # A replay is a 200; a fresh publication is 201. The store decides which,
    # and the projection never downgrades a published revision to draft.
    from speechnexus_backend.speech.interfaces.http.binding_lifecycle import _receipt_wire

    wire = _receipt_wire(receipt())
    assert wire["publication"]["status"] == "published"
    assert "execution_defaults" not in wire
