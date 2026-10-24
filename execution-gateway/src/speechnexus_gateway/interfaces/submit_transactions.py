"""Full named coordination results for the submit-intent lifecycle.

``create_submit``, ``record_stop`` and ``mark_execution_unknown`` are the three transitions
of one durable execution fact. A trusted process-local caller gets the frozen
``TransactionResult`` vocabulary here — outcome, stable error code, recovery hint and the
recorded snapshot — instead of domain exceptions, and an identical repeat is reported as
``replayed`` rather than as a second write.

These are native trusted boundaries: they open no HTTP route and grant no permission to
dispatch. Both dependencies must be wired to the same durable execution store, because a
replay decision compares the fact this call replaced with the fact it returned.
"""

from __future__ import annotations

from typing import Any

from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.ports import ExecutionFactLedger
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StopEvidenceKind,
    SubmissionIntent,
    VerifiedStopEvidence,
)
from speechnexus_gateway.interfaces.execution_payloads import submit_request_from_payload
from speechnexus_gateway.interfaces.execution_transactions import execution_transaction_result


def _stop_evidence(value: Any) -> VerifiedStopEvidence:
    return VerifiedStopEvidence(
        evidence_id=value["evidence_id"],
        submit_intent_id=value["submit_intent_id"],
        evidence_kind=StopEvidenceKind(value["evidence_kind"]),
        evidence_digest=value["evidence_digest"],
        observed_at_us=value["observed_at_us"],
        execution_stopped=value["execution_stopped"],
        resources_released=value["resources_released"],
        reason=value.get("reason"),
    )


def _replayed(before: SubmissionIntent | None, saved: SubmissionIntent) -> bool:
    """Whether an identical transition returned the fact that was already recorded.

    The durable transaction only advances the version when it writes a new fact and returns
    the recorded original on an exact repeat, so an unchanged version is that original.
    """
    return before is not None and saved.version == before.version


class ExecutionFactBoundary:
    def __init__(self, *, submit: SubmitExecutionService, ledger: ExecutionFactLedger) -> None:
        self._submit = submit
        self._ledger = ledger

    def create_submit(self, request: Any) -> dict[str, Any]:
        validate_coordination("create_submit", request)
        execution_request = request["execution_request"]

        def submit() -> tuple[bool, int | None, dict[str, object]]:
            try:
                command = submit_request_from_payload(
                    execution_request, output_stream_id=request["output_stream_id"]
                )
            except KeyError, TypeError:
                raise InvalidExecutionRequestError("incomplete execution request") from None
            outcome = self._submit.submit(command)
            return (
                outcome.idempotent_replay,
                outcome.intent.version,
                ExecutionSnapshot(outcome.intent).response(),
            )

        return execution_transaction_result(
            "create_submit", execution_request["submit_intent_id"], submit
        )

    def record_stop(self, request: Any) -> dict[str, Any]:
        validate_coordination("record_stop", request)
        raw = request["evidence"]

        def stop() -> tuple[bool, int | None, dict[str, object]]:
            try:
                evidence = _stop_evidence(raw)
                terminal_state = ExecutionState(request["terminal_state"])
                error_code = (
                    None
                    if request["error_code"] is None
                    else ExecutionErrorCode(request["error_code"])
                )
            except KeyError, TypeError, ValueError:
                raise InvalidExecutionRequestError("incomplete stop request") from None
            before = self._ledger.get(evidence.submit_intent_id)
            saved = self._submit.record_stop(
                evidence, terminal_state=terminal_state, error_code=error_code
            )
            return _replayed(before, saved), saved.version, ExecutionSnapshot(saved).response()

        return execution_transaction_result("record_stop", raw["submit_intent_id"], stop)

    def mark_execution_unknown(self, request: Any) -> dict[str, Any]:
        validate_coordination("mark_execution_unknown", request)
        submit_intent_id = request["submit_intent_id"]

        def mark() -> tuple[bool, int | None, dict[str, object]]:
            try:
                command = MarkExecutionUnknown(
                    submit_intent_id,
                    ExecutionErrorCode(request["reason_code"]),
                    request["observed_at_us"],
                )
            except KeyError, TypeError, ValueError:
                raise InvalidExecutionRequestError("incomplete unknown request") from None
            before = self._ledger.get(submit_intent_id)
            saved = self._ledger.mark_execution_unknown(command)
            return _replayed(before, saved), saved.version, ExecutionSnapshot(saved).response()

        return execution_transaction_result("mark_execution_unknown", submit_intent_id, mark)
