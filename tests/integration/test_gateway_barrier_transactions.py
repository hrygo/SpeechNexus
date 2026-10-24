"""Named full result for the durable barrier fact on real PostgreSQL.

The unit suite covers the same boundary against an in-memory store. Here the barrier is
sealed through the restricted gateway login and a real process ingress, so these tests also
show the two facts the coordinator depends on: a repeated command returns the original
proof even after the owner that sealed it was replaced, and capacity is never released by
sealing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_barrier import takeover
from tests.integration.test_gateway_stop_evidence import counters, new_pool
from tests.integration.test_production_jobs import database as database
from tests.support.contracts import validator

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.fencing import BarrierRequest, IngressFence
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.interfaces.barrier_transactions import GatewayBarrierBoundary

pytestmark = [
    pytest.mark.integration,
    # The barrier table is read and written through the PostgreSQL adapter.
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

POOL = "tts-realtime"
GATEWAY = "gateway-test"
NODE = "host-test"


def authority(foundation: Foundation) -> tuple[str, int]:
    """The owner and token the pool row currently carries."""
    row = foundation.database.query(
        "SELECT owner_id, fencing_token FROM resource_pools WHERE pool_id = %s", (POOL,)
    )[0]
    return row["owner_id"], row["fencing_token"]


def seal_request(
    owner_id: str,
    token: int,
    *,
    observation_gateway_id: str = GATEWAY,
    observation_pool_id: str = POOL,
    enforcement_token: int | None = None,
    node_id: str = NODE,
) -> dict[str, Any]:
    fence = IngressFence(
        GATEWAY, node_id, BarrierRequest(POOL, owner_id, token, abort_in_flight=False)
    )
    return {
        "authority": {"pool_id": POOL, "owner_id": owner_id, "fencing_token": token},
        "barrier": {
            "gateway_id": observation_gateway_id,
            "pool_id": observation_pool_id,
            "enforced_min_fencing_token": token if enforcement_token is None else enforcement_token,
            "barrier_sealed": True,
            "submitting_intent_ids": [],
            "executing_intent_ids": [],
            "quarantined_intent_ids": [],
            "enforced_at": "2026-10-24T00:00:00Z",
        },
        "evidence_digest": fence.evidence_digest,
    }


def assert_frozen_result(request: dict[str, Any], result: dict[str, Any]) -> None:
    validator("internal/coordination-transactions.schema.json", "seal_barrier").validate(request)
    validator("internal/coordination-transactions.schema.json", "TransactionResult").validate(
        result
    )


@pytest.fixture
def barrier(
    foundation: Foundation, tmp_path: Path
) -> Iterator[tuple[GatewayBarrierBoundary, PostgresSubmissionLedger]]:
    pool: GatewayPool = new_pool(foundation)
    owner = ProcessIngressOwnership(tmp_path, node_id=NODE, gateway_id=GATEWAY)
    ingress = GatewayIngress(owner, timeout_seconds=0.1)
    ledger = PostgresSubmissionLedger(pool)
    try:
        yield GatewayBarrierBoundary(ingress=ingress, ledger=ledger), ledger
    finally:
        owner.close()
        pool.close()


def test_a_first_seal_is_committed_and_a_repeat_returns_the_original_proof(
    foundation: Foundation, barrier: tuple[GatewayBarrierBoundary, PostgresSubmissionLedger]
) -> None:
    boundary, ledger = barrier
    takeover(foundation, token=2)
    owner_id, token = authority(foundation)
    request = seal_request(owner_id, token)
    before = counters(foundation)

    first = boundary.seal_barrier(request)
    replay = boundary.seal_barrier(request)

    assert_frozen_result(request, first)
    assert first["outcome"] == "committed"
    assert first["scope_id"] == GATEWAY
    assert first["version"] == "1"
    assert first["value"] == {
        "kind": "fact",
        "fact_id": f"{GATEWAY}:{POOL}",
        "request_digest": request["evidence_digest"],
    }
    assert replay["outcome"] == "replayed"
    assert replay["value"] == first["value"]
    assert foundation.database.query(
        "SELECT gateway_id, pool_id, min_fencing_token, sealed_evidence_digest, version "
        "FROM gateway_fencing_barriers"
    ) == [
        {
            "gateway_id": GATEWAY,
            "pool_id": POOL,
            "min_fencing_token": token,
            "sealed_evidence_digest": request["evidence_digest"],
            "version": 1,
        }
    ]
    recorded = ledger.get_barrier(GATEWAY, POOL)
    assert recorded is not None and recorded.min_fencing_token == token
    # Sealing refuses future sends; it releases nothing and reconciles no budget by itself.
    assert counters(foundation) == before


def test_a_replay_survives_a_takeover_and_a_higher_minimum_writes_one_version(
    foundation: Foundation, barrier: tuple[GatewayBarrierBoundary, PostgresSubmissionLedger]
) -> None:
    boundary, ledger = barrier
    takeover(foundation, token=2)
    original = seal_request(*authority(foundation))
    assert boundary.seal_barrier(original)["outcome"] == "committed"

    # The owner that sealed the barrier is replaced; only the recorded fact can answer now.
    takeover(foundation, token=3)
    replay = boundary.seal_barrier(original)

    assert replay["outcome"] == "replayed"
    assert replay["version"] == "1"
    assert replay["value"]["request_digest"] == original["evidence_digest"]

    raised = boundary.seal_barrier(seal_request(*authority(foundation)))
    assert raised["outcome"] == "committed"
    assert raised["version"] == "2"
    stored = foundation.database.query(
        "SELECT min_fencing_token, sealed_evidence_digest, version FROM gateway_fencing_barriers"
    )
    assert stored == [
        {
            "min_fencing_token": 3,
            "sealed_evidence_digest": raised["value"]["request_digest"],
            "version": 2,
        }
    ]

    stale = boundary.seal_barrier(original)
    assert stale["outcome"] == "rejected"
    assert stale["error_code"] == "stale_fencing_token"
    recorded = ledger.get_barrier(GATEWAY, POOL)
    assert recorded is not None and recorded.min_fencing_token == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_gateway_id": "gateway-other"},
        {"observation_pool_id": "asr-realtime"},
        {"enforcement_token": 7},
        {"node_id": "host-other"},
    ],
)
def test_a_command_that_does_not_describe_this_gateway_seal_records_nothing(
    foundation: Foundation,
    barrier: tuple[GatewayBarrierBoundary, PostgresSubmissionLedger],
    changes: dict[str, Any],
) -> None:
    boundary, _ = barrier
    takeover(foundation, token=2)

    rejected = boundary.seal_barrier(seal_request(*authority(foundation), **changes))

    assert rejected["outcome"] == "rejected"
    assert rejected["error_code"] == "invalid_execution_request"
    assert foundation.database.query("SELECT version FROM gateway_fencing_barriers") == []
