"""Job kind lane membership comes from the one kind table (#1047).

The dispatcher used to spell its own kind tuples: a kind added to the public
table landed outside every queue and was only noticed by its absence in
production. These assertions make the ``lane`` column the single source and the
dispatch tuples its projections. They are structural on purpose; the runtime
behavior (the default queue still claims ASR/TTS only) is pinned by the dispatch
tests.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from itertools import combinations
from typing import get_args

import pytest

from speechnexus_backend.speech.domain.job_dispatch import (
    DISPATCHABLE_JOB_KIND_UNION,
    DISPATCHABLE_JOB_KINDS,
    VOICE_GENERATION_JOB_KINDS,
    VOICE_JOB_KINDS,
    VOICE_VALIDATION_JOB_KINDS,
    DispatchJobKind,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JOB_KIND_PROFILES,
    JobKind,
    JobKindProfile,
    JobLane,
)


def _kinds_in(*lanes: str) -> set[str]:
    return {kind for kind, profile in JOB_KIND_PROFILES.items() if profile.lane in lanes}


def test_the_default_queue_claims_exactly_the_default_lane() -> None:
    assert set(DISPATCHABLE_JOB_KINDS) == _kinds_in("default")
    assert DISPATCHABLE_JOB_KINDS == ("asr", "tts")


def test_the_voice_queues_claim_exactly_the_voice_lanes() -> None:
    assert set(VOICE_GENERATION_JOB_KINDS) == _kinds_in("voice_generation")
    assert set(VOICE_VALIDATION_JOB_KINDS) == _kinds_in("voice_validation")
    assert set(VOICE_JOB_KINDS) == _kinds_in("voice_generation", "voice_validation")


def test_the_default_lane_and_the_voice_lanes_never_overlap() -> None:
    # Voice work runs on a lane whose worker is configured for its engine
    # adapter, so the default lane must not claim it. This is policy, not a
    # construction-time rejection: the derived union would happily accept a
    # kind that was given the wrong lane.
    assert set(DISPATCHABLE_JOB_KINDS) & set(VOICE_GENERATION_JOB_KINDS) == set()
    assert set(DISPATCHABLE_JOB_KINDS) & set(VOICE_VALIDATION_JOB_KINDS) == set()
    assert "voice_design" not in DISPATCHABLE_JOB_KINDS


def test_the_three_lanes_are_pairwise_disjoint() -> None:
    for first, second in combinations(get_args(JobLane), 2):
        assert _kinds_in(first) & _kinds_in(second) == set()


def test_every_kind_is_in_at_most_one_lane() -> None:
    laned: set[str] = set()
    for lane in get_args(JobLane):
        lane_kinds = _kinds_in(lane)
        assert laned & lane_kinds == set()
        laned |= lane_kinds


def test_the_laned_kinds_are_the_dispatch_union() -> None:
    laned: set[str] = set()
    for lane in get_args(JobLane):
        laned |= _kinds_in(lane)
    assert laned == set(DISPATCHABLE_JOB_KIND_UNION)
    assert laned == set(DISPATCHABLE_JOB_KINDS) | set(VOICE_JOB_KINDS)


def test_a_kind_without_a_lane_is_exactly_the_undispatched_preview() -> None:
    # The table partitions every kind into exactly one lane or none. Only
    # ``voice_preview`` is deliberately laneless: it is committed and readable,
    # but no queue dispatches it yet. A new kind that forgets its lane fails
    # here instead of silently landing outside every queue.
    unlaned = {kind for kind, profile in JOB_KIND_PROFILES.items() if profile.lane is None}
    assert unlaned == {"voice_preview"}
    assert unlaned == set(get_args(JobKind)) - set(DISPATCHABLE_JOB_KIND_UNION)
    assert unlaned | {
        kind for kind, profile in JOB_KIND_PROFILES.items() if profile.lane is not None
    } == set(get_args(JobKind))


def test_the_dispatch_type_names_exactly_the_union() -> None:
    # mypy cannot derive this Literal from the runtime table. This equality is
    # the only check for the reverse drift: editing the alias without the table
    # keeps mypy green. A new laned kind is caught by several assertions (the
    # literal pins, the union derivation, the publish-side media map), not by
    # this one alone.
    assert get_args(DispatchJobKind) == DISPATCHABLE_JOB_KIND_UNION


def test_the_lane_column_is_required_and_closed() -> None:
    # A new kind cannot omit the column: the dataclass has no default, and no
    # value outside ``JobLane`` reaches an imported module.
    lane_field = next(field for field in fields(JobKindProfile) if field.name == "lane")
    assert lane_field.default is MISSING
    assert lane_field.default_factory is MISSING
    assert {
        profile.lane for profile in JOB_KIND_PROFILES.values() if profile.lane is not None
    } <= set(get_args(JobLane))


@pytest.mark.parametrize("lane", ["voice", "defaults", "", "DEFAULT"])
def test_a_lane_outside_the_closed_vocabulary_is_rejected(lane: str) -> None:
    with pytest.raises(ValueError, match="Invalid Job kind lane"):
        JobKindProfile(
            task="asr_file",
            model_slot="asr",
            progress_unit="audio_ms",
            command_kind="asr",
            pinned=False,
            lane=lane,  # type: ignore[arg-type]
        )
