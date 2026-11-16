"""The operator exit refuses anything it cannot prove, and prints what it moved."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from speechnexus_backend import completed_release_cli as cli
from speechnexus_backend.speech.application.completed_releases import CompletedLeaseSnapshot
from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineSurfaceError,
    QuarantineUnavailableError,
    QuarantineUnknownError,
)
from speechnexus_backend.speech.domain.completed_release import (
    CompletedStopEvidence,
    DrainRecord,
)
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseLedger,
    ReleaseOutcome,
)

INSTANCE = "dur-1"
GENERATION = 3
LEASE = "lease-stage-main-1"
INTENT = "job-1"
OBSERVED_AT = "1970-01-01T00:00:07Z"
OBSERVED_AT_US = 7_000_000
TERMINATED_AT_US = 5_000_000
REASON = "ticket #852: drained dur-1 generation 3"
DRAIN: dict[str, Any] = {
    "instance_id": INSTANCE,
    "instance_generation": GENERATION,
    "drain_state": "drained",
    "active_intent_count": 0,
    "submitting_intent_count": 0,
    "quarantined_intent_count": 0,
    "safe_to_stop_instance": True,
    "updated_at": "1970-01-01T00:00:06Z",
}


class _Adapter:
    """Scripted snapshot reads; the released lease reports zero held slots."""

    def __init__(self, *, held_before: int = 1, held_after: int = 0) -> None:
        self.reads = 0
        self._held = (held_before, held_after)

    def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None:
        held = self._held[min(self.reads, len(self._held) - 1)]
        self.reads += 1
        return snapshot(held_slots=held)


class _MissingAdapter:
    def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None:
        return None


class _Service:
    def __init__(self, *, error: BaseException | None = None, replay: bool = False) -> None:
        self.calls: list[tuple[Any, DrainRecord]] = []
        self._error, self._replay = error, replay

    def release(self, evidence: Any, drain: DrainRecord) -> ReleaseOutcome:
        self.calls.append((evidence, drain))
        if self._error is not None:
            raise self._error
        return ReleaseOutcome(
            ReleaseLedger(
                lease_id=LEASE,
                evidence_kind="operator_verified_instance_terminated",
                evidence_digest=evidence.evidence_digest,
                evidence_observed_at_us=evidence.observed_at_us,
                reason=evidence.reason,
                budget_released_at_us=8_000_000,
                released_submit_intent_ids=(INTENT,),
                idempotency_key_digest=evidence.idempotency_key_digest,
                request_digest=evidence.request_digest,
            ),
            self._replay,
        )


def snapshot(
    *,
    held_slots: int = 1,
    intent_version: int = 4,
    submit_intent_id: str | None = INTENT,
    execution_state: str | None = "succeeded",
    execution_cleanup_state: str | None = "complete",
    fence_accepted_at_us: int | None = 3_000_000,
    reconciliation_settled_at_us: int | None = None,
    job_state: str | None = "cancelling",
) -> CompletedLeaseSnapshot:
    return CompletedLeaseSnapshot(
        lease_id=LEASE,
        operation_id="job-1",
        attempt_id="attempt-1",
        stage_id="stage-main",
        instance_id=INSTANCE,
        instance_generation=GENERATION,
        lease_state="released" if held_slots == 0 else "reserved",
        lease_cleanup_state="complete" if held_slots == 0 else "pending",
        budget_released_at_us=8_000_000 if held_slots == 0 else None,
        quarantine_observed_at_us=None,
        submit_intent_id=submit_intent_id,
        execution_state=execution_state,
        execution_cleanup_state=execution_cleanup_state,
        recorded_at_us=1_000_000,
        dispatched_at_us=2_000_000,
        terminated_at_us=TERMINATED_AT_US,
        intent_version=intent_version,
        stop=CompletedStopEvidence("engine_terminal_response", TERMINATED_AT_US, 1, 1),
        fence_accepted_at_us=fence_accepted_at_us,
        reconciliation_settled_at_us=reconciliation_settled_at_us,
        job_state=job_state,
        instance_held_slots=held_slots,
        instance_quarantined_slots=0,
    )


def drain() -> DrainRecord:
    return cli.parse_drain_response(DRAIN)


def run(
    *,
    adapter: Any = None,
    service: Any = None,
    drain_record: DrainRecord | None = None,
    dry_run: bool = False,
    lease_id: str = LEASE,
    reason: str = REASON,
    observed_at: str = OBSERVED_AT,
) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.execute(
        adapter=_Adapter() if adapter is None else adapter,
        service=_Service() if service is None else service,
        lease_id=lease_id,
        drain=drain() if drain_record is None else drain_record,
        observed_at_text=observed_at,
        reason=reason,
        idempotency_key=None,
        dry_run=dry_run,
        out=out,
    )
    return code, out.getvalue()


def test_the_exit_reports_the_capacity_it_returned() -> None:
    """#852: the whole point is a zero, so the tool prints it before and after."""
    code, text = run()

    assert code == cli.EXIT_RELEASED
    assert "instance_held_slots: 1" in text
    assert f"instance_id: {INSTANCE}" in text
    assert f"instance_generation: {GENERATION}" in text
    assert "capacity before:" in text
    assert "capacity after:" in text
    assert text.count("instance_held_slots: 0") == 1
    assert "outcome: committed" in text
    assert "drain_state: drained" in text
    assert "the reconciliation worker closes the Job as failed/cancellation_unconfirmed" in text


