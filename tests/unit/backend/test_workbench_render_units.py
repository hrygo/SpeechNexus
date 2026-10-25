"""Fixed render intent and explicit attempts, without speech or storage I/O."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_backend.workbench.domain.render_units import (
    RenderAttemptConflictError,
    RenderDependencies,
    RenderGeneration,
    RenderSource,
    RenderUnit,
    RevisionPin,
)
from speechnexus_backend.workbench.domain.versions import RevisionConflictError

NOW = datetime(2026, 10, 24, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)


def dependencies(**changes: Any) -> RenderDependencies:
    return replace(
        RenderDependencies(
            original=RevisionPin("original", 1),
            working=RevisionPin("working", 2),
            chapter_id="chapter",
            role_id="narrator",
            text_plan=RevisionPin("plan", 1),
            voice=RevisionPin("voice", 3),
            model="synthetic",
            model_binding=RevisionPin("binding", 4),
            text="合成样例 e\u0301\n",
        ),
        **changes,
    )


def unit() -> RenderUnit:
    return RenderUnit.create(
        unit_id="unit", book_id="book", ordinal=0, dependencies=dependencies(), now=NOW
    )


def running() -> RenderUnit:
    return unit().start(expected_revision=1, job_id="job", generation_id="generation", now=LATER)


def source(value: RenderUnit, **changes: Any) -> RenderSource:
    assert value.generation is not None
    return replace(
        RenderSource(
            "unit", value.generation.job_id, "artifact", "receipt", value.dependencies.sha256
        ),
        **changes,
    )


def test_explicit_attempt_pins_result_without_granting_saved_or_export_status() -> None:
    initial = unit()
    active = running()
    result = source(active)
    ready = active.complete(generation_id="generation", source=result, now=LATER)
    assert (initial.status, initial.revision, initial.generation, initial.source) == (
        "pending",
        1,
        None,
        None,
    )
    assert (active.status, active.revision, active.source) == ("generating", 2, None)
    assert (ready.status, ready.revision, ready.source) == ("ready", 3, result)
    assert ready.generation == active.generation
    assert ready.chapter_id == "chapter" and ready.role_id == "narrator"
    assert not hasattr(ready, "saved_asset_id") and not hasattr(ready, "exportable")
    assert "合成样例" not in repr(ready)
    with pytest.raises(FrozenInstanceError):
        ready.status = "failed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"original": RevisionPin("original", 2)},
        {"working": RevisionPin("working", 3)},
        {"chapter_id": "chapter-2"},
        {"role_id": "role-2"},
        {"text_plan": RevisionPin("plan", 2)},
        {"voice": RevisionPin("voice", 4)},
        {"pronunciation": RevisionPin("dictionary", 1)},
        {"model": "other"},
        {"model_binding": RevisionPin("binding", 5)},
        {"profile": RevisionPin("profile", 1)},
        {"text": "合成样例 é\n"},
        {"text": "合成样例 e\u0301"},
    ],
)
def test_every_fixed_dependency_and_exact_unicode_changes_the_fingerprint(
    changes: dict[str, Any],
) -> None:
    before = dependencies()
    after = replace(before, **changes)
    assert before.sha256 == dependencies().sha256
    assert before.sha256 != after.sha256
    assert before.text == "合成样例 e\u0301\n"


def test_a_b_a_remains_stale_and_cannot_reuse_any_old_job_or_generation() -> None:
    active = running()
    ready = active.complete(generation_id="generation", source=source(active), now=LATER)
    b = ready.revise(expected_revision=3, dependencies=dependencies(text="改文"), now=LATER)
    a = b.revise(expected_revision=4, dependencies=dependencies(), now=LATER)
    assert (b.status, a.status, a.revision, a.source, a.generation) == (
        "stale",
        "stale",
        5,
        None,
        None,
    )
    assert ready.source is not None  # Original immutable snapshot survives.
    for job_id, generation_id in (("job", "new"), ("new", "generation")):
        with pytest.raises(RenderAttemptConflictError):
            a.start(expected_revision=5, job_id=job_id, generation_id=generation_id, now=LATER)
    again = a.start(expected_revision=5, job_id="job-2", generation_id="second", now=LATER)
    with pytest.raises(RenderAttemptConflictError):
        again.complete(generation_id="generation", source=source(active), now=LATER)
    with pytest.raises(RenderAttemptConflictError):
        again.complete(generation_id="second", source=source(active), now=LATER)
    assert len(again.generations) == 2


def test_failure_requires_explicit_new_attempt_and_completed_attempt_is_terminal() -> None:
    active = running()
    failed = active.fail(generation_id="generation", now=LATER)
    assert failed.status == "failed" and failed.source is None
    assert failed.generation == active.generation
    with pytest.raises(RenderAttemptConflictError):
        failed.complete(generation_id="generation", source=source(active), now=LATER)
    retry = failed.start(expected_revision=3, job_id="job-2", generation_id="second", now=LATER)
    ready = retry.complete(generation_id="second", source=source(retry), now=LATER)
    with pytest.raises(RenderAttemptConflictError):
        ready.fail(generation_id="second", now=LATER)
    regenerated = ready.start(expected_revision=5, job_id="job-3", generation_id="third", now=LATER)
    assert regenerated.status == "generating" and regenerated.source is None


@pytest.mark.parametrize("operation", ["complete", "fail", "start"])
def test_wrong_state_or_generation_never_accepts_a_result(operation: str) -> None:
    active = running()
    with pytest.raises(RenderAttemptConflictError):
        if operation == "start":
            active.start(expected_revision=2, job_id="other", generation_id="other", now=LATER)
        elif operation == "fail":
            active.fail(generation_id="wrong", now=LATER)
        else:
            unit().complete(generation_id="generation", source=source(active), now=LATER)


@pytest.mark.parametrize(
    "changes",
    [{"unit_id": "other"}, {"job_id": "other"}, {"dependencies_sha256": "sha256:" + "0" * 64}],
)
def test_mismatched_source_is_rejected(changes: dict[str, Any]) -> None:
    active = running()
    with pytest.raises(RenderAttemptConflictError):
        active.complete(generation_id="generation", source=source(active, **changes), now=LATER)


def test_noop_checks_cas_and_time_without_touching_ready_result() -> None:
    active = running()
    ready = active.complete(generation_id="generation", source=source(active), now=LATER)
    assert ready.revise(expected_revision=3, dependencies=dependencies(), now=LATER) is ready
    with pytest.raises(RevisionConflictError) as caught:
        ready.revise(expected_revision=2, dependencies=dependencies(), now=LATER)
    assert (caught.value.expected, caught.value.actual) == (2, 3)
    with pytest.raises(ValueError):
        ready.revise(expected_revision=3, dependencies=dependencies(), now=NOW)


@pytest.mark.parametrize("revision", [True, 0, -1, 1.0, 10**19])
def test_bad_revision_cannot_bypass_cas(revision: Any) -> None:
    with pytest.raises(ValueError):
        RevisionPin("reference", revision)
    with pytest.raises(ValueError):
        unit().revise(expected_revision=revision, dependencies=dependencies(), now=NOW)
    with pytest.raises(ValueError):
        unit().start(expected_revision=revision, job_id="j", generation_id="g", now=NOW)


@pytest.mark.parametrize("text", ["", " ", "a" * 200001, "a\x00", "\ud800", 1, []])
def test_render_text_boundary_is_lossless_and_errors_do_not_echo_content(text: Any) -> None:
    with pytest.raises(ValueError) as caught:
        dependencies(text=text)
    assert "200001" not in str(caught.value) and "\ud800" not in str(caught.value)
    assert len(dependencies(text="😀" * 200000).text) == 200000


@pytest.mark.parametrize("identifier", ["", " ", "i" * 129, "x\x00", "\ud800", 1, []])
def test_reference_ids_validate_without_echo(identifier: Any) -> None:
    with pytest.raises(ValueError):
        RevisionPin(identifier, 1)
    with pytest.raises(ValueError):
        dependencies(chapter_id=identifier)
    with pytest.raises(ValueError):
        RenderSource("unit", "job", identifier, "receipt", dependencies().sha256)


def test_a_source_carries_the_retained_copy_of_its_result() -> None:
    source = RenderSource(
        "unit",
        "job",
        "artifact",
        "receipt",
        dependencies().sha256,
        saved_asset_id="asset-saved-one",
    )
    assert source.saved_asset_id == "asset-saved-one"
    assert (
        RenderSource("unit", "job", "artifact", "receipt", dependencies().sha256).saved_asset_id
        is None
    )


@pytest.mark.parametrize("identifier", ["", " ", "i" * 129, "x\x00", "\ud800", 1, []])
def test_saved_copy_identifier_is_validated_without_echo(identifier: Any) -> None:
    with pytest.raises(ValueError):
        RenderSource(
            "unit",
            "job",
            "artifact",
            "receipt",
            dependencies().sha256,
            saved_asset_id=identifier,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"original": {"id": "a", "revision": 1}},
        {"working": None},
        {"text_plan": []},
        {"voice": "voice"},
        {"model_binding": 1},
        {"pronunciation": {}},
        {"profile": 1},
        {"model": []},
        {"role_id": None},
    ],
)
def test_mutable_or_wrong_dependency_types_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        dependencies(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ready"},
        {"status": "generating"},
        {"status": []},
        {"ordinal": True},
        {"ordinal": -1},
        {"book_id": ""},
        {"id": ""},
        {"dependencies": {}},
        {"generations": []},
        {"generations": ("fake",)},
        {"source": {}},
        {"updated_at": datetime(2026, 10, 24)},
        {"updated_at": None},
    ],
)
def test_direct_construction_cannot_bypass_unit_invariants(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(unit(), **changes)


@pytest.mark.parametrize("now", [NOW, datetime(2026, 10, 24), None])
def test_completion_and_failure_require_aware_monotonic_time(now: Any) -> None:
    active = running()
    with pytest.raises(ValueError):
        active.complete(generation_id="generation", source=source(active), now=now)
    with pytest.raises(ValueError):
        active.fail(generation_id="generation", now=now)


def test_exhaustion_invalid_digest_history_and_bad_completion_source() -> None:
    maximum = replace(unit(), revision=10**19 - 1)
    with pytest.raises(ValueError):
        maximum.start(expected_revision=maximum.revision, job_id="j", generation_id="g", now=NOW)
    active = running()
    assert active.generation is not None
    for changes in (
        {"generations": (active.generation, active.generation)},
        {"generations": (replace(active.generation, revision=3),)},
        {"generations": (replace(active.generation, dependencies_sha256="sha256:" + "a" * 64),)},
        {"source": source(active)},
    ):
        with pytest.raises(ValueError):
            replace(active, **changes)
    with pytest.raises(ValueError):
        active.complete(generation_id="generation", source={}, now=LATER)  # type: ignore[arg-type]
    digests: tuple[object, ...] = ("sha256:x", "SHA256:" + "a" * 64, [], None)
    for digest in digests:
        with pytest.raises(ValueError):
            RenderGeneration("g", "j", digest, 2)  # type: ignore[arg-type]
