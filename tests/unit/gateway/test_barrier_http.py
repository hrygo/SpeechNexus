"""HTTP fencing uses the real ingress guard; no daemon, model or network is needed."""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.domain.fencing import (
    BarrierConflictError,
    BarrierSnapshot,
    IngressFence,
)
from speechnexus_gateway.domain.submission import (
    GatewayBarrierFencedError,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmissionStoreUnavailableError,
)
from speechnexus_gateway.settings import Settings
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.fake.readiness import StaticProbe
from tests.unit.gateway.test_submit_execution import COMMAND, NOW, identity_for

TOKEN = "synthetic-gateway-component-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "synthetic-http-request"}
PATH = "/internal/v1/gateways/gateway-1/fencing-barrier"
BODY: dict[str, Any] = {
    "pool_id": "asr-batch",
    "new_owner_scheduler_id": "scheduler-new",
    "min_accepted_fencing_token": 8,
    "abort_in_flight": False,
}


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "control_database_url": SecretStr("postgresql://unused@127.0.0.1/unused"),
        "node_id": "node-1",
        "gateway_id": "gateway-1",
        "component_token": SecretStr(TOKEN),
        "ingress_lock_directory": tmp_path,
    }
    return Settings(**(fields | overrides))


def app_for(
    tmp_path: Path, *, ledger: InMemorySubmissionLedger | None = None
) -> tuple[FastAPI, GatewayIngress]:
    ingress = fake_ingress(timeout_seconds=0.01)
    resolved_ledger = ledger or InMemorySubmissionLedger(now=lambda: NOW)
    service = GatewayBarrierService(ingress=ingress, ledger=resolved_ledger)
    app = create_app(
        settings=settings_for(tmp_path),
        barrier_control=service,
        execution_query=ExecutionQueryService(ingress=ingress, ledger=resolved_ledger),
        readiness=ReadinessService([StaticProbe("control_postgresql")], timeout_seconds=1),
    )
    return app, ingress