def test_the_release_runs_once_and_a_replay_is_named_as_one() -> None:
    service = _Service(replay=True)

    code, text = run(service=service)

    assert code == cli.EXIT_RELEASED
    assert len(service.calls) == 1
    assert "outcome: replayed" in text


def test_a_dry_run_writes_nothing() -> None:
    service = _Service()

    code, text = run(service=service, dry_run=True)

    assert code == cli.EXIT_RELEASED
    assert service.calls == []
    assert "verdict: releasable (dry run, nothing written)" in text
    assert "release:" not in text


def test_a_drain_of_another_generation_is_refused_before_any_write() -> None:
    """#852: dropping the pin is the red case, so it may not silently release."""
    service = _Service()
    wrong = cli.parse_drain_response(DRAIN | {"instance_generation": GENERATION + 1})

    code, text = run(service=service, drain_record=wrong)

    assert code == cli.EXIT_REFUSED
    assert "refused: completed_release_drain_mismatch" in text
    assert service.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"drain_state": "draining"},
        {"active_intent_count": 2},
        {"safe_to_stop_instance": False},
    ],
)
def test_an_undrained_generation_is_refused_before_any_write(
    overrides: dict[str, Any],
) -> None:
    """#852: allowing a release without a drain is the other red case."""
    service = _Service()
    undrained = cli.parse_drain_response(DRAIN | overrides)

    code, text = run(service=service, drain_record=undrained)

    assert code == cli.EXIT_REFUSED
    assert "refused: completed_release_not_drained" in text
    assert service.calls == []


def test_an_unproven_execution_names_the_protocol_that_owns_it() -> None:
    class _NoTerminal(_Adapter):
        def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None:
            return replace(snapshot(), stop=None)

    service = _Service()
    out = io.StringIO()
    code = cli.execute(
        adapter=_NoTerminal(),
        service=service,
        lease_id=LEASE,
        drain=drain(),
        observed_at_text=OBSERVED_AT,
        reason=REASON,
        idempotency_key=None,
        dry_run=False,
        out=out,
    )

    assert code == cli.EXIT_REFUSED
    assert "completed_release_requires_terminal_evidence" in out.getvalue()
    assert service.calls == []


def test_a_missing_lease_is_not_found_rather_than_an_outage() -> None:
    code, text = run(adapter=_MissingAdapter())

    assert code == cli.EXIT_REFUSED
    assert "refused: not_found" in text


