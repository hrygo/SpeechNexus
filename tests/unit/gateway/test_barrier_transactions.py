"""Named full result for the durable barrier fact, without a database or a real seal."""

from __future__ import annotations

from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_gateway.domain.fencing import BarrierRequest, IngressFence
from speechnexus_gateway.interfaces.barrier_transactions import GatewayBarrierBoundary
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress

GATEWAY = "gateway-1"
NODE = "node-1"
POOL = "tts-realtime"
OTHER_PROOF = "sha256:" + "b" * 64


def seal_request(
    *,
    token: int = 1,
    owner_id: str = "scheduler-old",
    pool_id: str = POOL,
    gateway_id: str = GATEWAY,
    node_id: str = NODE,
    observation_gateway_id: str | None = None,
    observation_pool_id: str | None = None,
    enforcement_token: int | None = None,
    proof: str | None = None,
) -> dict[str, Any]:
    """One ``seal_barrier`` command as the gateway that sealed the pool would carry it."""
    fence = IngressFence(
        gateway_id, node_id, BarrierRequest(pool_id, owner_id, token, abort_in_flight=False)
    )
    return {
        "authority": {"pool_id": pool_id, "owner_id": owner_id, "fencing_token": token},
        "barrier": {
            "gateway_id": gateway_id if observation_gateway_id is None else observation_gateway_id,
            "pool_id": pool_id if observation_pool_id is None else observation_pool_id,
            "enforced_min_fencing_token": token if enforcement_token is None else enforcement_token,
            "barrier_sealed": True,
            "submitting_intent_ids": [],
            "executing_intent_ids": [],
            "quarantined_intent_ids": [],
            "enforced_at": "2026-10-24T00:00:00Z",
        },
        "evidence_digest": fence.evidence_digest if proof is None else proof,
    }


@pytest.fixture
def barrier() -> tuple[GatewayBarrierBoundary, InMemorySubmissionLedger]:
    ledger = InMemorySubmissionLedger()
    ingress = fake_ingress(node_id=NODE, gateway_id=GATEWAY)
    return GatewayBarrierBoundary(ingress=ingress, ledger=ledger), ledger


def test_a_first_seal_is_committed_and_a_repeat_returns_the_original_fact(
    barrier: tuple[GatewayBarrierBoundary, InMemorySubmissionLedger],
) -> None:
    boundary, ledger = barrier
    request = seal_request()

    first = boundary.seal_barrier(request)
    replay = boundary.seal_barrier(request)

    assert first["outcome"] == "committed"
    assert first["scope_id"] == GATEWAY
    assert first["recovery"] == "none"
    assert first["version"] == "1"
    assert first["value"] == {
        "kind": "fact",
        "fact_id": f"{GATEWAY}:{POOL}",
        "request_digest": request["evidence_digest"],
    }
    assert replay["outcome"] == "replayed"
    assert replay["version"] == "1"
    assert replay["value"] == first["value"]
    recorded = ledger.get_barrier(GATEWAY, POOL)
    assert recorded is not None and recorded.min_fencing_token == 1


def test_a_higher_minimum_writes_one_new_version(
    barrier: tuple[GatewayBarrierBoundary, InMemorySubmissionLedger],
) -> None:
    boundary, _ = barrier
    assert boundary.seal_barrier(seal_request())["outcome"] == "committed"

    raised = boundary.seal_barrier(seal_request(token=2, owner_id="scheduler-new"))

    assert raised["outcome"] == "committed"
    assert raised["version"] == "2"
    assert (
        raised["value"]["request_digest"]
        == seal_request(token=2, owner_id="scheduler-new")["evidence_digest"]
    )


def test_a_minimum_below_the_recorded_one_is_rejected(
    barrier: tuple[GatewayBarrierBoundary, InMemorySubmissionLedger],
) -> None:
    boundary, ledger = barrier
    assert boundary.seal_barrier(seal_request(token=2, owner_id="scheduler-new"))["outcome"] == (
        "committed"
    )

    stale = boundary.seal_barrier(seal_request())

    assert stale["outcome"] == "rejected"
    assert stale["error_code"] == "stale_fencing_token"
    recorded = ledger.get_barrier(GATEWAY, POOL)
    assert recorded is not None and recorded.min_fencing_token == 2 and recorded.version == 1


def test_one_minimum_cannot_carry_two_proofs(
    barrier: tuple[GatewayBarrierBoundary, InMemorySubmissionLedger],
) -> None:
    """A barrier sealed from another host keeps its proof; this host cannot add a second one."""
    boundary, ledger = barrier
    moved = GatewayBarrierBoundary(
        ingress=fake_ingress(node_id="node-other", gateway_id=GATEWAY), ledger=ledger
    )
    assert moved.seal_barrier(seal_request(node_id="node-other"))["outcome"] == "committed"
    recorded = ledger.get_barrier(GATEWAY, POOL)
    assert recorded is not None
    assert recorded.evidence_digest != seal_request()["evidence_digest"]

    conflict = boundary.seal_barrier(seal_request())

    assert conflict["outcome"] == "rejected"
    assert conflict["error_code"] == "idempotency_conflict"
    assert conflict["recovery"] == "query_current_authority"
    unchanged = ledger.get_barrier(GATEWAY, POOL)
    assert unchanged is not None
    assert (unchanged.min_fencing_token, unchanged.evidence_digest, unchanged.version) == (
        recorded.min_fencing_token,
        recorded.evidence_digest,
        recorded.version,
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"observation_gateway_id": "gateway-other"}, "another gateway"),
        ({"observation_pool_id": "asr-realtime"}, "another pool"),
        ({"enforcement_token": 7}, "another minimum"),
    ],
)
def test_an_observation_that_describes_another_seal_is_rejected(
    barrier: tuple[GatewayBarrierBoundary, InMemorySubmissionLedger],
    changes: dict[str, Any],
    reason: str,
) -> None:
    boundary, ledger = barrier

    rejected = boundary.seal_barrier(seal_request(**changes))

    assert rejected["outcome"] == "rejected", reason
    assert rejected["error_code"] == "invalid_execution_request"
    assert ledger.get_barrier(GATEWAY, POOL) is None


def test_a_proof_that_is_not_this_gateway_seal_is_rejected(
    barrier: tuple[GatewayBarrierBoundary, InMemorySubmissionLedger],
) -> None:
    boundary, ledger = barrier

    rejected = boundary.seal_barrier(seal_request(proof=OTHER_PROOF))

    assert rejected["outcome"] == "rejected"
    assert rejected["error_code"] == "invalid_execution_request"
    assert ledger.get_barrier(GATEWAY, POOL) is None


def test_an_unknown_field_is_refused_by_the_frozen_command_schema(
    barrier: tuple[GatewayBarrierBoundary, InMemorySubmissionLedger],
) -> None:
    boundary, ledger = barrier

    with pytest.raises(ContractValidationError):
        boundary.seal_barrier(seal_request() | {"abort_in_flight": True})

    assert ledger.get_barrier(GATEWAY, POOL) is None
