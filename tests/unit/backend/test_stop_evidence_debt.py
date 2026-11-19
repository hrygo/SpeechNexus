"""The dispatch gate's hold is visible, bounded and never takes the API out of service.

The gate refuses to project a failure the gateway cannot back, which is the right
direction and has no automatic exit: a `running` Job is not `cancelling`, so the
cancellation reconciliation and `cancellation_debt` never see it. These tests pin the two
things that make such a hold acceptable: one stable event an alert can match, and one
read-only detail an operator can enumerate -- neither of which may fail `/readyz`.
"""

from __future__ import annotations

import psycopg
import pytest

from speechnexus_backend.speech.application.job_dispatcher import (
    STOP_EVIDENCE_UNVERIFIED_EVENT,
    STOP_EVIDENCE_UNVERIFIED_LOG_FIELDS,
    stop_evidence_unverified_log,
)
from speechnexus_backend.speech.infrastructure.readiness import (
    STOP_EVIDENCE_DEBT_PREFIX,
    StopEvidenceDebtProbe,
    _describe_stop_evidence_debt,
    _failure_category,
    stop_evidence_unavailable_detail,
    unavailable_detail,
)


def test_the_refusal_emits_one_stable_event_with_every_documented_field() -> None:
    record = stop_evidence_unverified_log(
        job_id="job-" + "a" * 32,
        submit_intent_id="submit-" + "b" * 32,
        stop_evidence_kind="unconfirmed_timeout",
        cleanup_state="quarantined",
        execution_state="aborted",
        dispatch_attempts=5,
    )

    assert record["event"] == "dispatch_stop_evidence_unverified"
    assert record["event"] == STOP_EVIDENCE_UNVERIFIED_EVENT
    assert tuple(record) == STOP_EVIDENCE_UNVERIFIED_LOG_FIELDS
    assert record["reason_code"] == "stop_evidence_unverified"
    assert record["stop_evidence_kind"] == "unconfirmed_timeout"
    assert record["dispatch_attempts"] == 5
    # The gate returns before settling, so the admitted capacity is still reserved.
    assert record["lease_held"] is True
    for field in STOP_EVIDENCE_UNVERIFIED_LOG_FIELDS:
        assert field in record


def test_the_event_carries_no_request_content_or_credential() -> None:
    record = stop_evidence_unverified_log(
        job_id="job-" + "a" * 32,
        submit_intent_id="submit-" + "b" * 32,
        stop_evidence_kind=None,
        cleanup_state=None,
        execution_state="failed",
        dispatch_attempts=1,
    )

    text = repr(record)
    assert "Bearer" not in text and "http" not in text and "command" not in text


def test_the_detail_names_the_job_the_execution_and_the_missing_evidence() -> None:
    detail = _describe_stop_evidence_debt([("job-a", "intent-1", "failed", "complete", None)])

    assert detail is not None
    assert detail.startswith("stop_evidence_debt: 1 running Job(s) hold capacity")
    assert detail.endswith("job-a -> intent-1(execution=failed, cleanup=complete, evidence=none)")


def test_the_detail_bounds_how_many_jobs_it_names() -> None:
    detail = _describe_stop_evidence_debt(
        [(f"job-{index}", f"intent-{index}", "failed", "complete", None) for index in range(9)]
    )

    assert detail is not None
    assert detail.count(" -> ") == 3
    assert detail.endswith("(+6 more)")
    assert len(detail) < 500


def test_a_detail_with_long_identifiers_is_cut_to_a_bounded_size() -> None:
    """Row counts are bounded, but 96 long identifiers are still ~17 kB of detail.

    A readiness detail reaches `/readyz` and the diagnostics body on every poll, so its
    size has to be bounded independently of how many rows the query returns. The cut keeps
    a readable prefix and says it was cut, instead of ending mid-identifier.
    """
    rows = [
        (
            f"job-{index:03d}" + "a" * 119,
            f"submit-{index:03d}" + "b" * 115,
            "failed",
            "complete",
            None,
        )
        for index in range(96)
    ]
    detail = _describe_stop_evidence_debt(rows)

    assert detail is not None
    assert len(detail) <= 512
    assert detail.endswith("... (truncated)")
    assert detail.startswith("stop_evidence_debt: 96 running Job(s)")
    # The cut lands on a job boundary, so the first identifier is reported in full.
    assert rows[0][0] in detail


