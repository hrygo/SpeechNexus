"""The Job kind vocabulary is one table with several readers (#1040).

These assertions are structural on purpose: the runtime behavior each reader
used to pin with its own literal is covered elsewhere. What is checked here is
that no reader can drift to a different vocabulary row without a test failing.
"""

from __future__ import annotations

from typing import get_args

from speechnexus_backend.speech.domain.public_jobs import (
    ADMITTED_JOB_KINDS,
    JOB_KIND_PROFILES,
    JOB_KIND_TASK,
    JOB_KINDS,
    PINNED_JOB_KINDS,
    UNDISPATCHABLE_JOB_KINDS,
    VOICE_DRAFT_JOB_KINDS,
    VOICE_JOB_DRAFT_KINDS,
    DispatchableJobKind,
    JobKind,
)


def test_one_profile_row_per_closed_kind() -> None:
    assert get_args(JobKind) == JOB_KINDS
    assert tuple(JOB_KIND_PROFILES) == JOB_KINDS


def test_every_derived_view_is_a_projection_of_the_rows() -> None:
    assert {
        kind: profile.task
        for kind, profile in JOB_KIND_PROFILES.items()
        if profile.task is not None
    } == JOB_KIND_TASK
    assert ADMITTED_JOB_KINDS == ("asr", "tts", "voice_validation")
    assert {
        kind for kind, profile in JOB_KIND_PROFILES.items() if profile.command_kind is not None
    } == set(ADMITTED_JOB_KINDS)


def test_a_preview_task_is_the_drafts_not_a_second_row() -> None:
    # The preview's task is another layer: it comes from the target draft's
    # kind, so its own row must not invent one.
    assert JOB_KIND_PROFILES["voice_preview"].task is None
    assert "voice_preview" not in JOB_KIND_TASK
    assert VOICE_DRAFT_JOB_KINDS == {
        "design": "voice_design",
        "clone": "voice_clone",
    }
    assert VOICE_JOB_DRAFT_KINDS == {
        "voice_design": "design",
        "voice_clone": "clone",
    }


def test_only_the_kinds_whose_create_carries_a_pin_are_pinned() -> None:
    # ``asr`` is deliberately absent: its request shape has no ``model_binding``,
    # so a pin reaching the store is a closed invariant, not a request answer.
    assert frozenset({"tts", "voice_preview", "voice_validation"}) == PINNED_JOB_KINDS


def test_every_kind_is_either_admitted_or_refused() -> None:
    """A claim selects a lane and then joins the admission attempt and lease.

    So a kind is claimable only when it declares both columns, and the kinds missing
    either are exactly the ones whose create is refused before a row is written. The
    two sets partition the vocabulary: a new kind cannot be added to the table without
    landing in one of them, and it cannot be in both.
    """

    missing_a_column = {
        kind
        for kind, profile in JOB_KIND_PROFILES.items()
        if profile.lane is None or profile.command_kind is None
    }
    assert set(UNDISPATCHABLE_JOB_KINDS) == missing_a_column
    assert set(UNDISPATCHABLE_JOB_KINDS).isdisjoint(ADMITTED_JOB_KINDS)
    assert set(UNDISPATCHABLE_JOB_KINDS) | set(ADMITTED_JOB_KINDS) == set(JOB_KINDS)
    # The literal a queue validates its selection against is the lane column alone;
    # a Job is really claimable only when it is laned *and* admitted, and the
    # refusal is the complement of that intersection.
    laned = {kind for kind, profile in JOB_KIND_PROFILES.items() if profile.lane is not None}
    assert set(get_args(DispatchableJobKind)) == laned
    assert set(UNDISPATCHABLE_JOB_KINDS) == set(JOB_KINDS) - (laned & set(ADMITTED_JOB_KINDS))
    # Voice generation is refused for the missing stored command, not for a missing
    # lane: it declares the lane a future recipe would use.
    assert JOB_KIND_PROFILES["voice_design"].lane == "voice_generation"
    assert JOB_KIND_PROFILES["voice_clone"].lane == "voice_generation"
    assert JOB_KIND_PROFILES["voice_preview"].lane is None
