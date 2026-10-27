"""Authoritative public Job facts and one bounded atomic create transaction."""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.public_jobs import (
    BindingFact,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCreateResult,
    JobState,
    JobSubmission,
    NewPublicJob,
    PublicJob,
    PublicJobConflictError,
    PublicJobUnavailableError,
    identifier,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
)

_T = TypeVar("_T")
_JOB_COLUMNS = (
    "j.job_id, j.workspace_id, j.kind, j.model_id, j.request_digest, j.state, "
    "j.status_version, "
    "j.result_json, j.error_code, j.created_at_us, j.updated_at_us, "
    "s.request_id, s.client_reference, s.effective_configuration_json, "
    "s.execution_revision, s.progress_unit, s.progress_total, s.submitted_at_us"
)


class PostgresPublicJobs:
    """Implements the narrow Job facts and store ports with explicit deadlines."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Public Job transactions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def now_us(self) -> int:
        return time.time_ns() // 1_000

    def workspace(self, workspace_id: str) -> WorkspaceFact | None:
        rows = self._read(
            "SELECT workspace_state, policy_revision FROM speech_workspaces "
            "WHERE workspace_id = %s",
            (identifier(workspace_id),),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return WorkspaceFact(
            int(rows[0]["policy_revision"]), rows[0]["workspace_state"] == "active"
        )

    def latest_binding(self, model_id: str) -> BindingFact | None:
        rows = self._read(
            "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us, "
            "r.definition_json FROM speech_publications AS p "
            "JOIN speech_binding_revisions AS r ON r.binding_id = p.object_id "
            "AND r.revision = p.revision AND r.revision_id = p.revision_id "
            "WHERE p.kind = 'binding' AND p.object_id = %s "
            "ORDER BY p.revision::bigint DESC LIMIT 1",
            (model_id,),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        row = rows[0]
        try:
            definition = json.loads(row["definition_json"])
        except json.JSONDecodeError, RecursionError:
            raise PublicJobUnavailableError from None
        if not isinstance(definition, dict):
            raise PublicJobUnavailableError
        return BindingFact(_publication(row), definition)

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None:
        rows = self._read(
            "SELECT kind, object_id, revision, revision_id, published_at_us "
            "FROM speech_publications WHERE kind = %s AND object_id = %s AND revision = %s",
            (kind, object_id, str(revision)),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return _publication(rows[0])

    def pronunciation_exists(self, object_id: str, revision: int) -> bool:
        rows = self._read(
            "SELECT 1 AS present FROM speech_pronunciation_revisions "
            "WHERE pronunciation_id = %s AND revision = %s",
            (object_id, str(revision)),
        )
        return len(rows) == 1

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None:
        rows = self._read(
            "SELECT asset_state, purpose FROM speech_assets "
            "WHERE workspace_id = %s AND asset_id = %s",
            (workspace_id, asset_id),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return rows[0]["asset_state"], rows[0]["purpose"]

    def catalog_revision(self) -> int:
        rows = self._read("SELECT count(*) AS total FROM speech_publications", ())
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return int(rows[0]["total"])

    def get(self, job_id: str) -> PublicJob | None:
        rows = self._read(
            "SELECT " + _JOB_COLUMNS + " FROM speech_jobs AS j "
            "LEFT JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
            "WHERE j.job_id = %s",
            (identifier(job_id),),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return _job(rows[0])

    def create(self, job: NewPublicJob) -> JobCreateResult:
        deadline = time.monotonic() + self._timeout
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return JobCreateResult("unknown", error_code="outcome_unknown")
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return JobCreateResult("unknown", error_code="outcome_unknown")
                    connection.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(remaining * 1000))),),
                    )
                    if job.idempotency_key_digest is not None:
                        existing = _existing_by_key(connection, job)
                        if existing is not None:
                            return _replay_or_conflict(existing, job)
                    _insert(connection, job)
                    row = _select_job(connection, job.job_id)
                return JobCreateResult("created", _job(row))
            except psycopg.errors.UniqueViolation:
                if job.idempotency_key_digest is None:
                    return JobCreateResult("rejected", error_code="job_identity_conflict")
                if attempt == 0:
                    continue
                return JobCreateResult("unknown", error_code="outcome_unknown")
            except psycopg.OperationalError, psycopg.InterfaceError:
                return JobCreateResult("unknown", error_code="outcome_unknown")
            except psycopg.Error:
                return JobCreateResult("rejected", error_code="job_rejected")
            except PoolClosed, PoolTimeout, TooManyRequests:
                return JobCreateResult("rejected", error_code="job_unavailable")
        return JobCreateResult("unknown", error_code="outcome_unknown")

    def advance(
        self,
        current: PublicJob,
        target: JobState,
        *,
        result_json: str | None,
        error_code: str | None,
        now_us: int,
    ) -> PublicJob:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicJobUnavailableError
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                changed = connection.execute(
                    "UPDATE speech_jobs SET state = %s, status_version = status_version + 1, "
                    "result_json = %s, error_code = %s, updated_at_us = %s "
                    "WHERE job_id = %s AND status_version = %s AND state = %s",
                    (
                        target,
                        result_json,
                        error_code,
                        now_us,
                        current.job_id,
                        current.status_version,
                        current.state,
                    ),
                ).rowcount
                if changed != 1:
                    raise PublicJobConflictError("Public Job version changed")
                row = _select_job(connection, current.job_id)
            return _job(row)
        except PublicJobConflictError, PublicJobUnavailableError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise PublicJobUnavailableError from None

    def _read(self, query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicJobUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                rows = connection.execute(query, parameters).fetchall()
                if time.monotonic() >= deadline:
                    raise PublicJobUnavailableError
            return rows
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
            PublicationUnavailableError,
        ):
            raise PublicJobUnavailableError from None


def _publication(row: dict[str, Any]) -> Publication:
    try:
        return Publication(
            kind=row["kind"],
            object_id=row["object_id"],
            revision=int(row["revision"]),
            revision_id=row["revision_id"],
            published_at_us=int(row["published_at_us"]),
        )
    except ValueError, TypeError, KeyError:
        raise PublicJobUnavailableError from None


def _job(row: dict[str, Any] | None) -> PublicJob:
    if row is None or row.get("request_id") is None:
        raise PublicJobUnavailableError
    try:
        submission = JobSubmission(
            request_id=row["request_id"],
            client_reference=row["client_reference"],
            effective_configuration_json=row["effective_configuration_json"],
            execution_revision=int(row["execution_revision"]),
            progress_unit=row["progress_unit"],
            progress_total=row["progress_total"],
            submitted_at_us=int(row["submitted_at_us"]),
        )
        return PublicJob(
            job_id=row["job_id"],
            workspace_id=row["workspace_id"],
            kind=row["kind"],
            model_id=row["model_id"],
            state=row["state"],
            status_version=int(row["status_version"]),
            result_json=row["result_json"],
            error_code=row["error_code"],
            created_at_us=int(row["created_at_us"]),
            updated_at_us=int(row["updated_at_us"]),
            submission=submission,
        )
    except ValueError, TypeError, KeyError:
        raise PublicJobUnavailableError from None


def _select_job(connection: SpeechConnection, job_id: str) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT " + _JOB_COLUMNS + " FROM speech_jobs AS j "
        "LEFT JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
        "WHERE j.job_id = %s",
        (job_id,),
    ).fetchall()
    if len(rows) > 1:
        raise psycopg.errors.InternalError("Public Job identity is not unique")
    return rows[0] if rows else None


def _existing_by_key(connection: SpeechConnection, job: NewPublicJob) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT " + _JOB_COLUMNS + " FROM speech_jobs AS j "
        "JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
        "WHERE j.workspace_id = %s AND j.idempotency_key_digest = %s",
        (job.workspace_id, job.idempotency_key_digest),
    ).fetchall()
    if len(rows) > 1:
        raise psycopg.errors.InternalError("Idempotency identity is not unique")
    return rows[0] if rows else None


def _replay_or_conflict(row: dict[str, Any], job: NewPublicJob) -> JobCreateResult:
    if row.get("request_digest") != job.request_digest:
        return JobCreateResult("conflict", error_code="idempotency_conflict")
    return JobCreateResult("replayed", _job(row))


def _insert(connection: SpeechConnection, job: NewPublicJob) -> None:
    submission = job.submission
    attempt_id = job.job_id + "-a1"
    connection.execute(
        "INSERT INTO speech_jobs "
        "(job_id, workspace_id, kind, model_id, request_digest, idempotency_key_digest, "
        "state, status_version, result_json, error_code, created_at_us, updated_at_us) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'pending', 1, NULL, NULL, %s, %s)",
        (
            job.job_id,
            job.workspace_id,
            job.kind,
            job.model_id,
            job.request_digest,
            job.idempotency_key_digest,
            submission.submitted_at_us,
            submission.submitted_at_us,
        ),
    )
    connection.execute(
        "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
        "VALUES (%s, 0, 'pending', %s)",
        (job.job_id, submission.submitted_at_us),
    )
    connection.execute(
        "INSERT INTO speech_job_submissions "
        "(job_id, request_id, client_reference, effective_configuration_json, "
        "execution_revision, progress_unit, progress_total, submitted_at_us) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            job.job_id,
            submission.request_id,
            submission.client_reference,
            submission.effective_configuration_json,
            str(submission.execution_revision),
            submission.progress_unit,
            submission.progress_total,
            submission.submitted_at_us,
        ),
    )
    # Private original command: the dispatcher may rebuild the engine request
    # only from this canonical document, never from a digest or current default.
    connection.execute(
        "INSERT INTO admission_operations "
        "(operation_id, workspace_id, request_digest, request_document, "
        "initial_attempt_id, current_attempt_id, recorded_at_us) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            job.job_id,
            job.workspace_id,
            job.request_digest,
            job.request_document,
            attempt_id,
            attempt_id,
            max(1, submission.submitted_at_us),
        ),
    )
    # Coordination claim/outbox identity is committed with the public Job. The
    # claim row stays pending until the durable terminal transition.
    connection.execute(
        "INSERT INTO coordination_jobs "
        "(job_id, operation_id, attempt_id, initial_event_id, initial_event_digest, "
        "request_digest) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            job.job_id,
            job.job_id,
            attempt_id,
            job.job_id + "-queued",
            job.initial_event_digest,
            job.request_digest,
        ),
    )
    connection.execute(
        "INSERT INTO coordination_outbox (event_id, job_id, event_digest) VALUES (%s, %s, %s)",
        (job.job_id + "-queued", job.job_id, job.initial_event_digest),
    )
