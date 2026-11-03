"""Versioned recovery stays on the original authenticated GET without widening old JSON."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts.execution import parse

from speechnexus_gateway.domain.recovery import AbortReceipt, AbortRecovery, ExecutionSnapshot
from tests.fake.execution import InMemorySubmissionLedger
from tests.support.contracts import validator
from tests.unit.gateway.test_barrier_http import HEADERS, app_for
from tests.unit.gateway.test_execution_recovery import pending
from tests.unit.gateway.test_submit_execution import NOW

MEDIA = "application/vnd.speechnexus.execution-recovery.v1+json"
PATH = "/internal/v1/executions/intent-1"


def test_enhanced_snapshot_keeps_original_coordination_representation() -> None:
    ledger = RecoveryLedger(now=lambda: NOW)
    pending(ledger)
    snapshot = ledger.query_outcome("intent-1")
    assert snapshot is not None
    enhanced = snapshot.status()
    parse("ExecutionRecoveryStatusResponse", enhanced)
    original = snapshot.response()
    validator("internal/coordination-transactions.schema.json", "ExecutionSnapshot").validate(
        original
    )
    assert "abort_recovery" not in parse("ExecutionStatusResponse", original["execution"])
    assert snapshot.status() == enhanced


class RecoveryLedger(InMemorySubmissionLedger):
    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
        intent = self.get(submit_intent_id)
        return (
            None
            if intent is None
            else ExecutionSnapshot(
                intent,
                AbortRecovery(
                    intent.request_digest,
                    "verified",
                    AbortReceipt("sha256:" + "b" * 64, NOW, "received", None),
                ),
            )
        )


@pytest.mark.parametrize("accept", ["application/json", "*/*", MEDIA + ";q=0"])
async def test_old_json_never_contains_new_recovery_fields(tmp_path: Path, accept: str) -> None:
    ledger = RecoveryLedger(now=lambda: NOW)
    pending(ledger)
    before = ledger.get("intent-1")
    app, _ = app_for(tmp_path, ledger=ledger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, headers={**HEADERS, "Accept": accept})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["vary"] == "Accept"
    assert "abort_recovery" not in response.json()
    parse("ExecutionStatusResponse", response.json())
    assert ledger.get("intent-1") == before


async def test_explicit_recovery_media_observes_receipt_and_preserves_auth(tmp_path: Path) -> None:
    ledger = RecoveryLedger(now=lambda: NOW)
    pending(ledger)
    before = ledger.get("intent-1")
    app, _ = app_for(tmp_path, ledger=ledger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, headers={**HEADERS, "Accept": MEDIA})
        replay = await client.get(PATH, headers={**HEADERS, "Accept": MEDIA})
        unauthorized = await client.get(PATH, headers={"Accept": MEDIA})
    assert response.status_code == replay.status_code == 200
    assert response.headers["content-type"] == MEDIA
    assert response.headers["vary"] == "Accept"
    assert response.json() == replay.json()
    parsed = parse("ExecutionRecoveryStatusResponse", response.json())
    receipt = parsed["abort_recovery"]["receipt"]
    assert receipt is not None and receipt["claim_state"] == "received"
    assert unauthorized.status_code == 401
    assert ledger.get("intent-1") == before


async def test_unobserved_recovery_is_unavailable_instead_of_no_receipt(tmp_path: Path) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    app, _ = app_for(tmp_path, ledger=ledger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, headers={**HEADERS, "Accept": MEDIA})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "control_store_unavailable"
    parse("ExecutionControlErrorResponse", response.json())
