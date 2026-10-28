"""The stop ledger records the evidence's own confirmations, not writer constants."""

import pytest

from speechnexus_gateway.domain.submission import (
    CancellationUnconfirmedError,
    ExecutionState,
    RecordStop,
    StopEvidenceKind,
    VerifiedStopEvidence,
)
from speechnexus_gateway.infrastructure.stop_sql import (
    STOP_EVIDENCE_INSERT_COLUMNS,
    STOP_EVIDENCE_INSERT_SQL,
    stop_evidence_values,
)

_DIGEST = "sha256:" + "a" * 64


def evidence(*, stopped: bool = True, released: bool = True) -> VerifiedStopEvidence:
    return VerifiedStopEvidence(
        evidence_id="stop-terminal-test",
        submit_intent_id="intent-a",
        evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
        evidence_digest=_DIGEST,
        observed_at_us=1_700_000_000_000_000,
        execution_stopped=stopped,
        resources_released=released,
        reason=None,
    )


def test_insert_binds_the_evidence_flags_instead_of_hardcoding_them() -> None:
    confirmed = dict(
        zip(STOP_EVIDENCE_INSERT_COLUMNS, stop_evidence_values(evidence()), strict=True)
    )
    partial = stop_evidence_values(evidence(released=False))

    assert confirmed["execution_stopped"] == 1
    assert confirmed["resources_released"] == 1
    assert partial[STOP_EVIDENCE_INSERT_COLUMNS.index("resources_released")] == 0
    assert STOP_EVIDENCE_INSERT_SQL.count("%s") == len(STOP_EVIDENCE_INSERT_COLUMNS)
    assert ", 1, 1," not in STOP_EVIDENCE_INSERT_SQL


@pytest.mark.parametrize("unconfirmed", [{"stopped": False}, {"released": False}])
def test_a_partial_stop_never_reaches_the_ledger(unconfirmed: dict[str, bool]) -> None:
    partial = evidence(**unconfirmed)

    with pytest.raises(CancellationUnconfirmedError):
        RecordStop(partial, ExecutionState.SUCCEEDED)
