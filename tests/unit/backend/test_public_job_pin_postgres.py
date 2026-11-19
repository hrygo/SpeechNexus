"""Fault cursors exercise the production adapter; no real PG concurrency evidence."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import pytest
import rfc8785

from speechnexus_backend.speech.application.public_jobs import ModelBindingError
from speechnexus_backend.speech.domain.public_jobs import (
    JobSubmission,
    NewPublicJob,
    canonical_digest,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    PostgresPublicJobs,
    _lock_validation_target,
    _validate_pinned_create,
)
from tests.unit.backend.test_public_jobs import BINDING, NOW, _pin_facts, _pinned_body, _service


def _request() -> NewPublicJob:
    service, facts, store = _service()
    _pin_facts(facts)
    service.submit(_pinned_body(), request_id="req-pin", idempotency_key="idem-pin-0001")
    return store.requests[0]


def _public_row(request: NewPublicJob) -> dict[str, Any]:
    return {
        **{
            name: getattr(request, name)
            for name in ("job_id", "workspace_id", "kind", "model_id", "request_digest")
        },
        "state": "pending",
        "status_version": 1,
        "result_json": None,
        "error_code": None,
        "created_at_us": NOW,
        "updated_at_us": NOW,
        **{
            name: getattr(request.submission, name)
            for name in (
                "request_id",
                "client_reference",
                "effective_configuration_json",
                "execution_revision",
                "progress_unit",
                "progress_total",
                "submitted_at_us",
            )
        },
    }


class Rows:
    rowcount = 1

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class Pool:
    def __init__(self, request: NewPublicJob) -> None:
        self.request = request
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.commits = self.rollbacks = 0
        self.replay_after_lock = False
        self.locked = False
        self.binding_rows = [
            {
                "kind": "binding",
                "object_id": BINDING.object_id,
                "revision": str(BINDING.revision),
                "revision_id": BINDING.revision_id,
                "published_at_us": BINDING.published_at_us,
                # Actual draft producer stores identity in columns, not this JSON.
                "definition_json": json.dumps(
                    {"logical_model": "narrator-tts", "tasks": ["tts_file"]}
                ),
                "is_current": True,
                "is_retired": False,
            }
        ]

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        try:
            yield self
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        self.calls.append((query, parameters))
        if "speechnexus:catalog:" in query:
            self.locked = True
        if query.startswith("SELECT h.revision"):
            return Rows([{"revision": str(BINDING.revision)}])
        if "AS is_current" in query:
            return Rows(self.binding_rows)
        if "j.idempotency_key_digest = %s" in query:
            return Rows(
                [_public_row(self.request)] if self.replay_after_lock and self.locked else []
            )
        if "WHERE j.job_id = %s" in query:
            return Rows([_public_row(self.request)])
        return Rows([])


def _driver(pool: Pool) -> PostgresPublicJobs:
    return PostgresPublicJobs(cast(SpeechPool, pool))


def test_exact_lookup_keeps_decimal_text_and_three_column_identity() -> None:
    pool = Pool(_request())
    _driver(pool).binding_revision("binding-narrator", 9_999_999_999_999_999_999)
    query, parameters = next(call for call in pool.calls if "AS is_current" in call[0])
    assert parameters == ("binding-narrator", "9999999999999999999")
    assert "r.revision_id = p.revision_id" in query and "r.revision = p.revision" in query
    assert "p.revision::bigint" not in query


def test_create_locks_then_revalidates_before_any_insert_and_keeps_original_pin() -> None:
    request = _request()
    pool = Pool(request)
    result = _driver(pool).create(request)
    assert result.outcome == "created"
    queries = [query for query, _ in pool.calls]
    key_lock = next(i for i, query in enumerate(queries) if "speechnexus:public-job-key:" in query)
    lock = next(i for i, query in enumerate(queries) if "speechnexus:catalog:" in query)
    validation = next(i for i, query in enumerate(queries) if "AS is_current" in query)
    first_insert = next(i for i, query in enumerate(queries) if query.startswith("INSERT"))
    assert key_lock < lock < validation < first_insert
    assert "'speechnexus:catalog:' || current_schema()" in queries[lock]
    original = next(
        parameters
        for query, parameters in pool.calls
        if query.startswith("INSERT INTO admission_operations")
    )
    assert json.loads(str(original[3]))["model_binding"] == BINDING.reference
    assert pool.commits == 1 and pool.rollbacks == 0


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("is_retired", "model_binding_retired"),
        ("is_current", "model_binding_not_current"),
    ],
)
def test_visibility_changed_since_resolution_rolls_back_without_insert(
    field: str, code: str
) -> None:
    pool = Pool(_request())
    pool.binding_rows[0][field] = field == "is_retired"
    with pytest.raises(ModelBindingError, match=code):
        _driver(pool).create(pool.request)
    assert not any(query.startswith("INSERT") for query, _ in pool.calls)
    assert pool.rollbacks == 1 and pool.commits == 0


def test_replay_discovered_after_lock_beats_retirement_and_has_no_write() -> None:
    pool = Pool(_request())
    pool.replay_after_lock = True
    pool.binding_rows[0]["is_retired"] = True
    result = _driver(pool).create(pool.request)
    assert result.outcome == "replayed" and result.job is not None
    assert not any(
        query.startswith("INSERT") or "AS is_current" in query for query, _ in pool.calls
    )


def test_stored_revision_identity_changed_fails_closed() -> None:
    pool = Pool(_request())
    pool.binding_rows[0]["revision_id"] = "different-private-revision"
    with pytest.raises(ModelBindingError, match="model_binding_not_current"):
        _driver(pool).create(pool.request)
    assert pool.rollbacks == 1
    assert not any(query.startswith("INSERT") for query, _ in pool.calls)


def test_domain_refuses_dropped_pin_expectation() -> None:
    with pytest.raises(ValueError, match="binding disagrees"):
        replace(_request(), expected_binding=None)


PREVIEW_BINDING = Publication("binding", "voice-model", 1, "binding-preview", 1)


def _preview_job() -> NewPublicJob:
    """A pinned preview whose task must come from the target draft, not its row."""
    document: dict[str, Any] = {
        "kind": "voice_preview",
        "model": "voice-model",
        "model_binding": PREVIEW_BINDING.reference,
        "input": {"voice_draft_id": "voice-draft", "draft_revision": "1", "text": "hello"},
        "workspace_id": "workspace-default",
    }
    return NewPublicJob(
        job_id="job-preview",
        workspace_id="workspace-default",
        kind="voice_preview",
        model_id="voice-model",
        request_digest=canonical_digest(document),
        request_document=rfc8785.dumps(document).decode(),
        initial_event_digest="sha256:" + "a" * 64,
        idempotency_key_digest=None,
        submission=JobSubmission(
            request_id="req-preview",
            client_reference=None,
            effective_configuration_json=json.dumps(
                {"models": {"voice": PREVIEW_BINDING.reference}}
            ),
            execution_revision=1,
            progress_unit="items",
            progress_total=None,
            submitted_at_us=NOW,
        ),
        expected_binding=PREVIEW_BINDING,
    )


class _PinnedRows:
    def __init__(self, draft_kind: str, tasks: list[str]) -> None:
        self.draft_kind, self.tasks = draft_kind, tasks
        self.calls: list[str] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        self.calls.append(query)
        if query.startswith("SELECT kind FROM speech_voice_drafts"):
            return Rows([{"kind": self.draft_kind}])
        return Rows(
            [
                {
                    "kind": "binding",
                    "object_id": PREVIEW_BINDING.object_id,
                    "revision": str(PREVIEW_BINDING.revision),
                    "revision_id": PREVIEW_BINDING.revision_id,
                    "published_at_us": PREVIEW_BINDING.published_at_us,
                    "definition_json": json.dumps(
                        {"logical_model": "voice-model", "tasks": self.tasks}
                    ),
                    "is_current": True,
                    "is_retired": False,
                }
            ]
        )


def test_a_preview_is_validated_against_the_draft_task_and_the_voice_slot() -> None:
    connection = _PinnedRows("clone", ["voice_clone"])
    _validate_pinned_create(cast(Any, connection), _preview_job())
    assert any(
        query.startswith("SELECT kind FROM speech_voice_drafts") for query in connection.calls
    )


def test_a_preview_on_a_design_draft_needs_the_design_task() -> None:
    connection = _PinnedRows("design", ["voice_clone"])
    with pytest.raises(ModelBindingError, match="model_binding_task_not_supported"):
        _validate_pinned_create(cast(Any, connection), _preview_job())


def _validation_job() -> NewPublicJob:
    """A validation create; its draft task comes from the target revision."""
    document: dict[str, Any] = {
        "kind": "voice_validation",
        "model": "voice-model",
        "input": {
            "voice_draft_id": "voice-draft",
            "draft_revision": "1",
            "validation_set_revision": "1",
        },
        "workspace_id": "workspace-default",
    }
    return NewPublicJob(
        job_id="job-validation",
        workspace_id="workspace-default",
        kind="voice_validation",
        model_id="voice-model",
        request_digest=canonical_digest(document),
        request_document=rfc8785.dumps(document).decode(),
        initial_event_digest="sha256:" + "b" * 64,
        idempotency_key_digest=None,
        submission=JobSubmission(
            request_id="req-validation",
            client_reference=None,
            effective_configuration_json=json.dumps(
                {"models": {"voice": {"id": "voice-model", "revision": "1"}}}
            ),
            execution_revision=1,
            progress_unit="items",
            progress_total=None,
            submitted_at_us=NOW,
        ),
        expected_binding=None,
    )


class _ValidationRows:
    def __init__(self, draft_kind: str) -> None:
        self.draft_kind = draft_kind
        self.calls: list[str] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        self.calls.append(query)
        return Rows(
            [
                {
                    "head_revision": "1",
                    "status": "generating",
                    "draft_kind": self.draft_kind,
                    "generation_state": "succeeded",
                    "generation_result": "{}",
                    "generation_workspace": "workspace-default",
                }
            ]
        )


def test_a_preview_target_outside_the_vocabulary_is_a_binding_refusal() -> None:
    connection = _PinnedRows("sketch", [])
    with pytest.raises(ModelBindingError, match="model_binding_not_current"):
        _validate_pinned_create(cast(Any, connection), _preview_job())


def test_a_validation_target_outside_the_vocabulary_is_unavailable() -> None:
    connection = _ValidationRows("sketch")
    assert _lock_validation_target(cast(Any, connection), _validation_job()) == "job_unavailable"