def test_a_lease_without_an_execution_is_refused() -> None:
    adapter = _Adapter()
    adapter.completed_snapshot = lambda lease_id: snapshot(submit_intent_id=None)  # type: ignore[method-assign]
    service = _Service()

    code, text = run(adapter=adapter, service=service)

    assert code == cli.EXIT_USAGE
    assert "no submit intent" in text
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (
            QuarantineReleaseRejectedError("quarantine_release_conflict"),
            "refused: quarantine_release_conflict",
        ),
        (QuarantineUnavailableError(), "unavailable: the release transaction did not start"),
        (QuarantineUnknownError(), "unknown: the commit outcome is unknown"),
        (
            QuarantineSurfaceError("quarantine_release_schema_not_migrated"),
            "quarantine_release_schema_not_migrated",
        ),
    ],
)
def test_a_storage_answer_is_reported_with_its_stable_code(
    error: BaseException, needle: str
) -> None:
    code, text = run(service=_Service(error=error))

    assert code == (
        cli.EXIT_REFUSED
        if isinstance(error, QuarantineReleaseRejectedError)
        else cli.EXIT_UNAVAILABLE
    )
    assert needle in text


def test_the_snapshot_failure_is_reported_as_unavailable() -> None:
    class _Broken(_Adapter):
        def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None:
            raise QuarantineUnavailableError

    code, text = run(adapter=_Broken())

    assert code == cli.EXIT_UNAVAILABLE
    assert "unavailable: the lease snapshot could not be read" in text


def test_another_lease_on_the_generation_is_called_out() -> None:
    """A non-zero after value is another lease's business, not a silent failure."""
    adapter = _Adapter(held_after=1)

    code, text = run(adapter=adapter)

    assert code == cli.EXIT_RELEASED
    assert "still holds 1 slot(s) from another lease" in text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        DRAIN | {"active_intent_count": "0"},
        DRAIN | {"active_intent_count": True},
        DRAIN | {"safe_to_stop_instance": "true"},
        DRAIN | {"drain_state": 1},
        DRAIN | {"instance_id": 7},
        DRAIN | {"instance_generation": "3"},
        DRAIN | {"updated_at": "yesterday"},
        DRAIN | {"drain_state": "unknown"},
        ["drained"],
    ],
)
def test_a_recorded_response_is_parsed_strictly(payload: object) -> None:
    with pytest.raises(cli.DrainResponseError):
        cli.parse_drain_response(payload)


def test_a_recorded_response_missing_a_counter_names_it() -> None:
    with pytest.raises(cli.DrainResponseError, match="quarantined_intent_count"):
        cli.parse_drain_response(
            {key: value for key, value in DRAIN.items() if key != "quarantined_intent_count"}
        )


def test_a_drain_file_is_bounded_and_must_be_json(tmp_path: Path) -> None:
    good = tmp_path / "drain.json"
    good.write_text(json.dumps(DRAIN))
    assert cli.read_drain_response(good) == drain()

    huge = tmp_path / "huge.json"
    huge.write_text(json.dumps(DRAIN | {"padding": "x" * cli.MAX_RESPONSE_BYTES}))
    with pytest.raises(cli.DrainResponseError, match="exceeds"):
        cli.read_drain_response(huge)

    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(cli.DrainResponseError, match="not JSON"):
        cli.read_drain_response(broken)


