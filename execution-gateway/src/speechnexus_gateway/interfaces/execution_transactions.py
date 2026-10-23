"""Common full coordination result mapping for native execution transaction boundaries."""

from collections.abc import Callable
from typing import Any

from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionRejectedError,
    StaleSubmissionVersionError,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
)


def execution_transaction_result(
    operation: str, scope_id: str, work: Callable[[], tuple[bool, int, dict[str, object]]]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "operation": operation,
        "scope_id": scope_id,
        "recovery": "none",
    }
    try:
        replayed, version, value = work()
        result.update(
            outcome="replayed" if replayed else "committed",
            version=str(version),
            value=value,
        )
    except SubmissionOutcomeUnknownError:
        result.update(outcome="unknown", error_code="outcome_unknown", recovery="query_original")
    except SubmissionStoreError as error:
        result.update(
            outcome="conflict" if isinstance(error, StaleSubmissionVersionError) else "rejected",
            error_code=error.code,
            recovery=error.recovery,
        )
    except ExecutionRejectedError as error:
        result.update(
            outcome="rejected",
            error_code=error.error_code.value,
            recovery=(
                "reconcile"
                if error.error_code is ExecutionErrorCode.EXECUTION_CLEANUP_PENDING
                else "none"
            ),
        )
    validate_coordination("TransactionResult", result)
    return result
