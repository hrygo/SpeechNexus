"""Bounded PostgreSQL admission; ambiguous commit recovery is strictly read-only."""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from typing import cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from speechnexus_contracts.execution import parse

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRejectedError,
    AdmissionResult,
    AdmitRecipe,
    rejected,
    unknown,
)
from speechnexus_backend.speech.domain.runtime_intents import digest, integer
from speechnexus_backend.speech.infrastructure.admission_codec import (
    PreparedAdmission,
    prepare_admission,
)
from speechnexus_backend.speech.infrastructure.admission_recovery_sql import (
    AdmissionCursor,
    read_original,
)
from speechnexus_backend.speech.infrastructure.admission_sql import admit_in_transaction
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import (
    lock_binding,
    revision_is_disabled,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.leases_postgres import (
    _BudgetExpiredError,
    _DeadlineCursor,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.transcription_submit import (
    command_digest,
    deadline_text,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_sql import insert_tts_settlement

_LOGGER = logging.getLogger(__name__)


def binding_disable_admission_grants(schema: str, role: str) -> tuple[str, ...]:
    """Least-privilege provisioning for the synchronous admission barrier."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT SELECT (binding_id, revision_id) ON "{schema}".speech_binding_revisions '
        f'TO "{role}"',
        f"GRANT SELECT (kind, object_id, revision_id, mode) "
        f'ON "{schema}".speech_publication_retirements TO "{role}"',
    )


def tts_voice_admission_grants(schema: str, role: str) -> tuple[str, ...]:
    """Read only the publication identities needed under the voice lock."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f"GRANT SELECT (kind, object_id, revision, revision_id) "
        f'ON "{schema}".speech_publications TO "{role}"',
        f"GRANT SELECT (kind, object_id, revision, revision_id) "
        f'ON "{schema}".speech_publication_retirements TO "{role}"',
    )


def _is_tts_business(body: dict[str, object]) -> bool:
    """A carried TTS submit cannot opt out of the voice admission barrier."""
    kind = body.get("kind")
    for gateway in (body, body.get("gateway_request"), body.get("execution_request")):
        if isinstance(gateway, dict):
            command = gateway.get("command")
            if isinstance(command, dict):
                is_tts = command.get("kind") == "tts" or command.get("task_kind") == "tts_file"
                if is_tts and kind != "tts_file":
                    raise AdmissionRejectedError("invalid_execution_request")
    return kind == "tts_file"


class PostgresAdmissionTransactions:
    def __init__(
        self,
        pool: SpeechPool,
        *,
        max_command_bytes: int,
        timeout_seconds: float = 3,
        max_attempts: int = 3,
        enforce_binding_disable: bool = False,
        require_builtin_voice: Callable[..., object] | None = None,
    ) -> None:
        integer(max_command_bytes, minimum=1)
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Admission requires bounded transaction deadlines and retries")
        self._pool = pool
        self._max_bytes = max_command_bytes
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._enforce_binding_disable = enforce_binding_disable
        self._require_builtin_voice = require_builtin_voice

    def admit_recipe(self, command: AdmitRecipe) -> AdmissionResult:
        identity = command.request.identity
        try:
            prepared = prepare_admission(command, max_command_bytes=self._max_bytes)
        except ValueError, TypeError, KeyError:
            return rejected(identity, "invalid_execution_request")
        return self._run(identity, prepared=prepared, deadline=time.monotonic() + self._timeout)

    def query_admission(
        self, identity: AdmissionIdentity, *, request_digest: str | None = None
    ) -> AdmissionResult:
        if request_digest is not None:
            try:
                digest(request_digest)
            except ValueError:
                return rejected(identity, "invalid_execution_request")
        return self._run(
            identity, request_digest=request_digest, deadline=time.monotonic() + self._timeout
        )

    def _admit(self, cursor: _DeadlineCursor, prepared: PreparedAdmission) -> AdmissionResult:
        is_tts = _is_tts_business(json.loads(prepared.command.business_command.document))
        if self._enforce_binding_disable or is_tts:
            request = prepared.command.request
            existing = read_original(
                cursor, request.identity, max_command_bytes=self._max_bytes, prepared=prepared
            )
            if existing is not None:
                return existing
            cursor.execute(
                "SELECT binding_id FROM speech_binding_revisions WHERE revision_id=%s",
                (request.binding_revision_id,),
            )
            revisions = cursor.fetchall()
            if len(revisions) != 1:
                raise AdmissionRejectedError("reconciliation_required")
            binding_id = revisions[0]["binding_id"]
            lock_binding(cursor, binding_id)
            existing = read_original(
                cursor, request.identity, max_command_bytes=self._max_bytes, prepared=prepared
            )
            if existing is not None:
                return existing
            if revision_is_disabled(cursor, binding_id, request.binding_revision_id):
                raise AdmissionRejectedError("policy_or_resource_disabled")
        self._lock_tts_voice(cursor, prepared)
        return admit_in_transaction(
            cursor, prepared, clock=cursor.clock, max_command_bytes=self._max_bytes
        )

    def _lock_tts_voice(
        self, cursor: _DeadlineCursor, prepared: PreparedAdmission, *, recheck: bool = False
    ) -> None:
        """Serialize new TTS admission with retirement and recheck before commit."""
        try:
            body = json.loads(prepared.command.business_command.document)
            if body.get("kind") != "tts_file":
                return
            voice = body["voice"]
            plan = body["plan"]
            policy_digest = plan["gateway_policy_digest"]
            speaker_id = plan["native_speaker_id"]
            if not isinstance(policy_digest, str) or not isinstance(speaker_id, str):
                raise ValueError
            stages = prepared.command.request.stages
            if len(stages) != 1:
                raise ValueError
            stage = stages[0]
            voice_id, revision, revision_id = voice["id"], voice["revision"], voice["revision_id"]
            gateway = parse("SubmitExecutionRequest", body["gateway_request"])
            wire_voice = gateway["command"]["resources"]["voice"]
            request = prepared.command.request
            if (
                not isinstance(voice_id, str)
                or type(revision) is not int
                or revision < 1
                or not isinstance(revision_id, str)
                or wire_voice != {"id": voice_id, "revision": str(revision)}
                or gateway["voice_revision_id"] != revision_id
                or gateway["binding_revision_id"] != request.binding_revision_id
                or gateway["policy_revision_id"] != request.policy_revision_id
                or gateway["workspace_id"] != request.workspace_id
                or gateway["command"]["kind"] != "tts"
                or gateway["command"]["task_kind"] != "tts_file"
                or gateway["deadline_at"] != deadline_text(request.deadline_at_us)
                or gateway["operation_id"] != request.identity.operation_id
                or gateway["attempt_id"] != request.identity.attempt_id
                or any(
                    gateway[key] != getattr(stage, key)
                    for key in (
                        "stage_id",
                        "lease_id",
                        "pool_id",
                        "host_node_id",
                        "gateway_id",
                        "instance_id",
                        "instance_generation",
                    )
                )
                or gateway["fencing_token"] != request.authorities[0].fencing_token
                or gateway["command_digest"]
                != command_digest(cast(dict[str, object], gateway["command"]))
            ):
                raise ValueError
        except ValueError, TypeError, KeyError, AttributeError:
            raise AdmissionRejectedError("reconciliation_required") from None
        if not recheck:
            existing = read_original(
                cursor,
                prepared.command.request.identity,
                max_command_bytes=self._max_bytes,
                prepared=prepared,
            )
            if existing is not None:
                return
        if self._require_builtin_voice is None:
            raise AdmissionRejectedError("reconciliation_required")
        if not recheck:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'speechnexus:catalog:' || current_schema() || ':voice:' || %s, 0))",
                (voice_id,),
            )
            # A duplicate admission can commit while the voice lock is awaited.
            existing = read_original(
                cursor,
                prepared.command.request.identity,
                max_command_bytes=self._max_bytes,
                prepared=prepared,
            )
            if existing is not None:
                return
        cursor.execute(
            "SELECT p.revision_id FROM speech_publications p WHERE p.kind='voice' "
            "AND p.object_id=%s AND p.revision=%s AND p.revision_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM speech_publication_retirements r "
            "WHERE r.kind=p.kind AND r.object_id=p.object_id AND r.revision=p.revision "
            "AND r.revision_id=p.revision_id)",
            (voice_id, str(revision), revision_id),
        )
        if len(cursor.fetchall()) != 1:
            raise AdmissionRejectedError("policy_or_resource_disabled")
        self._require_builtin_voice(
            cursor,
            voice_id=voice_id,
            revision=revision,
            revision_id=revision_id,
            binding_revision_id=prepared.command.request.binding_revision_id,
            policy_revision_id=prepared.command.request.policy_revision_id,
            instance_id=stage.instance_id,
            instance_generation=stage.instance_generation,
            host_node_id=stage.host_node_id,
            gateway_id=stage.gateway_id,
            command=gateway["command"],
            sample_rate_hz=gateway["sample_rate_hz"],
            gateway_policy_digest=policy_digest,
            native_speaker_id=speaker_id,
        )

    def _recheck_before_commit(self, cursor: _DeadlineCursor, prepared: PreparedAdmission) -> None:
        """Revalidate new TTS authority after every potentially blocking transaction step."""
        self._lock_tts_voice(cursor, prepared, recheck=True)

    def _record_settlement(self, cursor: _DeadlineCursor, prepared: PreparedAdmission) -> None:
        body = json.loads(prepared.command.business_command.document)
        if _is_tts_business(body):
            insert_tts_settlement(
                cursor, body["gateway_request"], prepared.command.request.deadline_at_us
            )

    def _run(
        self,
        identity: AdmissionIdentity,
        *,
        deadline: float,
        prepared: PreparedAdmission | None = None,
        request_digest: str | None = None,
        recover_prepared: PreparedAdmission | None = None,
        conflict_if_absent: bool = False,
    ) -> AdmissionResult:
        for attempt in range(self._max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready_to_commit = False
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
                    if prepared is None:
                        connection.execute("SET TRANSACTION READ ONLY")
                    with connection.cursor() as raw:
                        cursor = _DeadlineCursor(cast(AdmissionCursor, raw), deadline)
                        if prepared is not None:
                            result = self._admit(cursor, prepared)
                            if result.outcome in ("committed", "replayed"):
                                self._record_settlement(cursor, prepared)
                            if result.outcome == "committed":
                                # Original replays must never depend on today's qualification.
                                # The voice lock remains held, and settlement may itself wait.
                                self._recheck_before_commit(cursor, prepared)
                        else:
                            found = read_original(
                                cursor,
                                identity,
                                max_command_bytes=self._max_bytes,
                                request_digest=request_digest,
                                prepared=recover_prepared,
                            )
                            if found is None:
                                result = (
                                    rejected(identity, "idempotency_conflict")
                                    if conflict_if_absent
                                    else unknown(identity)
                                )
                            else:
                                result = found
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                    ready_to_commit = True
                return result  # Return success only after commit acknowledgement.
            except AdmissionRejectedError as error:
                return rejected(identity, error.code)
            except psycopg.errors.UniqueViolation:
                # The context confirmed rollback. The collision may be an
                # alternate global lease/Job/event key; never issue a new write.
                if prepared is None:
                    return rejected(identity, "reconciliation_required")
                return self._run(
                    identity,
                    deadline=deadline,
                    recover_prepared=prepared,
                    conflict_if_absent=True,
                )
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)  # Confirmed rollback, after returning the connection.
            except (
                PoolClosed,
                PoolTimeout,
                TooManyRequests,
                _BudgetExpiredError,
                psycopg.errors.QueryCanceled,
            ):
                if ready_to_commit:
                    return unknown(identity)
                break
            except psycopg.OperationalError, psycopg.InterfaceError:
                return unknown(identity)
            except (psycopg.Error, ValueError, TypeError, KeyError) as error:
                sqlstate = getattr(error, "sqlstate", None)
                if (
                    not isinstance(sqlstate, str)
                    or len(sqlstate) != 5
                    or any(char not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for char in sqlstate)
                ):
                    sqlstate = None
                # Exception text, SQL and parameters can contain private commands or DSNs.
                _LOGGER.warning(
                    "Admission storage failure: operation=%s error=%s sqlstate=%s",
                    identity.operation_id,
                    type(error).__name__,
                    sqlstate,
                )
                if ready_to_commit:
                    return unknown(identity)
                return rejected(identity, "reconciliation_required")
        # An exhausted read cannot prove that the original admission write rolled back.
        if prepared is None:
            return unknown(identity)
        return AdmissionResult(
            "rejected", identity, error_code="reconciliation_required", recovery="retry_transaction"
        )
