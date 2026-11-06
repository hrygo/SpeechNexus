"""Adapter reads for retry lineage, event pages and result Artifacts.

The fake pool proves the SQL projection and the transaction boundary; the
PostgreSQL integration tests prove the real statements.
"""

from __future__ import annotations

import json
from typing import Any, cast

import psycopg
import pytest

from speechnexus_backend.speech.domain.public_jobs import (
    PublicJobConflictError,
    PublicJobUnavailableError,
    canonical_digest,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from tests.unit.backend.test_public_job_pin_postgres import Pool, Rows, _public_row, _request
from tests.unit.backend.test_public_jobs import NOW

DOCUMENT = json.dumps({"kind": "tts", "model": "tts-quality"})
DIGEST = canonical_digest({"kind": "tts", "model": "tts-quality"})


class ReadPool(Pool):
    """Route the four read statements and the advance transaction by shape."""

    def __init__(self, request: Any) -> None:
        super().__init__(request)
        self.intent: list[dict[str, Any]] = []
        self.event_rows: list[dict[str, Any]] = []
        self.artifact_rows: list[dict[str, Any]] = []
        self.job_row: dict[str, Any] = _public_row(request)
        self.successful_update = 1
        self.fail_with: Exception | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        self.calls.append((query, parameters))
        if self.fail_with is not None and query.startswith("INSERT INTO speech_job_events"):
            raise self.fail_with
        if "LEFT JOIN admission_operations" in query:
            return Rows(self.intent)
        if query.startswith("SELECT event_seq, state, occurred_at_us"):
            return Rows(self.event_rows)
        if "FROM speech_artifacts" in query:
            return Rows(self.artifact_rows)
        if query.startswith("UPDATE speech_jobs SET state"):
            result = Rows([])
            result.rowcount = self.successful_update
            if self.successful_update == 1:
                self.job_row = {
                    **self.job_row,
                    "state": parameters[0],
                    "result_json": parameters[1],
                    "error_code": parameters[2],
                    "updated_at_us": parameters[3],
                    "status_version": int(self.job_row["status_version"]) + 1,
                }
            return result
        if "INSERT INTO speech_job_events" in query:
            return Rows([])
        if "WHERE j.job_id = %s" in query:
            return Rows([self.job_row])
        return Rows([])


def _driver(pool: ReadPool) -> PostgresPublicJobs:
    return PostgresPublicJobs(cast(SpeechPool, pool))


def test_intent_document_returns_the_redacted_and_intact_cases() -> None:
    request = _request()
    pool = ReadPool(request)
    pool.intent = [{"request_digest": DIGEST, "request_document": DOCUMENT}]
    intent = _driver(pool).intent_document(request.job_id)
    assert intent is not None
    assert intent.request_digest == DIGEST and intent.document == DOCUMENT

    pool.intent = [{"request_digest": DIGEST, "request_document": None}]
    assert _driver(pool).intent_document(request.job_id) is None

    pool.intent = [{"request_digest": "sha256:" + "a" * 64, "request_document": DOCUMENT}]
    with pytest.raises(PublicJobUnavailableError):
        _driver(pool).intent_document(request.job_id)

    pool.intent = []
    with pytest.raises(PublicJobUnavailableError):
        _driver(pool).intent_document(request.job_id)


def test_events_reads_strictly_after_the_cursor_with_a_bounded_page() -> None:
    request = _request()
    pool = ReadPool(request)
    pool.event_rows = [
        {
            "event_seq": 2,
            "state": "failed",
            "occurred_at_us": 3,
            "kind": "lifecycle",
            "progress_unit": None,
            "progress_completed": None,
            "progress_total": None,
        },
        {
            "event_seq": 3,
            "state": "cancelled",
            "occurred_at_us": 4,
            "kind": "lifecycle",
            "progress_unit": None,
            "progress_completed": None,
            "progress_total": None,
        },
    ]
    events = _driver(pool).events(request.job_id, after_seq=1, limit=100)
    assert [event.event_seq for event in events] == [2, 3]
    query, parameters = next(call for call in pool.calls if "FROM speech_job_events" in call[0])
    assert "event_seq > %s" in query and "ORDER BY event_seq ASC LIMIT %s" in query
    assert parameters == (request.job_id, 1, 100)

    pool.event_rows = [
        {
            "event_seq": 1,
            "state": "paused",
            "occurred_at_us": 2,
            "kind": "lifecycle",
            "progress_unit": None,
            "progress_completed": None,
            "progress_total": None,
        }
    ]
    with pytest.raises(PublicJobUnavailableError):
        _driver(pool).events(request.job_id, after_seq=0, limit=100)


def test_artifact_only_resolves_a_ready_row_with_a_receipt() -> None:
    request = _request()
    pool = ReadPool(request)
    row = {
        "artifact_id": "artifact-unit",
        "artifact_kind": "audio",
        "artifact_state": "ready",
        "media_type": "audio/wav",
        "bytes": 128,
        "sha256": "sha256:" + "d" * 64,
        "receipt_id": "receipt-unit",
        "expires_at_us": None,
    }
    pool.artifact_rows = [row]
    fact = _driver(pool).artifact(request.workspace_id, "artifact-unit")
    assert fact is not None and fact.receipt_id == "receipt-unit"
    assert fact.expires_at_us is None

    pool.artifact_rows = []
    assert _driver(pool).artifact(request.workspace_id, "artifact-unit") is None

    pool.artifact_rows = [{**row, "artifact_state": "staging"}]
    assert _driver(pool).artifact(request.workspace_id, "artifact-unit") is None

    pool.artifact_rows = [{**row, "receipt_id": None}]
    assert _driver(pool).artifact(request.workspace_id, "artifact-unit") is None

    pool.artifact_rows = [{**row, "artifact_kind": "unknown"}]
    with pytest.raises(PublicJobUnavailableError):
        _driver(pool).artifact(request.workspace_id, "artifact-unit")


def test_advance_commits_the_transition_and_its_event_together() -> None:
    request = _request()
    pool = ReadPool(request)
    pool.job_row = {**pool.job_row, "state": "running", "status_version": 2}
    current = _driver(pool).get(request.job_id)
    assert current is not None and current.state == "running"
    pool.calls.clear()
    updated = _driver(pool).advance(
        current, "failed", result_json=None, error_code="engine_failure", now_us=NOW + 5
    )
    assert updated.state == "failed" and updated.error_code == "engine_failure"
    assert updated.status_version == 3
    queries = [query for query, _ in pool.calls]
    update = next(i for i, query in enumerate(queries) if query.startswith("UPDATE speech_jobs"))
    insert = next(
        i for i, query in enumerate(queries) if query.startswith("INSERT INTO speech_job_events")
    )
    assert update < insert
    parameters = next(
        parameters
        for query, parameters in pool.calls
        if query.startswith("INSERT INTO speech_job_events")
    )
    assert parameters == (current.job_id, "failed", NOW + 5, current.job_id)

    pool.successful_update = 0
    before = len(pool.calls)
    with pytest.raises(PublicJobConflictError):
        _driver(pool).advance(
            current, "failed", result_json=None, error_code="engine_failure", now_us=NOW + 6
        )
    assert not any(
        query.startswith("INSERT INTO speech_job_events") for query, _ in pool.calls[before:]
    )

    pool.successful_update = 1
    pool.fail_with = psycopg.OperationalError("synthetic")
    with pytest.raises(PublicJobUnavailableError):
        _driver(pool).advance(
            current, "failed", result_json=None, error_code="engine_failure", now_us=NOW + 7
        )
