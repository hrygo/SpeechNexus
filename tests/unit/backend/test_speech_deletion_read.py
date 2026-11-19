"""The deletion read surface projects the ledger and performs no execution work.

The fake store answers exactly the read protocol; every execution method raises, so
a read that resumed the purge, re-ran the plan or wrote a row fails here rather than
looking like a successful query. The PostgreSQL module pins the same decision with a
read-only transaction; this module pins the application and HTTP decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.deletions import SpeechDeletions
from speechnexus_backend.speech.domain.deletion import (
    DeletionLedgerUnavailableError,
    DeletionRecord,
    DeletionResult,
    DeletionTarget,
    DeletionTargetNotFoundError,
    deletion_payload,
)
from tests.contract.test_architecture_repairs import _deletion_validator
from tests.contract.test_speech_http_statuses import assert_error_contract
from tests.unit.backend.test_health import _settings

WORKSPACE = "workspace-default"
OTHER_WORKSPACE = "workspace-other"
NOW = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
DELETION = "dl-read-1"
# The frozen `Deletion` shape: the read may not add a field, even an internal one.
DELETION_KEYS = {
    "id",
    "object",
    "plan_id",
    "plan_digest",
    "status",
    "cleanup_state",
    "created_at",
    "updated_at",
    "result",
    "error",
}


def _digest(letter: str) -> str:
    return "sha256:" + letter * 64


def succeeded() -> DeletionRecord:
    target = DeletionTarget("speech", "asset", "asset-read-1", 1)
    return DeletionRecord(
        id=DELETION,
        plan_id="dp-read-1",
        plan_digest=_digest("a"),
        status="succeeded",
        cleanup_state="complete",
        created_at=NOW,
        updated_at=NOW,
        result=DeletionResult(
            deleted=(target,),
            retained_independent_copies=(),
            ledger_id=DELETION,
            completed_at=NOW,
        ),
    )


def queued() -> DeletionRecord:
    return DeletionRecord(
        id=DELETION,
        plan_id="dp-read-1",
        plan_digest=_digest("a"),
        status="queued",
        cleanup_state="pending",
        created_at=NOW,
        updated_at=NOW,
    )


class Executions:
    """One record map plus a call log; nothing execution-shaped is callable."""

    def __init__(
        self,
        records: dict[str, DeletionRecord] | None = None,
        *,
        workspace: str = WORKSPACE,
    ) -> None:
        self.records = records or {}
        self.workspace = workspace
        self.calls: list[str] = []
        self.reads: list[tuple[str, str]] = []

    def read(self, deletion_id: str, *, workspace_id: str) -> DeletionRecord | None:
        self.calls.append("read")
        self.reads.append((deletion_id, workspace_id))
        if workspace_id != self.workspace:
            # The PostgreSQL adapter filters the row by workspace; the fake keeps
            # the same contract so the service-level test measures the real rule.
            return None
        return self.records.get(deletion_id)

    def accept(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a read must not accept or resume an execution")

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a read must not complete an execution")

    def release_claim(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a read must not release a purge claim")

    def await_completion(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a read must not wait for another executor")


def service(executions: Executions | None) -> SpeechDeletions:
    return SpeechDeletions(
        cast(Any, object()),
        workspace_id=WORKSPACE,
        executions=cast(Any, executions),
    )


def http_app(executions: Executions | None) -> Any:
    return create_app(settings=_settings(), deletions=service(executions))


async def get(
    application: Any, deletion_id: str, *, request_id: str | None = None
) -> httpx.Response:
    headers = {} if request_id is None else {"X-Request-ID": request_id}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as http:
        return await http.get(f"/v1/nexus/deletions/{deletion_id}", headers=headers)


def test_read_projects_the_stored_record_for_this_workspace() -> None:
    executions = Executions({DELETION: succeeded()})
    record = service(executions).read(DELETION)
    assert record == succeeded()
    assert executions.calls == ["read"]
    assert executions.reads == [(DELETION, WORKSPACE)]


def test_read_of_an_unknown_deletion_is_not_found() -> None:
    with pytest.raises(DeletionTargetNotFoundError):
        service(Executions()).read("dl-read-missing")


def test_another_workspaces_record_is_not_found() -> None:
    executions = Executions({DELETION: succeeded()}, workspace=OTHER_WORKSPACE)
    with pytest.raises(DeletionTargetNotFoundError):
        service(executions).read(DELETION)
    # The lookup carries this deployment's workspace; a row outside it cannot match.
    assert executions.reads == [(DELETION, WORKSPACE)]


def test_read_without_a_ledger_is_unavailable_not_empty() -> None:
    with pytest.raises(DeletionLedgerUnavailableError):
        service(None).read(DELETION)


async def test_http_returns_the_contract_deletion() -> None:
    response = await get(http_app(Executions({DELETION: succeeded()})), DELETION)
    assert response.status_code == 200, response.text
    payload = response.json()
    _deletion_validator("Deletion").validate(payload)
    assert payload == deletion_payload(succeeded())
    assert response.headers["cache-control"] == "no-store"


async def test_http_missing_deletion_is_the_declared_404() -> None:
    response = await get(http_app(Executions()), "dl-read-missing", request_id="read-not-found")
    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "resource_not_found"
    assert error["type"] == "not_found_error"
    assert error["retryable"] is False
    assert error["param"] is None
    assert error["request_id"] == "read-not-found"
    assert response.headers["cache-control"] == "no-store"
    assert_error_contract("speechGetDeletion", response)


async def test_http_malformed_identifier_is_not_found_without_a_lookup() -> None:
    executions = Executions({DELETION: succeeded()})
    response = await get(http_app(executions), "NOT-A-DELETION-ID")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "resource_not_found"
    assert executions.calls == []


async def test_http_without_a_service_answers_unavailable() -> None:
    response = await get(http_app(None), DELETION)
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "deletion_unavailable"
    assert error["retryable"] is True


@pytest.mark.parametrize("record", [queued(), succeeded()])
async def test_the_wire_shape_never_exposes_a_purge_claim(
    record: DeletionRecord,
) -> None:
    """``purge_claimed_at`` is execution state, not part of the frozen resource.

    Both a claimed and an unclaimed row project the same keys; only ``status`` and
    ``cleanup_state`` say anything about clean-up progress.
    """
    response = await get(http_app(Executions({DELETION: record})), DELETION)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == DELETION_KEYS
    assert "purge_claimed_at" not in payload
    assert not any("claim" in key for key in payload)
    assert payload["status"] == record.status
    assert payload["cleanup_state"] == record.cleanup_state
    assert payload["result"] == (
        None if record.result is None else deletion_payload(record)["result"]
    )
    assert payload["error"] is None
