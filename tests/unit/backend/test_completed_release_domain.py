"""The completed-execution operator release decides on a pinned drain, not on hope."""

from __future__ import annotations

import pytest

from speechnexus_backend.speech.domain.completed_release import (
    CompletedIntent,
    CompletedReleaseFacts,
    CompletedReleaseRejectedError,
    CompletedStopEvidence,
    DrainRecord,
    completed_evidence_digest,
    prepared_digest,
    require_completed_releasable,
    require_observed_at,
)

TERMINATED_AT_US = 5_000_000
OBSERVED_AT_US = 7_000_000
NOW_US = 9_000_000
REASON = "ticket #852: drained dur-1 generation 3"
INSTANCE = "dur-1"
GENERATION = 3


def drain(**overrides: object) -> DrainRecord:
    values: dict[str, object] = {
        "instance_id": INSTANCE,
        "instance_generation": GENERATION,
        "drain_state": "drained",
        "active_intent_count": 0,
        "submitting_intent_count": 0,
        "quarantined_intent_count": 0,
        "safe_to_stop_instance": True,
        "updated_at_us": 6_000_000,
    }
    values.update(overrides)
    return DrainRecord(**values)  # type: ignore[arg-type]


def intent(**overrides: object) -> CompletedIntent:
    values: dict[str, object] = {
        "submit_intent_id": "job-1",
        "execution_state": "succeeded",
        "cleanup_state": "complete",
        "recorded_at_us": 1_000_000,
        "dispatched_at_us": 2_000_000,
        "terminated_at_us": TERMINATED_AT_US,
        "version": 4,
    }
    values.update(overrides)
    return CompletedIntent(**values)  # type: ignore[arg-type]


def facts(**overrides: object) -> CompletedReleaseFacts:
    values: dict[str, object] = {
        "lease_id": "lease-1",
        "operation_id": "job-1",
        "instance_id": INSTANCE,
        "instance_generation": GENERATION,
        "lease_state": "reserved",
        "lease_cleanup_state": "pending",
        "budget_released_at_us": None,
        "quarantine_observed_at_us": None,
        "intents": (intent(),),
        "stop": CompletedStopEvidence("engine_terminal_response", TERMINATED_AT_US, 1, 1),
        "drain": drain(),
        "observed_at_us": OBSERVED_AT_US,
        "reason": REASON,
    }
    values.update(overrides)
    return CompletedReleaseFacts(**values)  # type: ignore[arg-type]


def digest(facts_value: CompletedReleaseFacts, **overrides: object) -> str:
    values: dict[str, object] = {
        "lease_id": facts_value.lease_id,
        "submit_intent_id": facts_value.intents[0].submit_intent_id,
        "instance_id": facts_value.instance_id,
        "instance_generation": facts_value.instance_generation,
        "drain": facts_value.drain,
        "observed_at_us": facts_value.observed_at_us,
        "reason": facts_value.reason,
    }
    values.update(overrides)
    return completed_evidence_digest(**values)  # type: ignore[arg-type]


def test_a_drained_generation_authorizes_the_completed_release() -> None:
    """The one shape the bounded operator exit exists for."""
    target = facts()

    assert require_completed_releasable(target) is target.intents[0]


def test_the_testimony_is_pinned_to_the_lease_instance_and_generation() -> None:
    """#852: the testimony may not be about "some instance" or the Job.

    A drain of another generation proves nothing about this reservation, so it is
    refused as a mismatch instead of being accepted because the job id matched.
    """
    assert drain().drained
    assert drain(instance_generation=GENERATION + 1).drained

    with pytest.raises(CompletedReleaseRejectedError) as another_generation:
        require_completed_releasable(facts(drain=drain(instance_generation=GENERATION + 1)))
    assert another_generation.value.code == "completed_release_drain_mismatch"

    with pytest.raises(CompletedReleaseRejectedError) as another_instance:
        require_completed_releasable(facts(drain=drain(instance_id="dur-other")))
    assert another_instance.value.code == "completed_release_drain_mismatch"


def test_a_drain_of_the_pinned_generation_changes_the_digest() -> None:
    """Fixing generation 2 must not produce generation 2's digest."""
    base = facts()
    assert digest(base) != digest(base, instance_generation=GENERATION + 1)
    assert digest(base) != digest(base, drain=drain(updated_at_us=6_500_000))
    assert digest(base) != digest(base, reason="another reason")
    assert digest(base) == digest(base)


