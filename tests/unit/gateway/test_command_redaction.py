"""Deletion permits cannot erase active commands, release capacity or rewrite evidence."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.redact_execution_command import (
    ExecutionCommandRedactionService,
)
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    CommandDeletionTombstone,
    CommandRedactionConflictError,
    RedactExecutionCommand,
)
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    LeaseState,
    RecordStop,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.interfaces.command_redaction_transactions import (
    ExecutionCommandRedactionBoundary,
)
from tests.fake.command_deletion import SyntheticCommandDeletionAuthorizer
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine, fake_ingress
from tests.unit.gateway.test_execution_recovery import pending
from tests.unit.gateway.test_submit_execution import COMMAND, NOW, stop_evidence

DIGEST = "sha256:" + "d" * 64
LATER = NOW + timedelta(seconds=5)


def closed(ledger: InMemorySubmissionLedger, *, unsent: bool = False) -> SubmissionIntent:
    intent = pending(ledger)
    if unsent:
        intent.mark_not_dispatched_failure(ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=NOW)
        return ledger.record_execution_observation(
            RecordExecutionObservation.from_intent(intent, expected_version=1)
        )
    intent.mark_dispatched("engine-1", at=NOW)
    ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )
    return ledger.record_stop(RecordStop(stop_evidence(), ExecutionState.ABORTED))


def permit_for(intent: SubmissionIntent, **changes: Any) -> AuthorizedCommandRedaction:
    return AuthorizedCommandRedaction(
        replace(
            RedactExecutionCommand(
                intent.identity.submit_intent_id, intent.version, "deletion-1", DIGEST
            ),
            **changes,
        ),
        intent.identity,
    )


def boundary_for(
    ledger: InMemorySubmissionLedger, permit: AuthorizedCommandRedaction
) -> ExecutionCommandRedactionBoundary:
    return ExecutionCommandRedactionBoundary(
        ExecutionCommandRedactionService(
            ingress=fake_ingress(),
            ledger=ledger,
            authorizer=SyntheticCommandDeletionAuthorizer(permit),
        )
    )


def wire(permit: AuthorizedCommandRedaction) -> dict[str, Any]:
    request = permit.request
    return {
        "submit_intent_id": request.submit_intent_id,
        "expected_version": str(request.expected_version),
        "deletion_job_id": request.deletion_job_id,
        "deletion_evidence_digest": request.deletion_evidence_digest,
    }


@pytest.mark.parametrize("unsent", [False, True])
def test_redaction_retains_original_execution_and_never_authorizes_dispatch(unsent: bool) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger, unsent=unsent)
    ledger.set_test_lease_state(original.identity.lease_id, LeaseState.RELEASED)
    permit = permit_for(original)
    first = ledger.redact_execution_command(permit)
    second = ledger.redact_execution_command(permit)
    assert first.replayed is False and second.replayed is True
    assert first.intent == second.intent == replace(original, version=original.version + 1)
    assert first.tombstone == second.tombstone
    assert ledger._request_documents[original.identity.submit_intent_id] is None
    assert first.intent.stop_evidence_digest == original.stop_evidence_digest
    engine = ScriptedEngine()
    outcome = SubmitExecutionService(
        ingress=fake_ingress(), ledger=ledger, dispatch=engine, observation=engine
    ).submit(SubmitRequest(original.identity, COMMAND))
    assert outcome.idempotent_replay and engine.dispatch_count == 0
    assert outcome.intent.version == original.version + 1


@pytest.mark.parametrize("state", list(LeaseState))
def test_only_a_released_lease_allows_command_cleanup(state: LeaseState) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    ledger.set_test_lease_state(original.identity.lease_id, state)
    if state is LeaseState.RELEASED:
        assert not ledger.redact_execution_command(permit_for(original)).replayed
    else:
        with pytest.raises(ExecutionCleanupPendingError):
            ledger.redact_execution_command(permit_for(original))
        assert ledger.get("intent-1") == original
        assert ledger._request_documents["intent-1"] is not None


def test_released_lease_is_not_execution_cleanup_evidence() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(ledger)
    ledger.set_test_lease_state(original.identity.lease_id, LeaseState.RELEASED)
    with pytest.raises(ExecutionCleanupPendingError):
        ledger.redact_execution_command(permit_for(original))
    assert ledger.get("intent-1") == original


def test_version_conflict_and_another_tombstone_cannot_erase_or_rewrite() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    ledger.set_test_lease_state(original.identity.lease_id, LeaseState.RELEASED)
    with pytest.raises(StaleSubmissionVersionError):
        ledger.redact_execution_command(permit_for(original, expected_version=1))
    written = ledger.redact_execution_command(permit_for(original))
    for changed in (
        {"deletion_job_id": "deletion-other"},
        {"deletion_evidence_digest": "sha256:" + "a" * 64},
    ):
        with pytest.raises(CommandRedactionConflictError):
            ledger.redact_execution_command(permit_for(original, **changed))
    assert ledger.get("intent-1") == written.intent


def test_permit_must_match_the_entire_frozen_identity() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    permit = permit_for(original)
    ledger.set_test_lease_state(original.identity.lease_id, LeaseState.RELEASED)
    wrong = replace(permit, identity=replace(permit.identity, instance_generation=2))
    with pytest.raises(InvalidExecutionRequestError):
        ledger.redact_execution_command(wrong)
    assert ledger.get("intent-1") == original


def test_schema_valid_job_and_digest_do_not_authorize_a_deletion() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    permit = permit_for(original)
    result = boundary_for(ledger, permit).redact_execution_command(
        wire(permit) | {"deletion_job_id": "deletion-unapproved"}
    )
    assert result["error_code"] == "component_credential_invalid"
    assert result["outcome"] == "rejected" and result["recovery"] == "none"
    assert ledger._request_documents["intent-1"] is not None


def test_boundary_replays_the_complete_snapshot_and_preserves_wire_types() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    ledger.set_test_lease_state(original.identity.lease_id, LeaseState.RELEASED)
    permit = permit_for(original)
    boundary = boundary_for(ledger, permit)
    value = wire(permit)
    result = boundary.redact_execution_command(value)
    assert result["outcome"] == "committed" and result["version"] == str(original.version + 1)
    assert result["value"] == {
        "kind": "fact",
        "fact_id": "intent-1",
        "request_digest": original.request_digest,
    }
    validate_coordination("TransactionResult", result)
    assert boundary.redact_execution_command(value)["outcome"] == "replayed"
    assert value == wire(permit)
    with pytest.raises(ContractValidationError):
        boundary.redact_execution_command(value | {"expected_version": original.version})


def test_lost_commit_acknowledgment_requires_original_identity_recovery() -> None:
    class LostLedger(InMemorySubmissionLedger):
        def redact_execution_command(self, permit: AuthorizedCommandRedaction) -> Any:
            raise SubmissionOutcomeUnknownError

    ledger = LostLedger(now=lambda: NOW)
    original = closed(ledger)
    result = boundary_for(ledger, permit_for(original)).redact_execution_command(
        wire(permit_for(original))
    )
    assert result["outcome"] == "unknown" and result["recovery"] == "query_original"
    assert "value" not in result


def test_authorizer_cannot_change_the_request_or_route_to_a_foreign_gateway() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    permit = permit_for(original)

    class WrongAuthorizer:
        def authorize(self, request: RedactExecutionCommand) -> AuthorizedCommandRedaction:
            return replace(
                permit, request=replace(request, expected_version=request.expected_version + 1)
            )

    service = ExecutionCommandRedactionService(
        ingress=fake_ingress(), ledger=ledger, authorizer=WrongAuthorizer()
    )
    with pytest.raises(InvalidExecutionRequestError):
        service.redact_execution_command(permit.request)
    foreign = replace(permit, identity=replace(permit.identity, gateway_id="gateway-other"))
    result = boundary_for(ledger, foreign).redact_execution_command(wire(foreign))
    assert result["error_code"] == "invalid_execution_request"
    assert ledger._request_documents["intent-1"] is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"submit_intent_id": "BAD ID"},
        {"deletion_job_id": "BAD ID"},
        {"deletion_evidence_digest": "wrong"},
        {"expected_version": True},
        {"expected_version": 0},
        {"expected_version": 2**63},
    ],
)
def test_deletion_request_has_strict_scalar_domains(changes: dict[str, Any]) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        replace(RedactExecutionCommand("intent-1", 1, "deletion-1", DIGEST), **changes)


@pytest.mark.parametrize(
    "present,previous",
    [
        (False, None),
        (True, CommandDeletionTombstone("deletion-1", DIGEST, NOW)),
    ],
)
def test_partial_redaction_facts_fail_closed(
    present: bool, previous: CommandDeletionTombstone | None
) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    with pytest.raises(SubmissionStoreError):
        permit_for(original).apply(
            original,
            lease_state=LeaseState.RELEASED,
            document_present=present,
            previous=previous,
            at=LATER,
        )


def test_redaction_cannot_reverse_time_or_overflow_the_version() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = closed(ledger)
    for current, at in [
        (replace(original, updated_at=LATER), NOW),
        (replace(original, version=2**63 - 1), LATER),
    ]:
        with pytest.raises(SubmissionStoreError):
            permit_for(current).apply(
                current,
                lease_state=LeaseState.RELEASED,
                document_present=True,
                previous=None,
                at=at,
            )
