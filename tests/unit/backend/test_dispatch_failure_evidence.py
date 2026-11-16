"""Which gateway status shapes may settle a public Job as failed.

A Job failure returns capacity, so the projection only accepts the two shapes the
coordination contract can prove: a verified stop with complete cleanup, or a submit the
durable fence refused before any engine request existed. Everything else - including an
``aborted`` projection whose only evidence is ``unconfirmed_timeout`` - is reported as
unstoppable work instead of a settled failure.
"""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.domain.job_dispatch import (
    VERIFIED_STOP_EVIDENCE_KINDS,
    verified_failure_evidence,
)

DISPATCHED = "2026-11-17T11:00:00Z"


def status(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "submit_intent_id": "submit-" + "a" * 32,
        "execution_state": "failed",
        "cleanup_state": "complete",
        "stop_evidence_kind": "instance_terminated_by_controller",
        "dispatched_at": DISPATCHED,
        "engine_request_id": "engine-request-1",
        "error_code": "voice_generation_timeout",
    }
    document.update(changes)
    return document


@pytest.mark.parametrize("kind", sorted(VERIFIED_STOP_EVIDENCE_KINDS))
def test_a_verified_stop_with_complete_cleanup_settles_the_job(kind: str) -> None:
    assert verified_failure_evidence(status(stop_evidence_kind=kind)) == kind


@pytest.mark.parametrize("kind", ["none", "unconfirmed_timeout"])
def test_nothing_proved_never_settles_a_dispatched_failure(kind: str) -> None:
    assert verified_failure_evidence(status(stop_evidence_kind=kind)) is None


def test_unconfirmed_cleanup_never_settles_a_failure() -> None:
    assert verified_failure_evidence(status(cleanup_state="pending")) is None
    assert verified_failure_evidence(status(cleanup_state="quarantined")) is None


def test_an_aborted_execution_without_a_dispatched_identity_needs_instance_evidence() -> None:
    """The contract's only aborted-without-dispatch shape carries whole-instance proof."""
    accepted = status(
        execution_state="aborted",
        dispatched_at=None,
        engine_request_id=None,
        stop_evidence_kind="instance_terminated_by_controller",
    )
    refused = status(
        execution_state="aborted",
        dispatched_at=None,
        engine_request_id=None,
        stop_evidence_kind="unconfirmed_timeout",
    )

    assert verified_failure_evidence(accepted) == "instance_terminated_by_controller"
    assert verified_failure_evidence(refused) is None


def test_a_submit_refused_before_any_engine_request_is_not_a_stop() -> None:
    refused = status(
        cleanup_state="not_required",
        stop_evidence_kind="none",
        dispatched_at=None,
        engine_request_id=None,
        error_code="gateway_barrier_fenced",
    )
    dispatched = status(cleanup_state="not_required", stop_evidence_kind="none")

    assert verified_failure_evidence(refused) == "none"
    assert verified_failure_evidence(dispatched) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_evidence_kind": None},
        {"stop_evidence_kind": "engine_sleep_ack"},
        {"cleanup_state": None},
    ],
)
def test_a_status_outside_the_frozen_evidence_vocabulary_is_not_proof(
    changes: dict[str, Any],
) -> None:
    assert verified_failure_evidence(status(**changes)) is None


def test_the_evidence_vocabulary_has_no_sleep_member() -> None:
    """An engine sleep acknowledgment is not a stop the ledger can record."""
    assert "engine_sleep_ack" not in VERIFIED_STOP_EVIDENCE_KINDS