@pytest.mark.parametrize(
    "overrides",
    [
        {"drain_state": "draining"},
        {"drain_state": "quarantined"},
        {"safe_to_stop_instance": False},
        {"active_intent_count": 1},
        {"submitting_intent_count": 1},
        {"quarantined_intent_count": 1},
    ],
)
def test_an_undrained_generation_is_never_released(overrides: dict[str, object]) -> None:
    """#852: "completed but not drained" and "drained" may not share one testimony.

    The machine's terminal evidence proves the execution ended; only a drained
    generation proves the instance has nothing left, and without it the release
    would report capacity as safe to return while the gateway still owns work.
    """
    assert not drain(**overrides).drained

    with pytest.raises(CompletedReleaseRejectedError) as refused:
        require_completed_releasable(facts(drain=drain(**overrides)))
    assert refused.value.code == "completed_release_not_drained"


def test_a_successful_execution_without_the_gateway_terminal_fact_is_refused() -> None:
    """The exit releases a completed execution, never repairs an unproven one."""
    for stop in (
        None,
        CompletedStopEvidence("engine_terminal_response", TERMINATED_AT_US, 0, 1),
        CompletedStopEvidence("engine_terminal_response", TERMINATED_AT_US, 1, 0),
        CompletedStopEvidence("unconfirmed_timeout", TERMINATED_AT_US, 1, 1),
    ):
        with pytest.raises(CompletedReleaseRejectedError) as refused:
            require_completed_releasable(facts(stop=stop))
        assert refused.value.code == "completed_release_requires_terminal_evidence"


@pytest.mark.parametrize(
    "overrides",
    [
        {"lease_state": "released"},
        {"lease_state": "quarantined"},
        {"lease_state": "executing"},
        {"lease_cleanup_state": "complete"},
        {"budget_released_at_us": 8_000_000},
        {"quarantine_observed_at_us": 8_000_000},
        {"intents": ()},
        {"intents": (intent(execution_state="unknown_quarantined"),)},
        {"intents": (intent(execution_state="aborted"),)},
        {"intents": (intent(cleanup_state="pending"),)},
    ],
)
def test_every_other_shape_belongs_to_another_exit(overrides: dict[str, object]) -> None:
    """A first release of a completed execution only: nothing else is reconsidered."""
    with pytest.raises(CompletedReleaseRejectedError) as refused:
        require_completed_releasable(facts(**overrides))
    assert refused.value.code in {
        "quarantine_release_conflict",
        "completed_release_requires_terminal_evidence",
    }


def test_a_lease_with_two_submit_intents_cannot_be_built_at_all() -> None:
    """The model is one intent per lease; a release for two is a repair, not a loop."""
    with pytest.raises(ValueError):
        facts(intents=(intent(), intent(submit_intent_id="job-2")))


def test_the_observation_must_lie_inside_the_executions_own_window() -> None:
    """A testimony may not precede the execution it describes, or the server clock."""
    target = facts()

    require_observed_at(target, target.intents[0], NOW_US)

    for observed_at_us in (0, TERMINATED_AT_US - 1):
        with pytest.raises(CompletedReleaseRejectedError) as early:
            require_observed_at(facts(observed_at_us=observed_at_us), target.intents[0], NOW_US)
        assert early.value.code == "invalid"
    with pytest.raises(CompletedReleaseRejectedError) as late:
        require_observed_at(facts(observed_at_us=NOW_US + 1), target.intents[0], NOW_US)
    assert late.value.code == "invalid"


def test_an_intent_without_a_terminal_time_still_bounds_the_observation() -> None:
    """The lower bound must not collapse to zero when the gateway wrote no end time."""
    unbound = intent(terminated_at_us=None)

    with pytest.raises(CompletedReleaseRejectedError):
        require_observed_at(facts(observed_at_us=1_500_000), unbound, NOW_US)
    require_observed_at(facts(observed_at_us=OBSERVED_AT_US), unbound, NOW_US)


def test_the_wire_prepared_digest_matches_the_locked_one() -> None:
    """The operator tool mints the digest from the wire time; storage must agree."""
    target = facts()

    assert prepared_digest(
        lease_id=target.lease_id,
        submit_intent_id="job-1",
        instance_id=INSTANCE,
        instance_generation=GENERATION,
        drain=drain(),
        observed_at="1970-01-01T00:00:07Z",
        reason=REASON,
    ) == digest(target)


@pytest.mark.parametrize(
    "overrides",
    [
        {"drain_state": "complete"},
        {"drain_state": ""},
        {"instance_id": "Bad Instance"},
        {"instance_generation": 0},
        {"active_intent_count": -1},
        {"safe_to_stop_instance": 1},
        {"updated_at_us": 0},
    ],
)
def test_a_drain_record_refuses_values_the_gateway_never_sends(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        drain(**overrides)
