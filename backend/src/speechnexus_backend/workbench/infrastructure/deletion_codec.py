"""Map owned deletion objects to the frozen shared contract; no hand-written DTO type.

Only fixed ASCII property names, strings, booleans, lists and objects are emitted.
There are no JSON numbers and no dynamic property names: compact UTF-8 stdlib JSON
therefore has RFC 8785's representation for this closed projection. The contract
test compares against an independent RFC 8785 implementation and source schemas.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from speechnexus_backend.workbench.domain.deletion import (
    DeletionPlan,
    DeletionRecord,
    DeletionTarget,
)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def target_payload(target: DeletionTarget) -> dict[str, object]:
    return {
        "owner": target.owner,
        "kind": target.kind,
        "resource": {"id": target.id, "revision": str(target.revision)},
    }


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _unsigned_plan(plan: DeletionPlan) -> dict[str, object]:
    return {
        "id": plan.id,
        "object": "nexus.deletion_plan",
        "target": target_payload(plan.target),
        "mode": plan.mode,
        "plan_revision": str(plan.revision),
        "created_at": _time(plan.created_at),
        "expires_at": _time(plan.expires_at),
        "dependencies": [
            {
                "target": target_payload(item.target),
                "relation": item.relation,
                "disposition": item.disposition,
            }
            for item in plan.dependencies
        ],
        "eligible": plan.eligible,
    }


def plan_digest(plan: DeletionPlan) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(_unsigned_plan(plan))).hexdigest()


def plan_payload(plan: DeletionPlan) -> dict[str, object]:
    return {**_unsigned_plan(plan), "plan_digest": plan_digest(plan)}


def accepted_payload(record: DeletionRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "object": "nexus.deletion",
        "plan_id": record.plan_id,
        "plan_digest": record.plan_digest,
        "status": "queued",
        "cleanup_state": "pending",
        "created_at": _time(record.created_at),
        "updated_at": _time(record.created_at),
        "result": None,
        "error": None,
    }


def deletion_payload(record: DeletionRecord) -> dict[str, object]:
    payload = accepted_payload(record)
    result = record.result
    if result is not None:
        payload.update(
            status="succeeded",
            cleanup_state="complete",
            updated_at=_time(result.completed_at),
            result={
                "deleted": [target_payload(target) for target in result.deleted],
                "retained_independent_copies": [
                    target_payload(target) for target in result.retained_independent_copies
                ],
                "ledger_id": result.ledger_id,
                "completed_at": _time(result.completed_at),
            },
        )
    return payload
