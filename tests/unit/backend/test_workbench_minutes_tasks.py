"""Deterministic task state, independent of SQL, model or speech transport."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from speechnexus_backend.workbench.domain.minutes_tasks import (
    MinutesTask,
    MinutesTaskConflictError,
    MinutesTaskFailure,
)


async def test_disabled_tasks_never_read_or_enqueue_content() -> None:
    from speechnexus_backend.workbench.application.llm import LlmError
    from speechnexus_backend.workbench.application.minutes_tasks import (
        MinutesTasks,
        MinutesTaskStore,
    )

    class NoIo:
        def __getattr__(self, _: str) -> object:
            pytest.fail("disabled service reached persistence")

    service = MinutesTasks(cast(MinutesTaskStore, NoIo()))
    with pytest.raises(LlmError, match="disabled"):
        await service.enqueue(
            workspace_id="w", transcript_id="t", source_revision=2, template_revision=1, key="k"
        )
    with pytest.raises(LlmError, match="disabled"):
        await service.execute("j")


async def test_uncertain_final_commit_is_not_rewritten_as_an_llm_failure() -> None:
    from speechnexus_backend.workbench.application.llm import LlmCompletion, LlmProfile, LlmRequest
    from speechnexus_backend.workbench.application.minutes_tasks import (
        MinutesTasks,
        MinutesTaskStore,
        MinutesTaskView,
        MinutesWork,
    )

    profile_value = LlmProfile(model="synthetic", template_revision=1, system_prompt="synthetic")
    waiting = queued()
    work = MinutesWork(
        MinutesTaskView(waiting.claim(token="one", now=LATER), profile_value), "synthetic"
    )

    class Store:
        calls = 0

        async def get(self, task_id: str) -> MinutesTaskView:
            return MinutesTaskView(waiting, profile_value)

        async def claim(self, task_id: str, **kwargs: object) -> MinutesWork:
            return work

        async def finish(self, *args: object, **kwargs: object) -> MinutesTaskView:
            self.calls += 1
            raise RuntimeError("synthetic lost commit reply")

    class Llm:
        profile = profile_value
        binding_id = DIGEST

        async def generate(self, request: LlmRequest) -> LlmCompletion:
            return LlmCompletion(profile_value, "synthetic result")

    store = Store()
    with pytest.raises(RuntimeError, match="synthetic lost commit reply"):
        await MinutesTasks(cast(MinutesTaskStore, store), Llm()).execute("j")
    assert store.calls == 1


NOW = datetime(2026, 10, 24, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)
DIGEST = "sha256:" + "a" * 64


def queued() -> MinutesTask:
    return MinutesTask.create(
        task_id="j",
        workspace_id="w",
        project_id="p",
        transcript_id="t",
        source_revision=2,
        source_revision_id="r2",
        source_sha256=DIGEST,
        profile_sha256=DIGEST,
        binding_id=DIGEST,
        now=NOW,
    )


def test_claim_finish_and_retry_preserve_intent_and_hide_body() -> None:
    initial = queued()
    running = initial.claim(token="attempt-1", now=LATER)
    failed = running.finish(token="attempt-1", text=None, error="unavailable", now=LATER)
    retried = failed.retry(expected_attempt=1, now=LATER)
    second = retried.claim(token="attempt-2", now=LATER)
    done = second.finish(token="attempt-2", text="合成候选", error=None, now=LATER)
    assert (initial.status, running.status, failed.status, retried.status, done.status) == (
        "queued",
        "running",
        "failed",
        "queued",
        "succeeded",
    )
    assert (done.attempt, done.version) == (2, 6)
    assert done.transcript_id == initial.transcript_id and done.profile_sha256 == DIGEST
    assert done.text == "合成候选" and "合成候选" not in repr(done)


def test_running_is_not_reclaimed_and_stale_attempt_cannot_finish() -> None:
    running = queued().claim(token="one", now=LATER)
    with pytest.raises(MinutesTaskConflictError):
        running.claim(token="two", now=LATER)
    with pytest.raises(MinutesTaskConflictError):
        running.finish(token="old", text="late", error=None, now=LATER)
    with pytest.raises(MinutesTaskConflictError):
        running.retry(expected_attempt=1, now=LATER)


def test_cancel_and_expire_never_republish_late_text() -> None:
    running = queued().claim(token="one", now=LATER)
    for terminal in (running.cancel(now=LATER), running.expire(now=LATER)):
        assert terminal.finish(token="one", text="late body", error=None, now=LATER) == terminal
        assert terminal.text is None
        assert terminal.cancel(now=LATER) == terminal
    assert queued().cancel(now=NOW).attempt == 0
    success = running.finish(token="one", text="candidate", error=None, now=LATER)
    assert success.cancel(now=LATER) == success
    assert success.expire(now=LATER).text is None


@pytest.mark.parametrize("bad", [True, 0, -1, 10**19])
def test_rejects_invalid_source_revision(bad: int) -> None:
    with pytest.raises(ValueError):
        replace(queued(), source_revision=bad)


def test_rejects_bad_times_invalid_finish_and_stale_retry() -> None:
    running = queued().claim(token="one", now=LATER)
    for text, error in (
        (None, None),
        ("text", "unavailable"),
        ("", None),
        ("\ud800", None),
        ("\x00", None),
    ):
        with pytest.raises(ValueError):
            running.finish(
                token="one", text=text, error=cast(MinutesTaskFailure | None, error), now=LATER
            )
    with pytest.raises(ValueError):
        running.cancel(now=NOW)
    with pytest.raises(ValueError):
        queued().claim(token="", now=NOW)
    failed = running.finish(token="one", text=None, error="unavailable", now=LATER)
    with pytest.raises(MinutesTaskConflictError):
        failed.retry(expected_attempt=0, now=LATER)


@pytest.mark.parametrize("poll", [True, 0, 0.009, 2.01, float("inf"), float("nan")])
def test_cancel_polling_rejects_unbounded_or_boolean_configuration(poll: float) -> None:
    from speechnexus_backend.workbench.application.minutes_tasks import (
        MinutesTasks,
        MinutesTaskStore,
    )

    with pytest.raises(ValueError):
        MinutesTasks(cast(MinutesTaskStore, object()), poll_seconds=poll)


@pytest.mark.parametrize(
    ("revision", "template", "model", "code"),
    [
        (True, 1, None, "invalid_input"),
        (2, 0, None, "invalid_input"),
        (2, 2, None, "profile_mismatch"),
        (2, 1, "other", "profile_mismatch"),
    ],
)
async def test_bad_minutes_intent_is_rejected_before_persistence(
    revision: int, template: int, model: str | None, code: str
) -> None:
    from speechnexus_backend.workbench.application.llm import LlmError, LlmProfile, TextLlm
    from speechnexus_backend.workbench.application.minutes_tasks import (
        MinutesTasks,
        MinutesTaskStore,
    )

    class Binding:
        profile = LlmProfile(model="synthetic", template_revision=1, system_prompt="synthetic")
        binding_id = DIGEST

    with pytest.raises(LlmError, match=code):
        await MinutesTasks(cast(MinutesTaskStore, object()), cast(TextLlm, Binding())).enqueue(
            workspace_id="w",
            transcript_id="t",
            source_revision=revision,
            template_revision=template,
            llm_model=model,
            key="key",
        )


def test_completed_task_cannot_be_finished_again_by_domain_transition() -> None:
    done = (
        queued()
        .claim(token="one", now=LATER)
        .finish(token="one", text="合成纪要", error=None, now=LATER)
    )
    with pytest.raises(MinutesTaskConflictError):
        done.finish(token="one", text="替换纪要", error=None, now=LATER)
