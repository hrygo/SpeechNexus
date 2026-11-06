"""Authoritative public Job facts and one bounded atomic create transaction."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.public_jobs import (
    BindingFact,
    WorkspaceFact,
    require_pinned_binding,
)
from speechnexus_backend.speech.domain.public_jobs import (
    TERMINAL_STATES,
    JobArtifactFact,
    JobCancellationReceipt,
    JobCancellationResult,
    JobCreateResult,
    JobEventFact,
    JobIntent,
    JobProgressFact,
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
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import lock_binding
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import (
    RETIREMENT_TABLE,
    STATE_TABLE,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence import (
    write_cancellation_fence,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
)

_LATEST_PROGRESS = (
    "(SELECT e.progress_{column} FROM speech_job_events AS e "
    "WHERE e.job_id = j.job_id AND e.kind = 'progress' "
    "ORDER BY e.event_seq DESC LIMIT 1) AS latest_progress_{column}"
)
_JOB_COLUMNS = (
    "j.job_id, j.workspace_id, j.kind, j.model_id, j.request_digest, j.state, "
    "j.status_version, "
    "j.result_json, j.error_code, j.created_at_us, j.updated_at_us, j.retry_of_job_id, "
    "s.request_id, s.client_reference, s.effective_configuration_json, "
    "s.execution_revision, s.progress_unit, s.progress_total, s.submitted_at_us, "
    + ", ".join(_LATEST_PROGRESS.format(column=column) for column in ("unit", "completed", "total"))
)
_PINNED_BINDING_QUERY = (
    "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us, "
    "r.definition_json, EXISTS ("
    "SELECT 1 FROM speech_publication_heads AS h "
    "WHERE h.kind = p.kind AND h.object_id = p.object_id "
    "AND h.revision = p.revision AND h.revision_id = p.revision_id) AS is_current, "
    "EXISTS ("
    f"SELECT 1 FROM {RETIREMENT_TABLE} AS retired "
    "WHERE retired.kind = p.kind AND retired.object_id = p.object_id "
    "AND retired.revision = p.revision AND retired.revision_id = p.revision_id) AS is_retired "
    "FROM speech_publications AS p "
    "JOIN speech_binding_revisions AS r ON r.binding_id = p.object_id "
    "AND r.revision = p.revision AND r.revision_id = p.revision_id "
    "WHERE p.kind = 'binding' AND p.object_id = %s AND p.revision = %s"
)


class _Rows(Protocol):
    @property
    def rowcount(self) -> int: ...
    def fetchall(self) -> list[dict[str, Any]]: ...
    def fetchone(self) -> dict[str, Any] | None: ...


class _Executor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Rows: ...


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublicJobUnavailableError
    return remaining


class _DeadlineRows:
    def __init__(self, raw: _Rows, deadline: float) -> None:
        self.raw, self.deadline = raw, deadline

    @property
    def rowcount(self) -> int:
        _remaining(self.deadline)
        return self.raw.rowcount

    def fetchall(self) -> list[dict[str, Any]]:
        _remaining(self.deadline)
        rows = self.raw.fetchall()
        _remaining(self.deadline)
        return rows

    def fetchone(self) -> dict[str, Any] | None:
        _remaining(self.deadline)
        row = self.raw.fetchone()
        _remaining(self.deadline)
        return row


class _DeadlineConnection:
    def __init__(self, raw: SpeechConnection, deadline: float) -> None:
        self.raw, self.deadline = raw, deadline

    def arm(self) -> None:
        remaining = _remaining(self.deadline)
        self.raw.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        # Control round trips also consume time. Refuse the next SQL/COMMIT
        # if they exhausted it; this is not a hard network/COMMIT interrupter.
        _remaining(self.deadline)

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _DeadlineRows:
        self.arm()
        rows = self.raw.execute(query, parameters)
        _remaining(self.deadline)
        return _DeadlineRows(rows, self.deadline)


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
            "JOIN speech_publication_heads AS h "
            "  ON h.kind = p.kind AND h.object_id = p.object_id "
            " AND h.revision = p.revision AND h.revision_id = p.revision_id "
            "JOIN speech_binding_revisions AS r ON r.binding_id = p.object_id "
            "AND r.revision = p.revision AND r.revision_id = p.revision_id "
            "WHERE h.kind = 'binding' AND h.object_id = %s "
            "AND NOT EXISTS ("
            f"SELECT 1 FROM {RETIREMENT_TABLE} AS retired "
            "WHERE retired.kind = 'binding' AND retired.object_id = p.object_id "
            "AND retired.revision = p.revision "
            "AND retired.revision_id = p.revision_id"
            ")",
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

    def binding_revision(self, binding_id: str, revision: int) -> BindingFact | None:
        return _pinned_binding(self._read(_PINNED_BINDING_QUERY, (binding_id, str(revision))))

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None:
        rows = self._read(
            "SELECT kind, object_id, revision, revision_id, published_at_us "
            "FROM speech_publications AS p WHERE kind = %s AND object_id = %s AND revision = %s "
            "AND NOT EXISTS ("
            f"SELECT 1 FROM {RETIREMENT_TABLE} AS retired "
            "WHERE retired.kind = p.kind AND retired.object_id = p.object_id "
            "AND retired.revision = p.revision AND retired.revision_id = p.revision_id"
            ")",
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
        rows = self._read(
            f"SELECT revision AS total FROM {STATE_TABLE} WHERE singleton IS TRUE", ()
        )
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return int(rows[0]["total"])

    def intent_document(self, job_id: str) -> JobIntent | None:
        """Read the private canonical intent; None means it was redacted, never invented."""
        rows = self._read(
            "SELECT j.request_digest, a.request_document FROM speech_jobs AS j "
            "LEFT JOIN admission_operations AS a ON a.operation_id = j.job_id "
            "WHERE j.job_id = %s",
            (identifier(job_id),),
        )
        if len(rows) != 1:
            raise PublicJobUnavailableError
        document = rows[0]["request_document"]
        if document is None:
            return None
        try:
            return JobIntent(request_digest=rows[0]["request_digest"], document=document)
        except ValueError, TypeError, KeyError:
            raise PublicJobUnavailableError from None

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        """Read one ordered page of persisted events strictly after a sequence."""
        rows = self._read(
            "SELECT event_seq, state, occurred_at_us, kind, progress_unit, "
            "progress_completed, progress_total FROM speech_job_events "
            "WHERE job_id = %s AND event_seq > %s ORDER BY event_seq ASC LIMIT %s",
            (identifier(job_id), after_seq, limit),
        )
        try:
            return tuple(
                JobEventFact(
                    event_seq=int(row["event_seq"]),
                    state=row["state"],
                    occurred_at_us=int(row["occurred_at_us"]),
                    kind=row["kind"],
                    progress=_progress(row),
                )
                for row in rows
            )
        except ValueError, TypeError, KeyError:
            raise PublicJobUnavailableError from None

    def artifact(self, workspace_id: str, artifact_id: str) -> JobArtifactFact | None:
        """Read one committed Artifact row; absence is explicit."""
        rows = self._read(
            "SELECT artifact_id, artifact_kind, artifact_state, media_type, bytes, "
            "sha256, receipt_id, expires_at_us FROM speech_artifacts "
            "WHERE workspace_id = %s AND artifact_id = %s",
            (identifier(workspace_id), identifier(artifact_id)),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        row = rows[0]
        if row["artifact_state"] != "ready" or row["receipt_id"] is None:
            return None
        try:
            return JobArtifactFact(
                artifact_id=row["artifact_id"],
                kind=row["artifact_kind"],
                media_type=row["media_type"],
                bytes=int(row["bytes"]),
                sha256=row["sha256"],
                receipt_id=row["receipt_id"],
                expires_at_us=(None if row["expires_at_us"] is None else int(row["expires_at_us"])),
            )
        except ValueError, TypeError, KeyError:
            raise PublicJobUnavailableError from None

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

    def list_jobs(
        self,
        *,
        workspace_id: str,
        state: str | None,
        kind: str | None,
        after: tuple[int, str] | None,
        limit: int,
    ) -> tuple[PublicJob, ...]:
        # Fetch one lookahead row, never OFFSET or an unbounded in-memory sort.
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("Invalid Job read bound")
        predicates = ["j.workspace_id = %s"]
        parameters: list[object] = [identifier(workspace_id)]
        if state is not None:
            predicates.append("j.state = %s")
            parameters.append(state)
        if kind is not None:
            predicates.append("j.kind = %s")
            parameters.append(kind)
        if after is not None:
            predicates.append("(j.created_at_us, j.job_id) > (%s, %s)")
            parameters.extend(after)
        parameters.append(limit)
        rows = self._read(
            "SELECT " + _JOB_COLUMNS + " FROM speech_jobs AS j "
            "LEFT JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
            "WHERE "
            + " AND ".join(predicates)
            + " ORDER BY j.created_at_us ASC, j.job_id ASC LIMIT %s",
            tuple(parameters),
        )
        return tuple(_job(row) for row in rows)

    def cancellation_receipt(self, job_id: str) -> JobCancellationReceipt | None:
        """Read the accepted receipt used by the reconciliation stop adapter."""
        rows = self._read(
            "SELECT " + _CANCELLATION_COLUMNS + " FROM speech_job_cancellations WHERE job_id = %s",
            (identifier(job_id),),
        )
        if len(rows) > 1:
            raise PublicJobUnavailableError
        return _cancellation(rows[0]) if rows else None

    def find_by_key(
        self,
        workspace_id: str,
        key_digest: str,
        request_digest: str,
        retry_of_job_id: str | None,
    ) -> JobCreateResult | None:
        identifier(workspace_id)
        rows = self._read(
            "SELECT " + _JOB_COLUMNS + " FROM speech_jobs AS j "
            "LEFT JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
            "WHERE j.workspace_id = %s AND j.idempotency_key_digest = %s",
            (workspace_id, key_digest),
            submission_key=(workspace_id, key_digest),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return _replay_digest(rows[0], request_digest, retry_of_job_id)

    def create(self, job: NewPublicJob) -> JobCreateResult:
        deadline = time.monotonic() + self._timeout
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return JobCreateResult("unknown", error_code="outcome_unknown")
            try:
                with self._transaction(deadline) as connection:
                    if job.idempotency_key_digest is not None:
                        _lock_submission_key(
                            connection, job.workspace_id, job.idempotency_key_digest
                        )
                        existing = _existing_by_key(connection, job)
                        if existing is not None:
                            return _replay_or_conflict(existing, job)
                    if job.expected_binding is not None:
                        lock_binding(connection, job.expected_binding.object_id)
                        # Retain the defensive lookup for legacy writers that
                        # do not yet take the public Job key lock.
                        if job.idempotency_key_digest is not None:
                            existing = _existing_by_key(connection, job)
                            if existing is not None:
                                return _replay_or_conflict(existing, job)
                        _validate_pinned_create(connection, job)
                    else:
                        selected = _binding_is_still_selected(
                            connection,
                            job.kind,
                            job.model_id,
                            job.submission.effective_configuration_json,
                        )
                        if job.idempotency_key_digest is not None:
                            existing = _existing_by_key(connection, job)
                            if existing is not None:
                                return _replay_or_conflict(existing, job)
                        if not selected:
                            return JobCreateResult("rejected", error_code="job_rejected")
                    _insert(connection, job)
                    row = _select_job(connection, job.job_id)
                return JobCreateResult("created", _job(row))
            except psycopg.errors.UniqueViolation:
                if job.idempotency_key_digest is None:
                    return JobCreateResult("rejected", error_code="job_identity_conflict")
                if attempt == 0:
                    continue
                return JobCreateResult("unknown", error_code="outcome_unknown")
            except PublicJobUnavailableError, psycopg.OperationalError, psycopg.InterfaceError:
                return JobCreateResult("unknown", error_code="outcome_unknown")
            except psycopg.errors.QueryCanceled:
                return JobCreateResult("unknown", error_code="outcome_unknown")
            except psycopg.Error:
                return JobCreateResult("rejected", error_code="job_rejected")
            except PoolClosed, PoolTimeout, TooManyRequests:
                return JobCreateResult("rejected", error_code="job_unavailable")
        return JobCreateResult("unknown", error_code="outcome_unknown")

    def cancel(
        self,
        job_id: str,
        *,
        workspace_id: str | None,
        request_id: str,
        key_digest: str | None,
        request_digest: str,
        now_us: int,
    ) -> JobCancellationResult:
        """Accept one cancellation durably; stop evidence and release stay elsewhere."""
        del now_us  # authoritative time comes from the database clock in this transaction
        identifier(job_id)
        if workspace_id is not None:
            identifier(workspace_id)
        deadline = time.monotonic() + self._timeout
        try:
            with self._transaction(deadline) as connection:
                job_row = _lock_cancel_target(connection, job_id, workspace_id)
                if job_row is None:
                    return JobCancellationResult("not_found")
                job = _job(job_row)
                operation_id, attempt_id = job_row.get("operation_id"), job_row.get("attempt_id")
                if operation_id is None or attempt_id is None:
                    return JobCancellationResult("unknown")
                if key_digest is not None:
                    _lock_cancellation_key(connection, job.workspace_id, key_digest)
                    key_row = _cancellation_by_key(connection, job.workspace_id, key_digest)
                    if key_row is not None and (
                        key_row["job_id"] != job.job_id
                        or key_row["request_digest"] != request_digest
                    ):
                        return JobCancellationResult("conflict")
                # A terminal failure whose execution never confirmed stop holds
                # capacity and must not be reported as a clean cancellation.
                if job.state == "failed" and job.error_code == "cancellation_unconfirmed":
                    return JobCancellationResult("cleanup_pending")
                receipt_row = _cancellation_by_job(connection, job.job_id)
                if receipt_row is not None:
                    return JobCancellationResult(
                        "replayed" if _job_is_terminal(job) is False else "terminal",
                        job,
                        _cancellation(receipt_row),
                    )
                accepted = job.state in ("pending", "running", "cancelling")
                target = {"pending": "cancelled", "running": "cancelling"}.get(job.state)
                changed = None
                if target is not None:
                    changed = connection.execute(
                        "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) "
                        "* 1000000 AS BIGINT) AS now_us) "
                        "UPDATE speech_jobs AS j SET state = %s, "
                        "status_version = j.status_version + 1, updated_at_us = clock.now_us "
                        "FROM clock WHERE j.job_id = %s AND j.status_version = %s AND j.state = %s "
                        "RETURNING clock.now_us AS now_us",
                        (target, job.job_id, job.status_version, job.state),
                    ).fetchone()
                    if changed is None:
                        raise PublicJobConflictError("Public Job cancellation lost its guard")
                    connection.execute(
                        "INSERT INTO speech_job_events "
                        "(job_id, event_seq, state, occurred_at_us) "
                        "SELECT %s, COALESCE(MAX(event_seq), -1) + 1, %s, %s "
                        "FROM speech_job_events WHERE job_id = %s",
                        (job.job_id, target, int(changed["now_us"]), job.job_id),
                    )
                if changed is not None:
                    requested_at_us = int(changed["now_us"])
                else:
                    now_row = connection.execute(
                        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) "
                        "* 1000000 AS BIGINT) AS now_us"
                    ).fetchone()
                    if now_row is None:
                        raise PublicJobUnavailableError
                    requested_at_us = int(now_row["now_us"])
                connection.execute(
                    "INSERT INTO speech_job_cancellations "
                    "(job_id, workspace_id, operation_id, attempt_id, request_id, "
                    "request_digest, idempotency_key_digest, accepted, requested_at_us) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        job.job_id,
                        job.workspace_id,
                        operation_id,
                        attempt_id,
                        request_id,
                        request_digest,
                        key_digest,
                        accepted,
                        requested_at_us,
                    ),
                )
                updated = _select_job(connection, job.job_id)
                inserted = _cancellation_by_job(connection, job.job_id)
                if accepted:
                    # Last lock-taking write of acceptance: submit takes the same
                    # advisory lock before it can create an execution.
                    write_cancellation_fence(
                        connection,
                        job_id=job.job_id,
                        request_id=request_id,
                        accepted_at_us=requested_at_us,
                    )
            if updated is None or inserted is None:
                return JobCancellationResult("unknown")
            current = _job(updated)
            return JobCancellationResult(
                "accepted" if accepted else "terminal",
                current,
                _cancellation(inserted),
            )
        except PublicJobConflictError, PublicJobUnavailableError:
            raise
        except psycopg.errors.QueryCanceled, psycopg.OperationalError, psycopg.InterfaceError:
            return JobCancellationResult("unknown")
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            return JobCancellationResult("unknown")

    def settle_cancelled(
        self, job_id: str, *, expected_version: int, now_us: int
    ) -> PublicJob | None:
        """Close cancelling -> cancelled only after an external verified stop."""
        del now_us
        identifier(job_id)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Invalid public Job status version")
        deadline = time.monotonic() + self._timeout
        try:
            with self._transaction(deadline) as connection:
                changed = connection.execute(
                    "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) "
                    "* 1000000 AS BIGINT) AS now_us) "
                    "UPDATE speech_jobs AS j SET state = 'cancelled', "
                    "status_version = j.status_version + 1, updated_at_us = clock.now_us "
                    "FROM clock WHERE j.job_id = %s AND j.state = 'cancelling' "
                    "AND j.status_version = %s RETURNING clock.now_us AS now_us",
                    (job_id, expected_version),
                ).fetchone()
                if changed is None:
                    return None
                connection.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us) "
                    "SELECT %s, COALESCE(MAX(event_seq), -1) + 1, 'cancelled', %s "
                    "FROM speech_job_events WHERE job_id = %s",
                    (job_id, int(changed["now_us"]), job_id),
                )
                updated = _select_job(connection, job_id)
            return None if updated is None else _job(updated)
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise PublicJobUnavailableError from None

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
            with self._transaction(deadline) as connection:
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
                # The transition and its persisted event commit together, so a
                # reader never observes a state without its ordered event.
                connection.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us) "
                    "SELECT %s, COALESCE(MAX(event_seq), -1) + 1, %s, %s "
                    "FROM speech_job_events WHERE job_id = %s",
                    (current.job_id, target, now_us, current.job_id),
                )
                row = _select_job(connection, current.job_id)
            return _job(row)
        except PublicJobConflictError, PublicJobUnavailableError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise PublicJobUnavailableError from None

    def _read(
        self,
        query: str,
        parameters: tuple[object, ...],
        *,
        submission_key: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self._timeout
        try:
            with self._transaction(deadline) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                if submission_key is not None:
                    _lock_submission_key(connection, *submission_key)
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

    @contextmanager
    def _transaction(self, deadline: float) -> Iterator[_DeadlineConnection]:
        committing = False
        try:
            with self._pool.connection(timeout=_remaining(deadline)) as raw:
                connection = _DeadlineConnection(raw, deadline)
                _remaining(deadline)
                yield connection
                connection.arm()
                committing = True
            _remaining(deadline)
        except psycopg.Error:
            if committing:
                raise PublicJobUnavailableError from None
            raise


_CANCELLATION_COLUMNS = (
    "job_id, workspace_id, operation_id, attempt_id, request_id, request_digest, "
    "idempotency_key_digest, accepted, requested_at_us"
)


def _lock_cancel_target(
    connection: _Executor, job_id: str, workspace_id: str | None
) -> dict[str, Any] | None:
    query = (
        "SELECT " + _JOB_COLUMNS + ", c.operation_id, c.attempt_id "
        "FROM speech_jobs AS j "
        "LEFT JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
        "LEFT JOIN coordination_jobs AS c ON c.job_id = j.job_id "
        "WHERE j.job_id = %s"
    )
    parameters: tuple[object, ...] = (job_id,)
    if workspace_id is not None:
        query += " AND j.workspace_id = %s"
        parameters += (workspace_id,)
    query += " FOR UPDATE OF j"
    rows = connection.execute(query, parameters).fetchall()
    if len(rows) > 1:
        raise PublicJobUnavailableError
    return rows[0] if rows else None


def _lock_cancellation_key(connection: _Executor, workspace_id: str, key_digest: str) -> None:
    # Cancellation keys are scoped by workspace and never share the create
    # namespace. Acquire this lock before any receipt row write.
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:public-job-cancel-key:' || current_schema() || ':' || %s, 0))",
        (json.dumps([workspace_id, key_digest], separators=(",", ":")),),
    )


def _cancellation_by_key(
    connection: _Executor, workspace_id: str, key_digest: str
) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT " + _CANCELLATION_COLUMNS + " FROM speech_job_cancellations "
        "WHERE workspace_id = %s AND idempotency_key_digest = %s",
        (workspace_id, key_digest),
    ).fetchall()
    if len(rows) > 1:
        raise PublicJobUnavailableError
    return rows[0] if rows else None


def _cancellation_by_job(connection: _Executor, job_id: str) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT " + _CANCELLATION_COLUMNS + " FROM speech_job_cancellations WHERE job_id = %s",
        (job_id,),
    ).fetchall()
    if len(rows) > 1:
        raise PublicJobUnavailableError
    return rows[0] if rows else None


def _cancellation(row: dict[str, Any]) -> JobCancellationReceipt:
    try:
        return JobCancellationReceipt(
            job_id=row["job_id"],
            workspace_id=row["workspace_id"],
            operation_id=row["operation_id"],
            attempt_id=row["attempt_id"],
            request_id=row["request_id"],
            request_digest=row["request_digest"],
            idempotency_key_digest=row["idempotency_key_digest"],
            accepted=row["accepted"],
            requested_at_us=int(row["requested_at_us"]),
        )
    except KeyError, TypeError, ValueError:
        raise PublicJobUnavailableError from None


def _job_is_terminal(job: PublicJob) -> bool:
    return job.state in TERMINAL_STATES


def _binding_is_still_selected(
    connection: _Executor, kind: str, model_id: str, configuration_json: str
) -> bool:
    """Serialize a new Job write with lifecycle selection changes."""
    try:
        configuration = json.loads(configuration_json)
        reference = configuration["models"][kind]
        if (
            not isinstance(reference, dict)
            or reference.get("id") != model_id
            or not isinstance(reference.get("revision"), str)
        ):
            return False
        revision = reference["revision"]
    except ValueError, TypeError, KeyError, RecursionError:
        return False
    lock_binding(connection, model_id)
    row = connection.execute(
        "SELECT h.revision FROM speech_publication_heads h "
        "JOIN speech_publications p ON p.kind=h.kind AND p.object_id=h.object_id "
        "AND p.revision=h.revision AND p.revision_id=h.revision_id "
        "WHERE h.kind='binding' AND h.object_id=%s AND NOT EXISTS ("
        f"SELECT 1 FROM {RETIREMENT_TABLE} r WHERE r.kind=h.kind AND r.object_id=h.object_id "
        "AND r.revision=h.revision AND r.revision_id=h.revision_id)",
        (model_id,),
    ).fetchone()
    return row is not None and row["revision"] == revision


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


def _pinned_binding(rows: list[dict[str, Any]]) -> BindingFact | None:
    if not rows:
        return None
    if len(rows) != 1:
        raise PublicJobUnavailableError
    row = rows[0]
    try:
        definition = json.loads(row["definition_json"])
        if (
            not isinstance(definition, dict)
            or type(row["is_current"]) is not bool
            or type(row["is_retired"]) is not bool
        ):
            raise PublicJobUnavailableError
        return BindingFact(_publication(row), definition, row["is_current"], row["is_retired"])
    except ValueError, TypeError, KeyError, RecursionError:
        raise PublicJobUnavailableError from None


def _lock_submission_key(connection: _Executor, workspace_id: str, key_digest: str) -> None:
    # Serialize all keyed public creates and rejection reconciliation, including
    # keyless-binding requests. Never acquire this lock under a catalogue lock.
    # Canonical tuple encoding prevents ambiguous workspace/key concatenation.
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:public-job-key:' || current_schema() || ':' || %s, 0))",
        (json.dumps([workspace_id, key_digest], separators=(",", ":")),),
    )


def _validate_pinned_create(connection: _Executor, job: NewPublicJob) -> None:
    expected = job.expected_binding
    if expected is None:
        raise PublicJobUnavailableError
    reference = (expected.object_id, expected.revision)
    binding = require_pinned_binding(
        _pinned_binding(
            connection.execute(_PINNED_BINDING_QUERY, (reference[0], str(reference[1]))).fetchall()
        ),
        reference,
        job.model_id,
    )
    if binding.publication != expected:
        raise PublicJobUnavailableError
    intent = json.loads(job.request_document)
    configuration = json.loads(job.submission.effective_configuration_json)
    if (
        job.kind != "tts"
        or intent.get("kind") != "tts"
        or intent.get("model") != job.model_id
        or intent.get("model_binding") != expected.reference
        or configuration.get("models", {}).get("tts") != expected.reference
    ):
        raise PublicJobUnavailableError


def _progress(row: dict[str, Any]) -> JobProgressFact | None:
    """The committed progress value in one row, or None for a lifecycle row."""
    if row["kind"] == "lifecycle":
        return None
    if row["progress_unit"] is None or row["progress_completed"] is None:
        raise ValueError("Progress row is missing its fact")
    return JobProgressFact(
        unit=row["progress_unit"],
        completed=int(row["progress_completed"]),
        total=None if row["progress_total"] is None else int(row["progress_total"]),
    )


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
            retry_of_job_id=row.get("retry_of_job_id"),
            progress=(
                None
                if row.get("latest_progress_unit") is None
                else JobProgressFact(
                    unit=row["latest_progress_unit"],
                    completed=int(row["latest_progress_completed"]),
                    total=(
                        None
                        if row.get("latest_progress_total") is None
                        else int(row["latest_progress_total"])
                    ),
                )
            ),
        )
    except ValueError, TypeError, KeyError:
        raise PublicJobUnavailableError from None


def _select_job(connection: _Executor, job_id: str) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT " + _JOB_COLUMNS + " FROM speech_jobs AS j "
        "LEFT JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
        "WHERE j.job_id = %s",
        (job_id,),
    ).fetchall()
    if len(rows) > 1:
        raise psycopg.errors.InternalError("Public Job identity is not unique")
    return rows[0] if rows else None


def _existing_by_key(connection: _Executor, job: NewPublicJob) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT " + _JOB_COLUMNS + " FROM speech_jobs AS j "
        "LEFT JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
        "WHERE j.workspace_id = %s AND j.idempotency_key_digest = %s",
        (job.workspace_id, job.idempotency_key_digest),
    ).fetchall()
    if len(rows) > 1:
        raise psycopg.errors.InternalError("Idempotency identity is not unique")
    return rows[0] if rows else None


def _replay_or_conflict(row: dict[str, Any], job: NewPublicJob) -> JobCreateResult:
    return _replay_digest(row, job.request_digest, job.retry_of_job_id)


def _replay_digest(
    row: dict[str, Any], request_digest: str, retry_of_job_id: str | None
) -> JobCreateResult:
    """One key identifies one intent and one operation; lineage is part of it."""
    if row.get("request_digest") != request_digest or row.get("retry_of_job_id") != retry_of_job_id:
        return JobCreateResult("conflict", error_code="idempotency_conflict")
    return JobCreateResult("replayed", _job(row))


def _insert(connection: _Executor, job: NewPublicJob) -> None:
    submission = job.submission
    attempt_id = job.job_id + "-a1"
    connection.execute(
        "INSERT INTO speech_jobs "
        "(job_id, workspace_id, kind, model_id, request_digest, idempotency_key_digest, "
        "retry_of_job_id, state, status_version, result_json, error_code, "
        "created_at_us, updated_at_us) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 1, NULL, NULL, %s, %s)",
        (
            job.job_id,
            job.workspace_id,
            job.kind,
            job.model_id,
            job.request_digest,
            job.idempotency_key_digest,
            job.retry_of_job_id,
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
    # Private original public intent (including any exact binding selection).
    # This is NOT the complete effective execution command required by DEL-150;
    # it must not be dispatched or supplemented using mutable current defaults.
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