def test_the_entry_point_refuses_missing_inputs_without_opening_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A usage error must never reach the release identity or a transaction."""
    drain_file = tmp_path / "drain.json"
    drain_file.write_text(json.dumps(DRAIN))
    for variable in (cli.DSN_VARIABLE, cli.SCHEMA_VARIABLE):
        monkeypatch.delenv(variable, raising=False)
    base = [
        "--lease-id",
        LEASE,
        "--drain-response",
        str(drain_file),
        "--observed-at",
        OBSERVED_AT,
        "--reason",
        REASON,
    ]
    out = io.StringIO()

    assert cli.main(base, out=out) == cli.EXIT_USAGE
    assert cli.DSN_VARIABLE in out.getvalue()

    monkeypatch.setenv(cli.DSN_VARIABLE, "postgresql:///synthetic")
    out = io.StringIO()
    assert cli.main([*base[:5], "not-an-instant", *base[6:]], out=out) == cli.EXIT_USAGE
    assert "--observed-at" in out.getvalue()

    out = io.StringIO()
    assert cli.main([*base, "--reason", "   "], out=out) == cli.EXIT_USAGE
    assert "--reason" in out.getvalue()

    out = io.StringIO()
    missing = [*base]
    missing[3] = str(tmp_path / "absent.json")
    assert cli.main(missing, out=out) == cli.EXIT_USAGE
    assert "cannot read" in out.getvalue()


def test_an_already_released_lease_is_decided_by_the_ledger_not_by_the_gate() -> None:
    """A retry must not be refused just because the state it acted on is gone.

    The committed ledger row is the final fact for this lease, so the tool asks it
    instead of re-deciding a gate that can only say "already released" now.
    """
    service = _Service(replay=True)

    code, text = run(adapter=_Adapter(held_before=0, held_after=0), service=service)

    assert code == cli.EXIT_RELEASED
    assert "lease: already released; the committed ledger decides this retry" in text
    assert len(service.calls) == 1


def test_a_dry_run_on_a_released_lease_says_a_retry_would_replay() -> None:
    service = _Service()

    code, text = run(adapter=_Adapter(held_before=0, held_after=0), service=service, dry_run=True)

    assert code == cli.EXIT_RELEASED
    assert "verdict: already released" in text
    assert service.calls == []


def test_the_tool_refuses_a_job_that_was_never_cancelled() -> None:
    """#852 review M2: measured -- a Job with no fence released successfully."""
    service = _Service()
    adapter = _Adapter()
    adapter.completed_snapshot = lambda lease_id: snapshot(fence_accepted_at_us=None)  # type: ignore[method-assign]

    code, text = run(adapter=adapter, service=service)

    assert code == cli.EXIT_REFUSED
    assert "refused: completed_release_not_accepted_cancellation" in text
    assert service.calls == []


def test_the_tool_refuses_a_settled_cancellation() -> None:
    """A concluded judgment already accounted for this capacity."""
    service = _Service()
    adapter = _Adapter()
    adapter.completed_snapshot = lambda lease_id: snapshot(  # type: ignore[method-assign]
        reconciliation_settled_at_us=8_000_000
    )

    code, text = run(adapter=adapter, service=service)

    assert code == cli.EXIT_REFUSED
    assert "refused: completed_release_already_settled" in text
    assert service.calls == []


def test_the_tool_refuses_a_job_that_is_no_longer_cancelling() -> None:
    service = _Service()
    adapter = _Adapter()
    adapter.completed_snapshot = lambda lease_id: snapshot(job_state="failed")  # type: ignore[method-assign]

    code, text = run(adapter=adapter, service=service)

    assert code == cli.EXIT_REFUSED
    assert "refused: completed_release_not_cancelling" in text
    assert service.calls == []


def test_an_inconsistent_lease_is_refused_rather_than_summarised() -> None:
    """Two intents or two terminal facts are a repair problem, not a choice."""
    from speechnexus_backend.speech.domain.quarantine_release import (
        QuarantineReleaseRejectedError,
    )

    class _Ambiguous(_Adapter):
        def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None:
            raise QuarantineReleaseRejectedError("reconciliation_required")

    code, text = run(adapter=_Ambiguous())

    assert code == cli.EXIT_REFUSED
    assert "refused: reconciliation_required" in text


def test_a_stale_drain_is_refused_before_any_write() -> None:
    """#852 review M1: a drain taken before the execution ended proves nothing.

    The recorded file is otherwise perfect (drained, zero unfinished work, the
    right generation): only the gateway's own read time is too early.
    """
    service = _Service()
    stale = cli.parse_drain_response(DRAIN | {"updated_at": "1970-01-01T00:00:04Z"})

    code, text = run(service=service, drain_record=stale)

    assert code == cli.EXIT_REFUSED
    assert "refused: completed_release_drain_stale" in text
    assert service.calls == []


def test_a_drain_taken_after_the_observation_is_refused_before_any_write() -> None:
    service = _Service()
    late = cli.parse_drain_response(DRAIN | {"updated_at": "1970-01-01T00:00:09Z"})

    code, text = run(service=service, drain_record=late)

    assert code == cli.EXIT_REFUSED
    assert "refused: completed_release_drain_stale" in text
    assert service.calls == []
