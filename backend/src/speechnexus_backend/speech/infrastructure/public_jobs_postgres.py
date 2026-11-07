"""Authoritative public Job facts and one bounded atomic create transaction."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.public_jobs import (
    BindingFact,
    VoiceDraftRevisionFact,
    WorkspaceFact,
    require_pinned_binding,
    resolve_voice_previews,
)
from speechnexus_backend.speech.application.voice_drafts import (
    draft_id_for_job,
    voice_input_document,
    voice_input_fits,
    voice_input_shape,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JOB_STATES,
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
    RevisionCreateResult,
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
_ARTIFACT_COLUMNS = (
    "artifact_id, artifact_kind, artifact_state, media_type, bytes, "
    "sha256, receipt_id, expires_at_us"
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
            "SELECT " + _ARTIFACT_COLUMNS + " FROM speech_artifacts "
            "WHERE workspace_id = %s AND artifact_id = %s",
            (identifier(workspace_id), identifier(artifact_id)),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return _artifact(rows[0])

    def voice_draft_revision(self, draft_id: str, revision: str) -> VoiceDraftRevisionFact | None:
        rows = self._read(
            "SELECT j.workspace_id, r.kind, d.head_revision, r.generation_job_id, "
            "j.state, j.result_json, j.model_id, r.binding_json "
            "FROM speech_voice_draft_revisions AS r "
            "JOIN speech_voice_drafts AS d ON d.draft_id = r.draft_id "
            "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
            "WHERE r.draft_id = %s AND r.revision = %s",
            (identifier(draft_id), revision),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        row = rows[0]
        try:
            binding = json.loads(row["binding_json"])
            if not isinstance(binding, dict) or set(binding) != {"id", "revision"}:
                raise ValueError
            binding_id, binding_revision = binding["id"], binding["revision"]
            if not isinstance(binding_id, str) or not isinstance(binding_revision, str):
                raise ValueError
            identifier(binding_id)
            if not binding_revision.isdigit() or binding_revision.startswith("0"):
                raise ValueError
            state = row["state"]
            if state not in JOB_STATES:
                raise ValueError
            return VoiceDraftRevisionFact(
                workspace_id=row["workspace_id"],
                kind=row["kind"],
                head_revision=row["head_revision"],
                generation_job_id=row["generation_job_id"],
                state=state,
                result_json=row["result_json"],
                model_id=row["model_id"],
                binding=(binding_id, int(binding_revision)),
            )
        except ValueError, TypeError, KeyError:
            raise PublicJobUnavailableError from None

    def artifacts_batch(
        self, workspace_id: str, artifact_ids: Sequence[str]
    ) -> dict[str, JobArtifactFact]:
        """Read every Artifact a page references in one bounded READ ONLY transaction.

        Missing or non-ready rows stay absent from the mapping; the caller's
        per-Job verification decides whether that absence is fatal.
        """
        wanted = sorted({identifier(artifact_id) for artifact_id in artifact_ids})
        if not wanted:
            return {}
        rows = self._read(
            "SELECT " + _ARTIFACT_COLUMNS + " FROM speech_artifacts "
            "WHERE workspace_id = %s AND artifact_id = ANY(%s)",
            (identifier(workspace_id), wanted),
        )
        if len(rows) > len(wanted):
            raise PublicJobUnavailableError
        facts = (_artifact(row) for row in rows)
        return {fact.artifact_id: fact for fact in facts if fact is not None}

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

    def revision_for_job(self, draft_id: str, job_id: str) -> str | None:
        rows = self._read(
            "SELECT revision FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s AND generation_job_id = %s",
            (identifier(draft_id), identifier(job_id)),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicJobUnavailableError
        return str(rows[0]["revision"])

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
                        if job.kind == "voice_validation":
                            rejected = _lock_validation_target(connection, job)
                            if rejected is not None:
                                return JobCreateResult("rejected", error_code=rejected)
                        elif job.kind == "voice_preview":
                            rejected = _lock_preview_target(connection, job)
                            if rejected is not None:
                                return JobCreateResult("rejected", error_code=rejected)
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
                # A non-operational database refusal (integrity, data, program)
                # is deterministic. Keep it distinct from changed facts so the
                # HTTP boundary can report an implementation defect, not a
                # client conflict or a retryable outage.
                return JobCreateResult("rejected", error_code="storage_rejected")
        return JobCreateResult("unknown", error_code="outcome_unknown")

    def create_revision(
        self, job: NewPublicJob, *, draft_id: str, expected_revision: str
    ) -> RevisionCreateResult:
        """Append one immutable revision and its Job rows in one transaction.

        Replay is decided before the draft row lock: a head advance must never
        turn a same-key replay into a stale conflict.
        """
        deadline = time.monotonic() + self._timeout
        selected: dict[str, Any] | None = None
        revision: str | None = None
        try:
            with self._transaction(deadline) as connection:
                if job.idempotency_key_digest is not None:
                    _lock_submission_key(connection, job.workspace_id, job.idempotency_key_digest)
                    existing = _existing_by_key(connection, job)
                    if existing is not None:
                        replayed = _replay_digest(existing, job.request_digest, job.retry_of_job_id)
                        if replayed.outcome != "replayed" or replayed.job is None:
                            return RevisionCreateResult(
                                "conflict", error_code="idempotency_conflict"
                            )
                        replayed_revision = _revision_for_job(
                            connection, draft_id, replayed.job.job_id
                        )
                        if replayed_revision is None:
                            return RevisionCreateResult("unknown", error_code="revision_missing")
                        return RevisionCreateResult(
                            "replayed", job=replayed.job, revision=replayed_revision
                        )
                if not _binding_is_still_selected(
                    connection,
                    job.kind,
                    job.model_id,
                    job.submission.effective_configuration_json,
                ):
                    return RevisionCreateResult("rejected", error_code="job_rejected")
                row = connection.execute(
                    "SELECT d.head_revision, d.status, d.kind, d.display_name, "
                    "d.metadata_revision, d.created_at_us, j.workspace_id "
                    "FROM speech_voice_drafts AS d "
                    "JOIN speech_jobs AS j ON j.job_id = d.generation_job_id "
                    "WHERE d.draft_id = %s FOR UPDATE OF d",
                    (draft_id,),
                ).fetchone()
                if row is None or row["workspace_id"] != job.workspace_id:
                    return RevisionCreateResult("not_found")
                draft_kind = "design" if job.kind == "voice_design" else "clone"
                if row["kind"] != draft_kind:
                    return RevisionCreateResult("conflict", error_code="draft_kind_mismatch")
                if row["status"] in ("published", "preview_only"):
                    return RevisionCreateResult("conflict", error_code="draft_not_generatable")
                if row["head_revision"] != expected_revision:
                    return RevisionCreateResult("stale")
                revision = _next_revision(expected_revision)
                if revision is None:
                    return RevisionCreateResult("rejected", error_code="revision_exhausted")
                _, stored_input, binding_json = _voice_stored_values(job, draft_kind)
                _insert_job_rows(connection, job)
                connection.execute(
                    "INSERT INTO speech_voice_draft_revisions "
                    "(draft_id, revision, kind, generation_job_id, input_json, binding_json, "
                    "created_at_us) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        draft_id,
                        revision,
                        draft_kind,
                        job.job_id,
                        stored_input,
                        binding_json,
                        job.submission.submitted_at_us,
                    ),
                )
                # The row is held FOR UPDATE and its head was compared above;
                # repeating head_revision in this predicate is unreachable and
                # must not be mistaken for an independent CAS guard.
                updated = connection.execute(
                    "UPDATE speech_voice_drafts SET head_revision = %s, "
                    "generation_job_id = %s, updated_at_us = GREATEST(updated_at_us, %s) "
                    "WHERE draft_id = %s",
                    (
                        revision,
                        job.job_id,
                        job.submission.submitted_at_us,
                        draft_id,
                    ),
                ).rowcount
                if updated != 1:
                    raise PublicJobUnavailableError
                selected = _select_job(connection, job.job_id)
            if selected is None or revision is None:
                return RevisionCreateResult("unknown", error_code="outcome_unknown")
            return RevisionCreateResult("created", job=_job(selected), revision=revision)
        except psycopg.errors.UniqueViolation:
            return RevisionCreateResult("unknown", error_code="outcome_unknown")
        except (
            PublicJobUnavailableError,
            psycopg.OperationalError,
            psycopg.InterfaceError,
        ):
            return RevisionCreateResult("unknown", error_code="outcome_unknown")
        except psycopg.errors.QueryCanceled:
            return RevisionCreateResult("unknown", error_code="outcome_unknown")
        except psycopg.Error:
            # See create(): a deterministic database refusal is not a conflict.
            return RevisionCreateResult("rejected", error_code="storage_rejected")

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
        reference = configuration["models"]["voice" if kind.startswith("voice_") else kind]
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


def _locked_artifact_reader(
    connection: _Executor,
) -> Callable[[str, str], JobArtifactFact | None]:
    """Read one Artifact through this transaction instead of the request facts."""

    def read(workspace_id: str, artifact_id: str) -> JobArtifactFact | None:
        row = connection.execute(
            "SELECT " + _ARTIFACT_COLUMNS + " FROM speech_artifacts "
            "WHERE workspace_id = %s AND artifact_id = %s",
            (workspace_id, artifact_id),
        ).fetchone()
        return None if row is None else _artifact(row)

    return read


def _lock_validation_target(connection: _Executor, job: NewPublicJob) -> str | None:
    """Recheck the candidate under the draft row lock before the Job insert.

    ``_prepare_job`` resolves the candidate in earlier statements. Holding the
    draft row here prevents a concurrent append from advancing the head, but it
    does not refresh what those earlier statements already read. The locked
    statement therefore re-reads every fact this transaction can see: the head,
    the draft lifecycle status, the generation Job state and result, and every
    preview Artifact that result references. Preview readiness reuses
    ``resolve_voice_previews``, the same rule the prepare step and the public
    projections apply, so a preview that turns non-ready between prepare and
    insert is rejected instead of being admitted with stale evidence.

    Binding publication is not re-read under this lock on purpose.
    ``_validate_pinned_create`` already re-reads the pinned revision in this
    same transaction while holding ``lock_binding`` until commit, and
    ``create_revision`` takes that binding advisory lock before the draft row
    lock. Re-reading it here would acquire the binding lock after this row lock
    and invert the established binding -> draft order.

    The draft row lock does not cover the Artifact row itself, so an expiry
    committed after this statement is outside its scope. This re-read closes the
    prepare -> insert gap; it does not make Artifact state immutable.
    """
    try:
        intent = json.loads(job.request_document)
        request_input = intent["input"]
        draft_id, revision = request_input["voice_draft_id"], request_input["draft_revision"]
    except ValueError, TypeError, KeyError:
        raise PublicJobUnavailableError from None
    row = connection.execute(
        "SELECT d.head_revision, d.status, r.kind AS draft_kind, "
        "j.state AS generation_state, j.result_json AS generation_result, "
        "j.workspace_id AS generation_workspace "
        "FROM speech_voice_drafts AS d "
        "JOIN speech_voice_draft_revisions AS r "
        "ON r.draft_id = d.draft_id AND r.revision = %s "
        "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
        "WHERE d.draft_id = %s FOR UPDATE OF d",
        (revision, draft_id),
    ).fetchone()
    if row is None or row["generation_workspace"] != job.workspace_id:
        return "not_found"
    if row["head_revision"] != revision:
        return "stale"
    if row["status"] in ("published", "preview_only"):
        return "draft_not_generatable"
    if row["generation_state"] != "succeeded":
        return "not_candidate"
    try:
        resolve_voice_previews(
            workspace_id=job.workspace_id,
            kind="voice_" + row["draft_kind"],
            state=row["generation_state"],
            result_json=row["generation_result"],
            artifact=_locked_artifact_reader(connection),
        )
    except PublicJobUnavailableError:
        # A result or Artifact that no longer resolves is not a validation
        # target. The prepare step reports the same condition as unavailable.
        return "job_rejected"
    return None


def _lock_preview_target(connection: _Executor, job: NewPublicJob) -> str | None:
    """Recheck the exact head candidate under the draft row lock before insert.

    The early replay lookup runs before this lock. A concurrent-commit miss is
    retried by ``PublicJobs.submit`` only after this transaction fails, i.e.
    after any lock it took has been released, so a head advance never turns a
    same-key replay into a stale conflict.
    """
    try:
        intent = json.loads(job.request_document)
        request_input = intent["input"]
        draft_id, revision = request_input["voice_draft_id"], request_input["draft_revision"]
    except ValueError, TypeError, KeyError:
        raise PublicJobUnavailableError from None
    row = connection.execute(
        "SELECT d.head_revision, j.state AS generation_state, "
        "j.workspace_id AS generation_workspace "
        "FROM speech_voice_drafts AS d "
        "JOIN speech_voice_draft_revisions AS r "
        "ON r.draft_id = d.draft_id AND r.revision = %s "
        "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
        "WHERE d.draft_id = %s FOR UPDATE OF d",
        (revision, draft_id),
    ).fetchone()
    if row is None or row["generation_workspace"] != job.workspace_id:
        return "not_found"
    if row["head_revision"] != revision:
        return "stale"
    if row["generation_state"] != "succeeded":
        return "not_candidate"
    return None


def _validate_pinned_create(connection: _Executor, job: NewPublicJob) -> None:
    expected = job.expected_binding
    if expected is None:
        raise PublicJobUnavailableError
    reference = (expected.object_id, expected.revision)
    intent = json.loads(job.request_document)
    if job.kind == "tts":
        task, model_key = "tts_file", "tts"
    elif job.kind == "voice_validation":
        task, model_key = "voice_validation", "voice"
    elif job.kind == "voice_preview":
        # A preview runs on the draft kind's task, still under the pinned binding.
        request_input = intent.get("input")
        if not isinstance(request_input, dict) or not isinstance(
            request_input.get("voice_draft_id"), str
        ):
            raise PublicJobUnavailableError
        row = connection.execute(
            "SELECT kind FROM speech_voice_drafts WHERE draft_id = %s",
            (request_input["voice_draft_id"],),
        ).fetchone()
        if row is None:
            raise PublicJobUnavailableError
        task, model_key = (
            ("voice_design" if row["kind"] == "design" else "voice_clone"),
            "voice",
        )
    else:
        raise PublicJobUnavailableError
    binding = require_pinned_binding(
        _pinned_binding(
            connection.execute(_PINNED_BINDING_QUERY, (reference[0], str(reference[1]))).fetchall()
        ),
        reference,
        job.model_id,
        task=task,
    )
    if binding.publication != expected:
        raise PublicJobUnavailableError
    configuration = json.loads(job.submission.effective_configuration_json)
    if (
        intent.get("kind") != job.kind
        or intent.get("model", job.model_id) != job.model_id
        or intent.get("model_binding") != expected.reference
        or configuration.get("models", {}).get(model_key) != expected.reference
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


def _artifact(row: dict[str, Any]) -> JobArtifactFact | None:
    """A ready Artifact with a committed receipt; anything else has no fact."""
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


def _insert_job_rows(connection: _Executor, job: NewPublicJob) -> None:
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


def _voice_stored_values(job: NewPublicJob, voice_kind: str) -> tuple[dict[str, Any], str, str]:
    """Canonical stored input and pinned voice binding for one revision row."""
    try:
        intent = json.loads(job.request_document)
        if not isinstance(intent, dict) or not isinstance(intent.get("input"), dict):
            raise ValueError
        source = intent["input"]
        voice_input = voice_input_shape(source, voice_kind)
        stored_input = voice_input_document(source, voice_kind)
        if not voice_input_fits(stored_input):
            raise ValueError
        configuration = json.loads(job.submission.effective_configuration_json)
        binding = configuration.get("models", {}).get("voice")
        if not isinstance(binding, dict) or set(binding) != {"id", "revision"}:
            raise ValueError
        return (
            voice_input,
            stored_input,
            json.dumps(binding, ensure_ascii=False),
        )
    except ValueError, TypeError, KeyError, RecursionError:
        raise PublicJobUnavailableError from None


def _next_revision(current: str) -> str | None:
    try:
        value = int(current) + 1
    except ValueError:
        return None
    if value >= 10**19:
        return None
    return str(value)


def _revision_for_job(connection: _Executor, draft_id: str, job_id: str) -> str | None:
    rows = connection.execute(
        "SELECT revision FROM speech_voice_draft_revisions "
        "WHERE draft_id = %s AND generation_job_id = %s",
        (draft_id, job_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise PublicJobUnavailableError
    return str(rows[0]["revision"])


def _insert_creation_draft(connection: _Executor, job: NewPublicJob) -> None:
    submission = job.submission
    voice_kind = job.kind.removeprefix("voice_")
    voice_input, stored_input, binding_json = _voice_stored_values(job, voice_kind)
    connection.execute(
        "INSERT INTO speech_voice_drafts "
        "(draft_id, generation_job_id, kind, status, head_revision, metadata_revision, "
        "display_name, input_json, binding_json, created_at_us, updated_at_us) "
        "VALUES (%s,%s,%s,'generating','1',1,%s,%s,%s,%s,%s)",
        (
            draft_id_for_job(job.job_id),
            job.job_id,
            voice_kind,
            voice_input.get("display_name", "Voice " + voice_kind),
            stored_input,
            binding_json,
            submission.submitted_at_us,
            submission.submitted_at_us,
        ),
    )
    connection.execute(
        "INSERT INTO speech_voice_draft_revisions "
        "(draft_id, revision, kind, generation_job_id, input_json, binding_json, "
        "created_at_us) VALUES (%s,'1',%s,%s,%s,%s,%s)",
        (
            draft_id_for_job(job.job_id),
            voice_kind,
            job.job_id,
            stored_input,
            binding_json,
            submission.submitted_at_us,
        ),
    )


def _insert(connection: _Executor, job: NewPublicJob) -> None:
    _insert_job_rows(connection, job)
    if job.kind in ("voice_design", "voice_clone"):
        _insert_creation_draft(connection, job)
