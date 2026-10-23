"""Effective-request identity, output ceiling and authority verification rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionRejectedError,
    GatewayBarrierFencedError,
    InstanceGenerationMismatchError,
    InvalidExecutionRequestError,
    LeaseFacts,
    LeaseNotActiveError,
    LeaseState,
    StaleFencingTokenError,
    SubmissionIdentity,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmitIntentConflictError,
    SubmitRequest,
    contract_instant_text,
    hard_output_ceiling_samples,
    max_output_audio_ms_for,
    verify_submit_identity,
)
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 24, tzinfo=UTC)
COMMAND: JsonValue = {"schema_version": "1", "task": "tts.synthesize", "input": {"text": "hi"}}


def identity_for(**overrides: object) -> SubmissionIdentity:
    fields: dict[str, object] = {
        "submit_intent_id": "intent-1",
        "lease_id": "lease-1",
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "stage_id": "stage-1",
        "workspace_id": "ws-1",
        "pool_id": "tts-realtime",
        "host_node_id": "node-1",
        "gateway_id": "gateway-1",
        "instance_id": "instance-1",
        "instance_generation": 3,
        "binding_revision_id": "binding-rev-1",
        "policy_revision_id": "policy-rev-1",
        "voice_revision_id": "voice-rev-1",
        "fencing_token": 7,
        "command_digest": command_digest(COMMAND),
        "deadline_at": NOW + timedelta(minutes=5),
        "max_output_audio_ms": 2000,
        "sample_rate_hz": 24000,
    }
    fields.update(overrides)
    return SubmissionIdentity(**fields)  # type: ignore[arg-type]


def facts_for(identity: SubmissionIdentity, **overrides: object) -> LeaseFacts:
    fields: dict[str, object] = {
        "lease_id": identity.lease_id,
        "operation_id": identity.operation_id,
        "attempt_id": identity.attempt_id,
        "stage_id": identity.stage_id,
        "workspace_id": identity.workspace_id,
        "pool_id": identity.pool_id,
        "host_node_id": identity.host_node_id,
        "gateway_id": identity.gateway_id,
        "instance_id": identity.instance_id,
        "instance_generation": identity.instance_generation,
        "fencing_token": identity.fencing_token,
        "lease_state": LeaseState.RESERVED,
        "binding_revision_id": identity.binding_revision_id,
        "policy_revision_id": identity.policy_revision_id,
    }
    fields.update(overrides)
    return LeaseFacts(**fields)  # type: ignore[arg-type]


def request_for(identity: SubmissionIdentity, **overrides: object) -> SubmitRequest:
    fields: dict[str, object] = {
        "identity": identity,
        "command": COMMAND,
        "output_stream_id": "tts-main",
    }
    fields.update(overrides)
    return SubmitRequest(**fields)  # type: ignore[arg-type]


def build_service() -> SubmitExecutionService:
    engine = ScriptedEngine()
    return SubmitExecutionService(
        ledger=InMemorySubmissionLedger(),
        dispatch=engine,
        observation=engine,
        now=lambda: NOW,
    )


def test_effective_request_document_matches_the_recorded_digest() -> None:
    request = request_for(identity_for())
    document = json.loads(request.request_document)

    assert document["output_stream_id"] == "tts-main"
    assert document["execution_request"]["command"] == COMMAND
    assert document["execution_request"]["deadline_at"] == "2026-10-24T00:05:00.000000Z"
    assert command_digest(document) == request.request_digest
    assert request.request_digest != request.identity.command_digest


def test_effective_request_digest_covers_fields_outside_the_command() -> None:
    first = request_for(identity_for())
    later_deadline = request_for(identity_for(deadline_at=NOW + timedelta(minutes=6)))
    other_stream = request_for(identity_for(), output_stream_id="tts-other")

    assert first.identity.command_digest == later_deadline.identity.command_digest
    assert first.request_digest != later_deadline.request_digest
    assert first.request_digest != other_stream.request_digest


def test_replay_with_the_same_command_but_another_identity_is_a_conflict() -> None:
    service = build_service()
    service.submit(request_for(identity_for()))

    with pytest.raises(SubmitIntentConflictError):
        service.submit(request_for(identity_for(fencing_token=8)))

    stored = service.query("intent-1")
    assert stored is not None
    assert stored.identity.fencing_token == 7


def test_a_pcm_stage_requires_a_stream_and_a_non_pcm_stage_forbids_one() -> None:
    silent = {"max_output_audio_ms": None, "sample_rate_hz": None}

    with pytest.raises(ValueError, match="output_stream_id"):
        request_for(identity_for(**silent))
    with pytest.raises(ValueError, match="output_stream_id"):
        request_for(identity_for(**silent), output_stream_id="tts-main")
    assert request_for(identity_for(**silent), output_stream_id=None).output_stream_id is None


@pytest.mark.parametrize(
    ("sample_rate_hz", "max_output_audio_ms", "ceiling"),
    [
        (16000, 100, 1600),
        (24000, 2000, 48000),
        (48000, 333, 15984),
    ],
)
def test_output_ceiling_is_the_smallest_whole_sample_bound(
    sample_rate_hz: int, max_output_audio_ms: int, ceiling: int
) -> None:
    assert (
        hard_output_ceiling_samples(
            max_output_audio_ms=max_output_audio_ms, sample_rate_hz=sample_rate_hz
        )
        == ceiling
    )
    assert (
        max_output_audio_ms_for(ceiling_samples=ceiling, sample_rate_hz=sample_rate_hz)
        == max_output_audio_ms
    )


def test_a_stage_without_audio_has_no_ceiling_or_stream() -> None:
    assert hard_output_ceiling_samples(max_output_audio_ms=None, sample_rate_hz=None) is None
    with pytest.raises(ValueError, match="sample_rate_hz"):
        hard_output_ceiling_samples(max_output_audio_ms=2000, sample_rate_hz=None)


def test_a_stored_ceiling_that_is_not_whole_milliseconds_is_reported() -> None:
    with pytest.raises(ValueError, match="whole-millisecond"):
        max_output_audio_ms_for(ceiling_samples=1, sample_rate_hz=24000)


def test_identity_rejects_an_output_budget_below_the_contract_minimum() -> None:
    with pytest.raises(ValueError, match="at least 100"):
        identity_for(max_output_audio_ms=99)


def test_a_frozen_ceiling_never_overflows_a_signed_sample_count() -> None:
    with pytest.raises(ValueError, match="overflows"):
        hard_output_ceiling_samples(max_output_audio_ms=2**53 - 1, sample_rate_hz=48000)


def test_contract_instants_are_utc_microseconds_and_reject_naive_values() -> None:
    elsewhere = timezone(timedelta(hours=2))

    assert (
        contract_instant_text(datetime(2026, 10, 24, 4, 5, tzinfo=elsewhere))
        == "2026-10-24T02:05:00.000000Z"
    )
    with pytest.raises(ValueError, match="timezone aware"):
        contract_instant_text(datetime(2026, 10, 24, 2, 5))


def test_a_matching_claim_and_authority_are_accepted() -> None:
    identity = identity_for()

    verify_submit_identity(identity, facts_for(identity), sealed_min_fencing_token=None)
    verify_submit_identity(identity, facts_for(identity), sealed_min_fencing_token=7)


@pytest.mark.parametrize(
    "field",
    [
        "lease_id",
        "operation_id",
        "attempt_id",
        "stage_id",
        "workspace_id",
        "pool_id",
        "host_node_id",
        "gateway_id",
        "instance_id",
        "binding_revision_id",
        "policy_revision_id",
    ],
)
def test_any_identity_field_that_disagrees_with_the_lease_is_invalid(field: str) -> None:
    identity = identity_for()

    with pytest.raises(InvalidExecutionRequestError, match=field):
        verify_submit_identity(
            identity, facts_for(identity, **{field: "other"}), sealed_min_fencing_token=None
        )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"instance_generation": 4}, InstanceGenerationMismatchError),
        ({"fencing_token": 8}, StaleFencingTokenError),
        ({"lease_state": LeaseState.RELEASED}, LeaseNotActiveError),
        ({"lease_state": LeaseState.QUARANTINED}, LeaseNotActiveError),
        ({"lease_state": LeaseState.STOPPING}, LeaseNotActiveError),
    ],
)
def test_lease_disagreements_map_to_their_stable_error_code(
    overrides: dict[str, object], expected: type[ExecutionRejectedError]
) -> None:
    identity = identity_for()

    with pytest.raises(expected) as raised:
        verify_submit_identity(
            identity, facts_for(identity, **overrides), sealed_min_fencing_token=None
        )

    assert raised.value.error_code is expected.error_code


@pytest.mark.parametrize("lease_state", [LeaseState.RESERVED, LeaseState.EXECUTING])
def test_an_unstopped_lease_accepts_a_first_submit(lease_state: LeaseState) -> None:
    identity = identity_for()

    verify_submit_identity(
        identity, facts_for(identity, lease_state=lease_state), sealed_min_fencing_token=7
    )


def test_a_sealed_barrier_rejects_a_lower_fencing_token() -> None:
    identity = identity_for()

    with pytest.raises(GatewayBarrierFencedError) as raised:
        verify_submit_identity(identity, facts_for(identity), sealed_min_fencing_token=8)

    assert raised.value.error_code is ExecutionErrorCode.GATEWAY_BARRIER_FENCED


def _subclasses(parent: type[Any]) -> set[type[Any]]:
    found: set[type[Any]] = set()
    for child in parent.__subclasses__():
        found.add(child)
        found |= _subclasses(child)
    return found


def test_every_rejection_uses_a_execution_contract_error_code() -> None:
    execution = yaml.safe_load(
        (ROOT / "contracts/internal/execution-control.openapi.yaml").read_text()
    )
    contract_codes = set(execution["components"]["schemas"]["ExecutionErrorCode"]["enum"])
    gateway_codes = {code.value for code in ExecutionErrorCode}

    assert gateway_codes <= contract_codes
    rejections = _subclasses(ExecutionRejectedError) | {InvalidExecutionRequestError}
    assert rejections
    for rejection in rejections:
        assert rejection.error_code.value in contract_codes


def test_every_store_failure_uses_a_contract_outcome_and_recovery() -> None:
    coordination = json.loads(
        (ROOT / "contracts/internal/coordination-transactions.schema.json").read_text()
    )
    definitions = coordination["$defs"]
    error_codes = set(definitions["TransactionErrorCode"]["anyOf"][0]["enum"])
    recoveries = set(definitions["TransactionResult"]["properties"]["recovery"]["enum"])

    for failure in _subclasses(SubmissionStoreError) | {SubmissionStoreError}:
        assert failure.code in error_codes
        assert failure.recovery in recoveries
    assert SubmissionOutcomeUnknownError.code == "outcome_unknown"
    assert SubmissionOutcomeUnknownError.recovery in {
        "query_original",
        "query_current_authority",
        "reconcile",
    }
