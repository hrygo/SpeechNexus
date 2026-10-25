"""Workbench deletion projection consumes the frozen shared schema unchanged."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import rfc8785

from speechnexus_backend.workbench.domain.deletion import DeletionRecord, DeletionResult
from speechnexus_backend.workbench.infrastructure.deletion_codec import (
    accepted_payload,
    deletion_payload,
    plan_digest,
    plan_payload,
)
from tests.contract.test_architecture_repairs import _deletion_validator
from tests.unit.backend.test_workbench_deletion_rules import plan


def test_full_plan_digest_matches_rfc8785_and_authoritative_schema() -> None:
    candidate = plan()
    payload = plan_payload(candidate)
    _deletion_validator("DeletionPlan").validate(payload)
    digest = payload.pop("plan_digest")
    wire_value = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    assert digest == "sha256:" + hashlib.sha256(rfc8785.dumps(wire_value)).hexdigest()
    assert digest == plan_digest(candidate)


def test_current_completion_and_original_acceptance_are_distinct_contract_projections() -> None:
    candidate = plan()
    accepted = DeletionRecord(
        "deletion-synthetic", candidate.id, plan_digest(candidate), candidate.created_at
    )
    result = DeletionResult(
        candidate.confirmed_targets, (), "ledger-synthetic", candidate.created_at
    )
    current = replace(accepted, result=result)
    _deletion_validator("Deletion").validate(accepted_payload(current))
    _deletion_validator("Deletion").validate(deletion_payload(current))
    assert accepted_payload(current)["status"] == "queued"
    assert deletion_payload(current)["status"] == "succeeded"
    assert deletion_payload(accepted)["result"] is None
