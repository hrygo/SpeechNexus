"""HTTP boundary: contract-validated evidence, one stable code per decision."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts.admin_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineReleaseService,
    QuarantineUnavailableError,
    QuarantineUnknownError,
)
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseEvidence,
    ReleaseLedger,
    ReleaseOutcome,
)
from speechnexus_backend.speech.interfaces.http.quarantine_release_errors import (
    ERRORS,
    OPERATION,
)
from speechnexus_backend.speech.interfaces.http.quarantine_releases import PATH
from tests.unit.backend.test_health import _settings

DIGEST = "sha256:" + "a" * 64
REQUEST_ID = "request-quarantine-release-1"
OBSERVED_AT = "2026-11-13T00:00:00.123456Z"
OBSERVED_AT_US = 1_794_528_000_123_456
BUDGET_RELEASED_AT = "2026-11-13T00:00:01.000000Z"
LEASE_ID = "lease-stage-main"


class _Transactions:
    """A programmable port: each level returns or raises one scripted result."""

    def __init__(self) -> None:
        self.outcome: ReleaseOutcome = _ledger()
        self.lookup_failure: Exception | None = None
        self.commit_failure: Exception | None = None

    def lookup(self, evidence: ReleaseEvidence) -> ReleaseOutcome | None:
        if self.lookup_failure is not None:
            raise self.lookup_failure
        return None

    def commit(self, evidence: ReleaseEvidence) -> ReleaseOutcome:
        if self.commit_failure is not None:
            raise self.commit_failure
        return self.outcome


def _ledger(
    *, key_digest: str | None = "sha256:" + "b" * 64, replay: bool = False
) -> ReleaseOutcome:
    return ReleaseOutcome(
        ReleaseLedger(
            LEASE_ID,
            "operator_verified_instance_terminated",
            DIGEST,
            OBSERVED_AT_US,
            "container record verified by the on-call operator",
            1_794_528_001_000_000,
            ("submit-stage-main",),
            key_digest,
            "sha256:" + "c" * 64,
        ),
        replay,
    )


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "evidence_kind": "operator_verified_instance_terminated",
        "evidence_digest": DIGEST,
        "observed_at": OBSERVED_AT,
        "reason": "container record verified by the on-call operator",
    }
    body.update(overrides)
    return body


def _client(service: QuarantineReleaseService | None) -> AsyncClient:
    app = create_app(settings=_settings(), quarantine_releases=service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def port() -> _Transactions:
    return _Transactions()


@pytest.fixture
async def client(port: _Transactions) -> AsyncIterator[AsyncClient]:
    async with _client(QuarantineReleaseService(port)) as client:
        yield client


async def post(client: AsyncClient, body: Any, **headers: str) -> httpx.Response:
    return await client.post(
        PATH.format(lease_id=LEASE_ID),
        json=body,
        headers={"X-Request-ID": REQUEST_ID, **headers},
    )


async def test_first_release_returns_the_committed_fact(client: AsyncClient) -> None:
    response = await post(client, _body(), **{"Idempotency-Key": "operator-poll-0001"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["lease_id"] == LEASE_ID
    assert body["object"] == "nexus.quarantine_release"
    assert body["lease_state"] == "released"
    assert body["cleanup_state"] == "complete"
    assert body["budget_released_at"] == BUDGET_RELEASED_AT
    assert body["evidence_observed_at"] == OBSERVED_AT
    assert body["released_submit_intent_ids"] == ["submit-stage-main"]
    assert body["idempotent_replay"] is False
    validate_response(OPERATION, 200, {"body": body})


async def test_replay_reports_the_original_budget(port: _Transactions) -> None:
    port.outcome = _ledger(replay=True)

    async with _client(QuarantineReleaseService(port)) as client:
        response = await post(client, _body(), **{"Idempotency-Key": "operator-poll-0001"})

    assert response.status_code == 200
    assert response.json()["idempotent_replay"] is True
    assert response.json()["budget_released_at"] == BUDGET_RELEASED_AT


async def test_absent_idempotency_key_is_accepted(port: _Transactions) -> None:
    async with _client(QuarantineReleaseService(port)) as client:
        response = await post(client, _body())

    assert response.status_code == 200


@pytest.mark.parametrize(
    "code,status",
    [
        ("not_found", 404),
        ("idempotency_conflict", 409),
        ("quarantine_release_conflict", 409),
        ("invalid", 400),
    ],
)
async def test_rejections_become_their_declared_error(
    port: _Transactions, code: str, status: int
) -> None:
    port.commit_failure = QuarantineReleaseRejectedError(code)
    async with _client(QuarantineReleaseService(port)) as client:
        response = await post(client, _body())

    assert response.status_code == status
    error = response.json()["error"]
    assert error["retryable"] is False
    assert response.headers["x-should-retry"] == "false"
    assert error["request_id"] == REQUEST_ID
    if code == "not_found":
        assert error["code"] == "quarantine_release_not_found"
    validate_response(
        OPERATION, status, {"body": response.json(), "headers": dict(response.headers)}
    )


async def test_unavailable_storage_is_retryable(port: _Transactions) -> None:
    port.lookup_failure = QuarantineUnavailableError()
    async with _client(QuarantineReleaseService(port)) as client:
        response = await post(client, _body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "quarantine_release_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert response.headers["x-should-retry"] == "true"


async def test_unknown_result_is_not_retryable(port: _Transactions) -> None:
    port.commit_failure = QuarantineUnknownError()
    async with _client(QuarantineReleaseService(port)) as client:
        response = await post(client, _body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "quarantine_release_unknown"
    assert response.json()["error"]["retryable"] is False


async def test_missing_service_reports_unavailable() -> None:
    async with _client(None) as client:
        response = await post(client, _body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "quarantine_release_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        _body(evidence_kind="instance_terminated_by_controller"),
        _body(evidence_digest="sha256:short"),
        _body(reason=""),
        _body(reason="   "),
        _body(reason="x" * 2001),
        _body(observed_at="yesterday"),
        _body(observed_at="2026-11-13T00:00:00.123456"),
        {"evidence_kind": "operator_verified_instance_terminated"},
        [],
    ],
)
async def test_invalid_evidence_is_rejected_before_storage(port: _Transactions, body: Any) -> None:
    async with _client(QuarantineReleaseService(port)) as client:
        response = await post(client, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "quarantine_release_invalid"


@pytest.mark.parametrize("key", ["short", "x" * 129, "newline\nkey-value"])
async def test_invalid_idempotency_key_is_rejected(port: _Transactions, key: str) -> None:
    async with _client(QuarantineReleaseService(port)) as client:
        response = await post(client, _body(), **{"Idempotency-Key": key})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_invalid"


async def test_duplicate_evidence_digest_is_rejected(port: _Transactions) -> None:
    async with _client(QuarantineReleaseService(port)) as client:
        response = await client.post(
            PATH.format(lease_id=LEASE_ID),
            content=(
                '{"evidence_kind":"operator_verified_instance_terminated",'
                '"evidence_digest":"' + DIGEST + '","evidence_digest":"' + DIGEST + '",'
                '"observed_at":"' + OBSERVED_AT + '","reason":"verified"}'
            ).encode(),
            headers={
                "X-Request-ID": REQUEST_ID,
                "Content-Type": "application/json",
                "Idempotency-Key": "operator-poll-0001",
            },
        )

    assert response.status_code == 400


async def test_error_catalogue_covers_every_mapped_code() -> None:
    assert set(ERRORS) == {
        "quarantine_release_invalid",
        "idempotency_key_invalid",
        "quarantine_release_not_found",
        "idempotency_conflict",
        "quarantine_release_conflict",
        "quarantine_release_unknown",
        "quarantine_release_unavailable",
    }
    for _code, (status, kind, message) in ERRORS.items():
        assert status in (400, 404, 409, 503)
        assert kind.endswith("_error")
        assert message
    assert datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=OBSERVED_AT_US) == datetime(
        2026, 11, 13, 0, 0, 0, 123456, tzinfo=UTC
    )