async def test_sealing_and_replay_expose_the_enforced_floor_and_correlation_id(
    tmp_path: Path,
) -> None:
    app, ingress = app_for(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(PATH, json=BODY, headers=HEADERS)
        replay = await client.post(PATH, json=BODY, headers=HEADERS)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["barrier_sealed"] is True
    assert first.json()["enforced_min_fencing_token"] == 8
    assert first.headers["x-request-id"] == HEADERS["X-Request-ID"]
    with pytest.raises(GatewayBarrierFencedError), ingress.dispatch(identity_for(COMMAND)):
        pytest.fail("HTTP confirmation did not enforce the local floor")


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic synthetic"])
async def test_credential_is_required_before_sealing(
    tmp_path: Path, authorization: str | None
) -> None:
    app, ingress = app_for(tmp_path)
    headers = {} if authorization is None else {"Authorization": authorization}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PATH, json=BODY, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "component_credential_invalid"
    assert response.headers["x-request-id"]
    with ingress.dispatch(identity_for(COMMAND)):
        pass  # Unauthorized input did not poison the local floor.


@pytest.mark.parametrize(
    "override",
    [
        {"min_accepted_fencing_token": "8"},
        {"min_accepted_fencing_token": True},
        {"min_accepted_fencing_token": 2**53},
        {"abort_in_flight": "false"},
        {"pool_id": "not-a-pool"},
        {"new_owner_scheduler_id": "NO"},
        {"private_payload": "synthetic-sensitive-marker"},
    ],
)
async def test_boundary_rejects_invalid_raw_types_and_unknown_fields_without_echoing_input(
    tmp_path: Path, override: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    app, ingress = app_for(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PATH, json=BODY | override, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_execution_request"
    assert "synthetic-sensitive-marker" not in response.text + caplog.text
    with ingress.dispatch(identity_for(COMMAND)):
        pass


async def test_foreign_gateway_and_unimplemented_abort_are_rejected(tmp_path: Path) -> None:
    app, ingress = app_for(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        foreign = await client.post(
            PATH.replace("gateway-1", "gateway-other"), json=BODY, headers=HEADERS
        )
        abort = await client.post(PATH, json=BODY | {"abort_in_flight": True}, headers=HEADERS)
        invalid_path = await client.post(
            PATH.replace("gateway-1", "UPPER"), json=BODY, headers=HEADERS
        )
    assert foreign.status_code == invalid_path.status_code == 422
    assert abort.status_code == 409
    assert abort.json()["error"]["code"] == "execution_cleanup_pending"
    with ingress.dispatch(identity_for(COMMAND)):
        pass


async def test_a_live_sender_causes_retryable_unavailability_and_no_sealed_ack(
    tmp_path: Path,
) -> None:
    app, ingress = app_for(tmp_path)
    with ingress.dispatch(identity_for(COMMAND)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(PATH, json=BODY, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "control_store_unavailable",
        "message": "The control operation could not complete within its bounded wait.",
        "retryable": True,
        "recovery": "retry_transaction",
    }
    assert "barrier_sealed" not in response.json()
    with pytest.raises(GatewayBarrierFencedError), ingress.dispatch(identity_for(COMMAND)):
        pytest.fail("HTTP timeout reopened the old floor")


@pytest.mark.parametrize(
    "error,code,recovery,status",
    [
        (SubmissionOutcomeUnknownError(), "control_outcome_unknown", "query_original", 503),
        (
            SubmissionStoreUnavailableError(),
            "control_store_unavailable",
            "retry_transaction",
            503,
        ),
        (SubmissionStoreError(), "control_reconciliation_required", "reconcile", 503),
        (BarrierConflictError(), "control_state_conflict", "query_current_authority", 409),
    ],
)
async def test_store_failures_preserve_recovery_semantics_and_never_acknowledge(
    tmp_path: Path, error: Exception, code: str, recovery: str, status: int
) -> None:
    class FailedLedger(InMemorySubmissionLedger):
        def seal_barrier(self, fence: IngressFence) -> BarrierSnapshot:
            raise error

    app, ingress = app_for(tmp_path, ledger=FailedLedger())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PATH, json=BODY, headers=HEADERS)
    assert response.status_code == status
    detail = response.json()["error"]
    assert detail["code"] == code
    assert detail["recovery"] == recovery
    assert detail["retryable"] is (recovery == "retry_transaction")
    assert response.headers["x-request-id"]
    with pytest.raises(GatewayBarrierFencedError), ingress.dispatch(identity_for(COMMAND)):
        pytest.fail("store failure reopened the old floor")


async def test_stale_local_floor_has_a_stable_conflict_response(tmp_path: Path) -> None:
    app, _ = app_for(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(PATH, json=BODY, headers=HEADERS)).status_code == 200
        response = await client.post(
            PATH, json=BODY | {"min_accepted_fencing_token": 7}, headers=HEADERS
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_fencing_token"


@pytest.mark.parametrize(
    "override",
    [
        {"component_token": None},
        {"ingress_lock_directory": None},
        {"ingress_lock_directory": Path("relative")},
        {"component_token": SecretStr("")},
    ],
)
def test_partial_or_unsafe_control_configuration_fails_at_startup(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        settings_for(tmp_path, **override)


def test_control_requires_explicit_node_and_gateway_identities(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="explicit node and gateway"):
        Settings(
            control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
            component_token=SecretStr(TOKEN),
            ingress_lock_directory=tmp_path,
        )


def test_invalid_environment_configuration_does_not_print_the_component_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN", TOKEN)
    monkeypatch.delenv("SPEECHNEXUS_GATEWAY_INGRESS_LOCK_DIRECTORY", raising=False)
    with pytest.raises(ValidationError) as rejected:
        Settings(
            control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
            node_id="node-1",
            gateway_id="gateway-1",
            _env_file=None,
        )
    assert TOKEN not in str(rejected.value)


async def test_control_cannot_acknowledge_without_its_production_lifespan(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(tmp_path),
        readiness=ReadinessService([StaticProbe("control_postgresql")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PATH, json=BODY, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["recovery"] == "retry_transaction"
    assert app.state.barrier_control is None
