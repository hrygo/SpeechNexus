"""Only changed units become stale; a casting counter is never a global render dependency."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_backend.workbench.domain.book_render_plan import BookRenderPlan
from speechnexus_backend.workbench.domain.render_units import (
    RenderAttemptConflictError,
    RenderDependencies,
    RenderSource,
    RenderUnit,
    RevisionPin,
)
from speechnexus_backend.workbench.domain.versions import RevisionConflictError

NOW = datetime(2026, 10, 24, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)


def ready(unit_id: str, chapter: str, ordinal: int, role: str, voice: str) -> RenderUnit:
    initial = RenderUnit.create(
        unit_id=unit_id,
        book_id="book",
        ordinal=ordinal,
        dependencies=RenderDependencies(
            original=RevisionPin("original", 1),
            working=RevisionPin("working", 1),
            chapter_id=chapter,
            role_id=role,
            text_plan=RevisionPin("plan", 1),
            voice=RevisionPin(voice, 1),
            model="synthetic",
            model_binding=RevisionPin("binding", 1),
            text="合成片段 " + unit_id,
        ),
        now=NOW,
    )
    active = initial.start(
        expected_revision=1, job_id="job-" + unit_id, generation_id="gen-" + unit_id, now=NOW
    )
    return active.complete(
        generation_id="gen-" + unit_id,
        now=NOW,
        source=RenderSource(
            unit_id,
            "job-" + unit_id,
            "artifact-" + unit_id,
            "receipt-" + unit_id,
            active.dependencies.sha256,
        ),
    )


def book() -> BookRenderPlan:
    return BookRenderPlan(
        "book",
        1,
        1,
        (
            ready("one", "chapter-1", 0, "narrator", "voice-n"),
            ready("two", "chapter-1", 1, "speaker", "voice-s"),
            ready("three", "chapter-2", 0, "narrator", "voice-n"),
        ),
    )


def test_paragraph_edit_invalidates_only_that_unit_and_keeps_source_provenance() -> None:
    before = book()
    target = before.units[0]
    dependencies = replace(
        target.dependencies,
        text="修改合成片段",
        working=RevisionPin("working", 2),
        text_plan=RevisionPin("plan", 2),
    )
    after = before.revise_unit("one", dependencies=dependencies, expected_revision=1, now=LATER)
    assert (after.revision, after.casting_revision, after.stale_ids) == (2, 1, ("one",))
    assert after.units[0].dependencies is dependencies
    assert after.units[0].revision == 4 and after.units[0].source is None
    assert after.units[1] is before.units[1] and after.units[2] is before.units[2]
    assert after.units[1].source is not None and target.source is not None
    assert before.stale_ids == ()


def test_role_recast_only_invalidates_matching_role_across_chapters_without_reordering() -> None:
    before = book()
    after = before.recast(
        "narrator", voice=RevisionPin("new-voice", 2), expected_revision=1, now=LATER
    )
    assert (after.revision, after.casting_revision, after.stale_ids) == (2, 2, ("one", "three"))
    assert [value.id for value in after.units] == ["one", "two", "three"]
    assert after.units[1] is before.units[1] and after.units[1].source is not None
    for index in (0, 2):
        assert after.units[index].dependencies.voice == RevisionPin("new-voice", 2)
        assert after.units[index].source is None and after.units[index].status == "stale"
        assert after.units[index].dependencies.working == before.units[index].dependencies.working


def test_recast_a_b_a_never_resurrects_old_artifacts_or_dispatch_identities() -> None:
    a = book()
    b = a.recast("narrator", voice=RevisionPin("new", 1), expected_revision=1, now=LATER)
    again = b.recast("narrator", voice=RevisionPin("voice-n", 1), expected_revision=2, now=LATER)
    assert again.units[0].dependencies == a.units[0].dependencies
    assert again.stale_ids == ("one", "three") and again.casting_revision == 3
    with pytest.raises(RenderAttemptConflictError):
        again.units[0].start(expected_revision=5, job_id="job-one", generation_id="new", now=LATER)
    with pytest.raises(RenderAttemptConflictError):
        again.units[0].complete(generation_id="gen-one", source=a.units[0].source, now=LATER)  # type: ignore[arg-type]


def test_noops_retain_identity_but_validate_book_cas_and_latest_unit_time() -> None:
    before = book()
    assert (
        before.recast("narrator", voice=RevisionPin("voice-n", 1), expected_revision=1, now=NOW)
        is before
    )
    assert (
        before.revise_unit(
            "one", dependencies=before.units[0].dependencies, expected_revision=1, now=NOW
        )
        is before
    )
    future = replace(before, units=(replace(before.units[0], updated_at=LATER), *before.units[1:]))
    for value in (before, future):
        with pytest.raises(RevisionConflictError):
            value.recast(
                "narrator", voice=RevisionPin("voice-n", 1), expected_revision=2, now=LATER
            )
        with pytest.raises(RevisionConflictError):
            value.revise_unit(
                "one", dependencies=value.units[0].dependencies, expected_revision=2, now=LATER
            )
    with pytest.raises(ValueError):
        future.recast("narrator", voice=RevisionPin("voice-n", 1), expected_revision=1, now=NOW)
    with pytest.raises(ValueError):
        future.revise_unit(
            "two", dependencies=future.units[1].dependencies, expected_revision=1, now=NOW
        )


@pytest.mark.parametrize("revision", [True, 0, -1, 1.0, 10**19])
def test_book_revision_and_cas_are_strict_integers(revision: Any) -> None:
    value = book()
    for changes in ({"revision": revision}, {"casting_revision": revision}):
        with pytest.raises(ValueError):
            replace(value, **changes)
    with pytest.raises(ValueError):
        value.recast("narrator", voice=RevisionPin("new", 1), expected_revision=revision, now=NOW)
    with pytest.raises(ValueError):
        value.revise_unit(
            "one", dependencies=value.units[0].dependencies, expected_revision=revision, now=NOW
        )


@pytest.mark.parametrize(
    "changes",
    [{"book_id": ""}, {"book_id": []}, {"units": ()}, {"units": []}, {"units": ("not-unit",)}],
)
def test_segmented_snapshot_rejects_empty_or_mutable_or_wrongly_typed_inputs(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        replace(book(), **changes)


def test_duplicate_identity_ordinal_cross_book_and_mixed_role_voice_are_rejected() -> None:
    value = book()
    first, second, third = value.units
    invalid = (
        (first, first),
        (first, replace(second, ordinal=0)),
        (first, replace(third, book_id="another")),
        (
            first,
            replace(
                third,
                dependencies=replace(third.dependencies, voice=RevisionPin("other", 1)),
                status="stale",
                source=None,
            ),
        ),
    )
    for units in invalid:
        with pytest.raises(ValueError):
            replace(value, units=units)
    assert value.units == (first, second, third)
    with pytest.raises(FrozenInstanceError):
        value.units = ()  # type: ignore[misc]


def test_single_unit_edit_cannot_silently_recast_the_same_role_inconsistently() -> None:
    value = book()
    with pytest.raises(ValueError):
        value.revise_unit(
            "one",
            dependencies=replace(value.units[0].dependencies, voice=RevisionPin("other", 1)),
            expected_revision=1,
            now=NOW,
        )


@pytest.mark.parametrize("identifier", ["missing", "", [], None])
def test_unknown_unit_or_role_rejects_without_echo_or_changing_any_unit(identifier: Any) -> None:
    value = book()
    with pytest.raises(ValueError) as caught:
        value.recast(identifier, voice=RevisionPin("other", 1), expected_revision=1, now=NOW)
    assert "missing" not in str(caught.value)
    with pytest.raises(ValueError):
        value.revise_unit(
            identifier, dependencies=value.units[0].dependencies, expected_revision=1, now=NOW
        )
    assert value.stale_ids == ()


@pytest.mark.parametrize("now", [datetime(2026, 10, 24), None])
def test_naive_and_wrong_time_types_are_rejected_even_for_noop(now: Any) -> None:
    value = book()
    with pytest.raises(ValueError):
        value.recast("narrator", voice=RevisionPin("voice-n", 1), expected_revision=1, now=now)
    with pytest.raises(ValueError):
        value.revise_unit(
            "one", dependencies=value.units[0].dependencies, expected_revision=1, now=now
        )


def test_invalid_voice_dependencies_and_revision_exhaustion_reject() -> None:
    value = book()
    with pytest.raises(ValueError):
        value.recast("narrator", voice={}, expected_revision=1, now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        value.revise_unit("one", dependencies={}, expected_revision=1, now=NOW)  # type: ignore[arg-type]
    for maximum in (
        replace(value, revision=10**19 - 1),
        replace(value, casting_revision=10**19 - 1),
    ):
        with pytest.raises(ValueError):
            maximum.recast(
                "narrator",
                voice=RevisionPin("other", 1),
                expected_revision=maximum.revision,
                now=NOW,
            )