def test_the_cancellation_detail_is_bounded_the_same_way() -> None:
    from speechnexus_backend.speech.infrastructure.readiness import _describe_debt

    # Distinct (job, lease) pairs: the detail names each pair once, so 96 rows of the
    # same pair would collapse to one line and never reach the 512-character bound.
    rows = [
        (f"job-{index:03d}" + "a" * 118, f"lease-{index:03d}" + "b" * 118) for index in range(96)
    ]
    detail = _describe_debt(
        [(job, lease, "reserved", "executing", None, None, None) for job, lease in rows]
    )

    assert detail is not None
    assert len(detail) <= 512
    assert detail.endswith("... (truncated)")


def test_a_short_detail_is_not_marked_truncated() -> None:
    detail = _describe_stop_evidence_debt([("job-a", "intent-1", "failed", "complete", None)])

    assert detail is not None
    assert not detail.endswith("... (truncated)")


def test_no_debt_means_no_detail() -> None:
    assert _describe_stop_evidence_debt([]) is None


def test_the_detail_says_which_layer_was_unreadable() -> None:
    detail = _describe_stop_evidence_debt(
        [("job-a", "intent-1", "failed", "complete", None)], missing="permission"
    )

    assert detail is not None
    assert detail.endswith("[lease detail unavailable: permission]")


def test_an_unconfigured_probe_degrades_instead_of_failing_readiness() -> None:
    probe = StopEvidenceDebtProbe(None, schema="speech", timeout_seconds=1)

    assert probe.name == "stop_evidence_debt"
    assert probe.name == STOP_EVIDENCE_DEBT_PREFIX
    assert _run(probe) == "stop_evidence_debt: unavailable (not configured)"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (psycopg.errors.InsufficientPrivilege(), "permission"),
        (psycopg.errors.UndefinedTable(), "relation"),
        (TimeoutError(), "timeout"),
        (psycopg.OperationalError(), "connection"),
        (ValueError("synthetic"), "error"),
    ],
)
def test_every_failure_mode_is_classified_under_the_new_prefix(
    error: BaseException, category: str
) -> None:
    assert _failure_category(error) == category
    assert (
        stop_evidence_unavailable_detail(error) == f"stop_evidence_debt: unavailable ({category})"
    )
    # The two prefixes stay distinguishable: one alert rule per class of debt.
    assert unavailable_detail(error) == f"cancellation_debt: unavailable ({category})"


def test_an_unreadable_store_reports_instead_of_raising() -> None:
    """A probe that raises would turn a held Job into a 503 for the whole API."""

    class _Broken(StopEvidenceDebtProbe):
        async def _inspect(self) -> str | None:
            raise psycopg.errors.InsufficientPrivilege()

    probe = _Broken(
        "postgresql://synthetic@127.0.0.1:1/synthetic", schema="speech", timeout_seconds=1
    )

    assert _run(probe) == "stop_evidence_debt: unavailable (permission)"


def test_the_grace_and_limit_are_bounded() -> None:
    with pytest.raises(ValueError):
        StopEvidenceDebtProbe(None, schema="speech", timeout_seconds=1, grace_seconds=0)
    with pytest.raises(ValueError):
        StopEvidenceDebtProbe(None, schema="speech", timeout_seconds=1, grace_seconds=86_401)
    with pytest.raises(ValueError):
        StopEvidenceDebtProbe(None, schema="speech", timeout_seconds=1, limit=0)
    with pytest.raises(ValueError):
        StopEvidenceDebtProbe(None, schema="speech", timeout_seconds=1, limit=513)
    with pytest.raises(ValueError):
        StopEvidenceDebtProbe(None, schema="NotASchema", timeout_seconds=1)


def _run(probe: StopEvidenceDebtProbe) -> str | None:
    import asyncio

    return asyncio.run(probe.check())
