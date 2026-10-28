"""Explicit submit-intent statements; the caller owns the transaction.

``create_submit`` locks the pool row, the instance head, the instance generation, the
attempt and the lease in the contract's global order, re-reads the authority after waiting
for those locks, verifies the claim and the deadline/instance eligibility against it, then
writes one ``submit_intents`` row and, for a PCM stage, its ``output_stream_heads`` row.
No engine call, object write, budget calculation or sleep happens inside the transaction,
and the canonical request body is written but never selected back here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from speechnexus_gateway.domain.execution_results import staging_result_from_document
from speechnexus_gateway.domain.submission import (
    CleanupState,
    EngineInstanceState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    LeaseCleanupState,
    LeaseFacts,
    LeaseState,
    StaleSubmissionVersionError,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionIntent,
    SubmissionStoreError,
    SubmitIntentConflictError,
    hard_output_ceiling_samples,
    max_output_audio_ms_for,
    verify_result_eligibility,
    verify_submit_eligibility,
    verify_submit_identity,
)
from speechnexus_gateway.infrastructure.binding_disable_guard import check_binding, lock_binding

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

#: Columns ``create_submit`` inserts; the gateway role holds table-wide INSERT here.
SUBMIT_INTENT_INSERT_COLUMNS: Final = (
    "submit_intent_id",
    "lease_id",
    "operation_id",
    "attempt_id",
    "command_digest",
    "request_digest",
    "request_document",
    "voice_revision_id",
    "deadline_at_us",
    "recorded_at_us",
    "updated_at_us",
    "execution_state",
    "cleanup_state",
    "engine_request_id",
    "dispatched_at_us",
    "error_code",
    "output_stream_id",
    "sample_rate_hz",
    "hard_output_ceiling_samples",
    "version",
)

#: Columns a state change rewrites. Identity, digest, body and stream stay immutable.
SUBMIT_INTENT_UPDATE_COLUMNS: Final = (
    "execution_state",
    "cleanup_state",
    "engine_request_id",
    "dispatched_at_us",
    "error_code",
    "updated_at_us",
    "version",
)

_INSERT_INTENT = (
    "INSERT INTO submit_intents ("
    + ", ".join(SUBMIT_INTENT_INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join("%s" for _ in SUBMIT_INTENT_INSERT_COLUMNS)
    + ")"
)

_UPDATE_INTENT = (
    "UPDATE submit_intents SET "
    + ", ".join(f"{column} = %s" for column in SUBMIT_INTENT_UPDATE_COLUMNS if column != "version")
    + ", version = version + 1 WHERE submit_intent_id = %s AND version = %s"
)

_LOCK_POOL = "SELECT pool_id FROM resource_pools WHERE pool_id = %s FOR UPDATE"

_LOCK_ATTEMPT = (
    "SELECT operation_id FROM admission_attempts "
    "WHERE operation_id = %s AND attempt_id = %s FOR UPDATE"
)

_LOCK_INSTANCE_HEAD = (
    "SELECT instance_id FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE"
)

_LOCK_INSTANCE = (
    "SELECT instance_id FROM engine_instances "
    "WHERE instance_id = %s AND instance_generation = %s FOR UPDATE"
)

_LOCK_LEASE = "SELECT lease_id FROM admission_leases WHERE lease_id = %s FOR UPDATE"

_SELECT_AUTHORITY = """
SELECT l.lease_id, l.operation_id, l.attempt_id, l.stage_id, l.host_node_id, l.instance_id,
       l.instance_generation, l.pool_id, l.gateway_id, l.fencing_token, l.lease_state,
       l.cleanup_state AS lease_cleanup_state, l.deadline_at_us,
       i.instance_state, i.last_fencing_token AS instance_last_fencing_token,
       h.current_generation AS instance_current_generation,
       a.workspace_id, a.binding_revision_id, a.policy_revision_id
FROM admission_leases AS l
JOIN admission_attempts AS a
  ON a.operation_id = l.operation_id AND a.attempt_id = l.attempt_id
JOIN engine_instances AS i
  ON i.instance_id = l.instance_id AND i.instance_generation = l.instance_generation
JOIN runtime_instance_heads AS h ON h.instance_id = l.instance_id
WHERE l.lease_id = %s
"""

_SELECT_BARRIER = (
    "SELECT min_fencing_token FROM gateway_fencing_barriers WHERE gateway_id = %s AND pool_id = %s"
)

_SELECT_STREAM_HEAD = (
    "SELECT sample_rate_hz FROM output_stream_heads "
    "WHERE operation_id = %s AND attempt_id = %s AND output_stream_id = %s FOR UPDATE"
)

_INSERT_STREAM_HEAD = (
    "INSERT INTO output_stream_heads "
    "(operation_id, attempt_id, output_stream_id, sample_rate_hz) VALUES (%s, %s, %s, %s)"
)

_INTENT_SELECTION = """
SELECT s.submit_intent_id, s.lease_id, s.operation_id, s.attempt_id, s.command_digest,
       s.request_digest, s.voice_revision_id, s.deadline_at_us, s.recorded_at_us,
       s.updated_at_us, s.execution_state, s.cleanup_state, s.engine_request_id,
       s.dispatched_at_us, s.error_code, s.output_stream_id, s.sample_rate_hz,
       s.hard_output_ceiling_samples, s.generated_pcm_samples, s.staging_output_object_key,
       s.staging_result_document,
       s.version, s.terminated_at_us,
       EXISTS (SELECT 1 FROM output_sample_intervals AS i
               WHERE i.operation_id = s.operation_id AND i.attempt_id = s.attempt_id
                 AND i.output_stream_id = s.output_stream_id
                 AND i.submit_intent_id = s.submit_intent_id) AS has_output_intervals,
       e.evidence_kind, e.evidence_digest,
       l.stage_id, l.pool_id, l.host_node_id, l.gateway_id, l.instance_id,
       l.instance_generation, l.fencing_token,
       a.workspace_id, a.binding_revision_id, a.policy_revision_id
FROM submit_intents AS s
JOIN admission_leases AS l
  ON l.lease_id = s.lease_id AND l.operation_id = s.operation_id AND l.attempt_id = s.attempt_id
JOIN admission_attempts AS a
  ON a.operation_id = s.operation_id AND a.attempt_id = s.attempt_id
LEFT JOIN execution_stop_evidence AS e ON e.submit_intent_id = s.submit_intent_id
"""

_SELECT_INTENT = _INTENT_SELECTION + "WHERE s.submit_intent_id = %s\n"

#: Scope stays fixed; the optional ID cursor and ordering are added at the read boundary.
_SELECT_QUARANTINED = (
    _INTENT_SELECTION + "WHERE s.execution_state = 'unknown_quarantined' "
    "AND l.gateway_id = %s AND l.host_node_id = %s "
)


class SubmitCursor(Protocol):
    """The part of a DB-API cursor these statements use."""

    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def to_unix_micros(value: datetime) -> int:
    """Encode an instant the way the coordination tables store it."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("coordination instants must be timezone aware")
    delta = value.astimezone(UTC) - _EPOCH
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros < 0:
        raise ValueError("coordination instants must not precede the Unix epoch")
    return micros


def from_unix_micros(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def submit_intent_values(intent: SubmissionIntent, request_document: str) -> dict[str, object]:
    """Map a durable intent to its stored columns; the order is the INSERT order."""
    identity = intent.identity
    return {
        "submit_intent_id": identity.submit_intent_id,
        "lease_id": identity.lease_id,
        "operation_id": identity.operation_id,
        "attempt_id": identity.attempt_id,
        "command_digest": identity.command_digest,
        "request_digest": intent.request_digest,
        "request_document": request_document,
        "voice_revision_id": identity.voice_revision_id,
        "deadline_at_us": to_unix_micros(identity.deadline_at),
        "recorded_at_us": to_unix_micros(intent.recorded_at),
        "updated_at_us": to_unix_micros(intent.updated_at),
        "execution_state": intent.execution_state.value,
        "cleanup_state": intent.cleanup_state.value,
        "engine_request_id": intent.engine_request_id,
        "dispatched_at_us": (
            None if intent.dispatched_at is None else to_unix_micros(intent.dispatched_at)
        ),
        "error_code": None if intent.error_code is None else intent.error_code.value,
        "output_stream_id": intent.output_stream_id,
        "sample_rate_hz": identity.sample_rate_hz,
        "hard_output_ceiling_samples": hard_output_ceiling_samples(
            max_output_audio_ms=identity.max_output_audio_ms,
            sample_rate_hz=identity.sample_rate_hz,
        ),
        "version": intent.version,
    }


def submit_intent_update_values(intent: SubmissionIntent) -> dict[str, object]:
    """Map the mutable part of an intent to its stored columns."""
    return {
        "execution_state": intent.execution_state.value,
        "cleanup_state": intent.cleanup_state.value,
        "engine_request_id": intent.engine_request_id,
        "dispatched_at_us": (
            None if intent.dispatched_at is None else to_unix_micros(intent.dispatched_at)
        ),
        "error_code": None if intent.error_code is None else intent.error_code.value,
        "updated_at_us": to_unix_micros(intent.updated_at),
        "version": intent.version,
    }


def _lock_authority_rows(cursor: SubmitCursor, identity: SubmissionIdentity) -> None:
    """Take the pool, instance head, instance, attempt and lease rows in the global order."""
    for statement, parameters, description in (
        (_LOCK_POOL, (identity.pool_id,), "pool"),
        (_LOCK_INSTANCE_HEAD, (identity.instance_id,), "instance head"),
        (
            _LOCK_INSTANCE,
            (identity.instance_id, identity.instance_generation),
            "instance",
        ),
        (
            _LOCK_ATTEMPT,
            (identity.operation_id, identity.attempt_id),
            "attempt",
        ),
        (_LOCK_LEASE, (identity.lease_id,), "lease"),
    ):
        cursor.execute(statement, parameters)
        if len(cursor.fetchall()) != 1:
            raise InvalidExecutionRequestError(f"submit references an unknown {description}")


def _read_lease_facts(cursor: SubmitCursor, lease_id: str) -> LeaseFacts:
    cursor.execute(_SELECT_AUTHORITY, (lease_id,))
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise InvalidExecutionRequestError("submit references an unknown lease or attempt")
    row = rows[0]
    try:
        return LeaseFacts(
            lease_id=row["lease_id"],
            operation_id=row["operation_id"],
            attempt_id=row["attempt_id"],
            stage_id=row["stage_id"],
            workspace_id=row["workspace_id"],
            pool_id=row["pool_id"],
            host_node_id=row["host_node_id"],
            gateway_id=row["gateway_id"],
            instance_id=row["instance_id"],
            instance_generation=row["instance_generation"],
            fencing_token=row["fencing_token"],
            lease_state=LeaseState(row["lease_state"]),
            lease_cleanup_state=LeaseCleanupState(row["lease_cleanup_state"]),
            deadline_at=from_unix_micros(row["deadline_at_us"]),
            instance_state=EngineInstanceState(row["instance_state"]),
            instance_current_generation=row["instance_current_generation"],
            instance_last_fencing_token=row["instance_last_fencing_token"],
            binding_revision_id=row["binding_revision_id"],
            policy_revision_id=row["policy_revision_id"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SubmissionStoreError(
            f"stored lease authority is not readable: {type(error).__name__}"
        ) from None


def _read_sealed_min_fencing_token(
    cursor: SubmitCursor, identity: SubmissionIdentity
) -> int | None:
    cursor.execute(_SELECT_BARRIER, (identity.gateway_id, identity.pool_id))
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise SubmissionStoreError("gateway barrier is not uniquely identified")
    return None if not rows else int(rows[0]["min_fencing_token"])


def _create_output_stream_head(cursor: SubmitCursor, intent: SubmissionIntent) -> None:
    identity = intent.identity
    if identity.sample_rate_hz is None:
        return
    stream_id = intent.output_stream_id
    if stream_id is None:
        raise InvalidExecutionRequestError("an audio stage submits with an output stream id")
    parameters = (identity.operation_id, identity.attempt_id, stream_id)
    cursor.execute(_SELECT_STREAM_HEAD, parameters)
    rows = cursor.fetchall()
    if rows:
        if rows[0]["sample_rate_hz"] != identity.sample_rate_hz:
            raise InvalidExecutionRequestError("the frozen output stream uses another sample rate")
        return
    cursor.execute(_INSERT_STREAM_HEAD, (*parameters, identity.sample_rate_hz))


def create_submit_in_transaction(
    cursor: SubmitCursor,
    intent: SubmissionIntent,
    request_document: str,
    *,
    clock: Callable[[], datetime],
    enforce_binding_barrier: bool = False,
) -> SubmissionIntent | None:
    """Create once, or recover an exact original after waiting for the binding lock."""
    intent.validate_new()
    identity = intent.identity
    if enforce_binding_barrier:
        binding_id, revision = lock_binding(cursor, identity.binding_revision_id)
        existing = read_submit_intent(cursor, identity.submit_intent_id)
        if existing is not None:
            if existing.identity != identity or existing.request_digest != intent.request_digest:
                raise SubmitIntentConflictError("submit intent has another effective request")
            return existing
        check_binding(cursor, binding_id, revision, identity.binding_revision_id)
    _lock_authority_rows(cursor, identity)
    facts = _read_lease_facts(cursor, identity.lease_id)
    verify_submit_identity(
        identity,
        facts,
        sealed_min_fencing_token=_read_sealed_min_fencing_token(cursor, identity),
    )
    verify_submit_eligibility(identity, facts, now=clock())
    values = submit_intent_values(intent, request_document)
    if tuple(values) != SUBMIT_INTENT_INSERT_COLUMNS:
        raise RuntimeError("submit intent insert columns drifted from the statement")
    cursor.execute(_INSERT_INTENT, tuple(values.values()))
    _create_output_stream_head(cursor, intent)
    return None


def read_submit_intent(cursor: SubmitCursor, submit_intent_id: str) -> SubmissionIntent | None:
    """Rebuild the durable intent from its own row and the authority it refers to."""
    cursor.execute(_SELECT_INTENT, (submit_intent_id,))
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise SubmissionStoreError("submit intent does not resolve to exactly one authority")
    return _intent_from_row(rows[0])


def list_quarantined_intents(
    cursor: SubmitCursor,
    *,
    gateway_id: str,
    host_node_id: str,
    limit: int,
    after_submit_intent_id: str | None = None,
) -> list[SubmissionIntent]:
    """Read one strictly increasing ID page without touching unresolved facts.

    Only rows this gateway and node already own are returned, and every row is rebuilt
    through the same projection a single read uses, so a sweep cannot reinterpret an
    unreadable record as a closable one. The immutable ID order avoids cursor movement
    when a concurrent output interval changes a record's version or updated_at.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("a recovery sweep reads between 1 and 500 executions")
    if after_submit_intent_id is not None and (
        not isinstance(after_submit_intent_id, str) or not 1 <= len(after_submit_intent_id) <= 128
    ):
        raise ValueError("a recovery cursor requires a bounded original intent ID")
    suffix = "ORDER BY s.submit_intent_id LIMIT %s\n"
    if after_submit_intent_id is None:
        cursor.execute(_SELECT_QUARANTINED + suffix, (gateway_id, host_node_id, limit))
    else:
        cursor.execute(
            _SELECT_QUARANTINED + "AND s.submit_intent_id > %s " + suffix,
            (gateway_id, host_node_id, after_submit_intent_id, limit),
        )
    return [_intent_from_row(row) for row in cursor.fetchall()]


def save_submit_intent(
    cursor: SubmitCursor,
    intent: SubmissionIntent,
    *,
    expected_version: int,
    registering_result: bool = False,
) -> None:
    """Write the state the caller derived, but only from the version it read."""
    if intent.version != expected_version + 1:
        raise ValueError("a saved intent carries exactly one version beyond the read version")
    if intent.cleanup_state is CleanupState.COMPLETE:
        raise ExecutionCleanupPendingError("terminal writes with cleanup require record_stop")
    intent.validate()
    current = lock_submit_intent(cursor, intent.identity.submit_intent_id)
    if registering_result and (
        current.staging_result is not None
        or current.has_staging_output
        or intent.staging_result is None
        or not intent.has_staging_output
    ):
        raise InvalidExecutionRequestError("result registration is not a first complete receipt")
    if current.version != expected_version:
        raise StaleSubmissionVersionError("submit intent version changed since it was read")
    if current.execution_state.is_terminal:
        raise ExecutionCleanupPendingError("a terminal execution cannot be reversed")
    if (
        current.identity != intent.identity
        or current.request_digest != intent.request_digest
        or current.output_stream_id != intent.output_stream_id
        or current.generated_pcm_samples != intent.generated_pcm_samples
        or (
            not registering_result
            and (
                current.has_staging_output != intent.has_staging_output
                or current.staging_result != intent.staging_result
            )
        )
        or current.has_output_intervals != intent.has_output_intervals
        or (
            current.engine_request_id is not None
            and current.engine_request_id != intent.engine_request_id
        )
        or (current.dispatched_at is not None and current.dispatched_at != intent.dispatched_at)
    ):
        raise InvalidExecutionRequestError("a saved observation changes a frozen execution fact")
    if (
        intent.cleanup_state is CleanupState.NOT_REQUIRED
        and current.execution_state is not ExecutionState.SUBMITTING
    ):
        raise ExecutionCleanupPendingError("only an unsent submission can omit stop evidence")
    values = submit_intent_update_values(intent)
    if tuple(values) != SUBMIT_INTENT_UPDATE_COLUMNS:
        raise RuntimeError("submit intent update columns drifted from the statement")
    cursor.execute(
        _UPDATE_INTENT,
        (
            *(value for name, value in values.items() if name != "version"),
            intent.identity.submit_intent_id,
            expected_version,
        ),
    )
    if cursor.rowcount != 1:
        raise StaleSubmissionVersionError(
            "submit intent was not updated at the version the caller read"
        )


def list_unfinished_intents(
    cursor: SubmitCursor, *, gateway_id: str, host_node_id: str, limit: int
) -> list[SubmissionIntent]:
    """Startup enumerates old active work to quarantine it, never to execute it."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("startup reads between 1 and 500 original executions")
    cursor.execute(
        _INTENT_SELECTION + "WHERE l.gateway_id = %s AND l.host_node_id = %s "
        "AND s.execution_state IN ('submitting', 'executing', 'stopping') "
        "ORDER BY s.submit_intent_id LIMIT %s",
        (gateway_id, host_node_id, limit),
    )
    return [_intent_from_row(row) for row in cursor.fetchall()]


def lock_result_registration(
    cursor: SubmitCursor, identity: SubmissionIdentity, *, clock: Callable[[], datetime]
) -> SubmissionIntent:
    """Use the existing global lock order and recheck authority after every wait."""
    initial = read_submit_intent(cursor, identity.submit_intent_id)
    if initial is None or initial.identity != identity:
        raise InvalidExecutionRequestError("result references another execution")
    _lock_authority_rows(cursor, identity)
    current = lock_submit_intent(cursor, identity.submit_intent_id)
    if current.staging_result is None:
        verify_result_eligibility(
            identity,
            _read_lease_facts(cursor, identity.lease_id),
            sealed_min_fencing_token=_read_sealed_min_fencing_token(cursor, identity),
            now=clock(),
        )
    return current


def check_file_start_in_transaction(
    cursor: SubmitCursor, intent: SubmissionIntent, *, clock: Callable[[], datetime]
) -> SubmissionIntent:
    """Verify a committed acceptance without issuing another execution or updating rows."""
    identity = intent.identity
    _lock_authority_rows(cursor, identity)
    current = lock_submit_intent(cursor, identity.submit_intent_id)
    if (
        current.identity != identity
        or current.request_digest != intent.request_digest
        or current.version != intent.version
        or current.engine_request_id != intent.engine_request_id
        or current.dispatched_at != intent.dispatched_at
    ):
        raise StaleSubmissionVersionError("accepted file execution changed before starting")
    if (
        current.execution_state is not ExecutionState.EXECUTING
        or current.cleanup_state is not CleanupState.PENDING
        or current.staging_result is not None
    ):
        raise ExecutionCleanupPendingError("file execution is no longer eligible to start")
    facts = _read_lease_facts(cursor, identity.lease_id)
    verify_submit_identity(
        identity,
        facts,
        sealed_min_fencing_token=_read_sealed_min_fencing_token(cursor, identity),
    )
    verify_submit_eligibility(identity, facts, now=clock())
    return current


def save_staging_result(cursor: SubmitCursor, intent: SubmissionIntent) -> None:
    result = intent.staging_result
    if result is None:
        raise InvalidExecutionRequestError("result metadata is missing")
    cursor.execute(
        "UPDATE submit_intents SET staging_result_document = %s, staging_output_object_key = %s "
        "WHERE submit_intent_id = %s AND version = %s "
        "AND staging_result_document IS NULL AND staging_output_object_key IS NULL",
        (
            result.document,
            result.reference.object_key,
            intent.identity.submit_intent_id,
            intent.version,
        ),
    )
    if cursor.rowcount != 1:
        raise StaleSubmissionVersionError("result changed before its registration")


def lock_submit_intent(
    cursor: SubmitCursor, submit_intent_id: str, *, lock_lease: bool = False
) -> SubmissionIntent:
    """Lock pool → optional lease → submit, then re-read the frozen authority."""
    cursor.execute(
        "SELECT l.pool_id, l.lease_id FROM submit_intents AS s "
        "JOIN admission_leases AS l ON l.lease_id = s.lease_id "
        "WHERE s.submit_intent_id = %s",
        (submit_intent_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise InvalidExecutionRequestError("unknown submit intent for execution observation")
    pool_id = rows[0]["pool_id"]
    cursor.execute("SELECT pool_id FROM resource_pools WHERE pool_id = %s FOR UPDATE", (pool_id,))
    if not cursor.fetchall():
        raise SubmissionStoreError("execution pool is missing")
    if lock_lease:
        cursor.execute(
            "SELECT lease_id FROM admission_leases WHERE lease_id = %s FOR UPDATE",
            (rows[0]["lease_id"],),
        )
        if not cursor.fetchall():
            raise SubmissionStoreError("execution lease is missing")
    cursor.execute(
        "SELECT submit_intent_id FROM submit_intents WHERE submit_intent_id = %s FOR UPDATE",
        (submit_intent_id,),
    )
    if not cursor.fetchall():
        raise SubmissionStoreError("execution disappeared while acquiring its lock")
    intent = read_submit_intent(cursor, submit_intent_id)
    if (
        intent is None
        or intent.identity.pool_id != pool_id
        or (lock_lease and intent.identity.lease_id != rows[0]["lease_id"])
    ):
        raise SubmissionStoreError("execution authority changed while acquiring its lock")
    return intent


def _intent_from_row(row: dict[str, Any]) -> SubmissionIntent:
    try:
        document = row.get("staging_result_document")
        result = None if document is None else staging_result_from_document(document)
        if result is not None and result.reference.object_key != row["staging_output_object_key"]:
            raise ValueError("stored result pointer disagrees with the receipt")
        sample_rate_hz = row["sample_rate_hz"]
        ceiling = row["hard_output_ceiling_samples"]
        identity = SubmissionIdentity(
            submit_intent_id=row["submit_intent_id"],
            lease_id=row["lease_id"],
            operation_id=row["operation_id"],
            attempt_id=row["attempt_id"],
            stage_id=row["stage_id"],
            workspace_id=row["workspace_id"],
            pool_id=row["pool_id"],
            host_node_id=row["host_node_id"],
            gateway_id=row["gateway_id"],
            instance_id=row["instance_id"],
            instance_generation=row["instance_generation"],
            binding_revision_id=row["binding_revision_id"],
            policy_revision_id=row["policy_revision_id"],
            voice_revision_id=row["voice_revision_id"],
            fencing_token=row["fencing_token"],
            command_digest=row["command_digest"],
            deadline_at=from_unix_micros(row["deadline_at_us"]),
            max_output_audio_ms=(
                None
                if ceiling is None
                else max_output_audio_ms_for(ceiling_samples=ceiling, sample_rate_hz=sample_rate_hz)
            ),
            sample_rate_hz=sample_rate_hz,
        )
        evidence_kind = row["evidence_kind"]
        state = ExecutionState(row["execution_state"])
        if row["cleanup_state"] == CleanupState.COMPLETE and (
            evidence_kind is None
            or evidence_kind in {StopEvidenceKind.NONE, StopEvidenceKind.UNCONFIRMED_TIMEOUT}
            or row["evidence_digest"] is None
            or row["terminated_at_us"] is None
        ):
            raise ValueError("a cleaned-up execution needs a durable verified stop")
        return SubmissionIntent(
            identity=identity,
            recorded_at=from_unix_micros(row["recorded_at_us"]),
            updated_at=from_unix_micros(row["updated_at_us"]),
            request_digest=row["request_digest"],
            output_stream_id=row["output_stream_id"],
            execution_state=state,
            cleanup_state=CleanupState(row["cleanup_state"]),
            dispatched_at=(
                None
                if row["dispatched_at_us"] is None
                else from_unix_micros(row["dispatched_at_us"])
            ),
            engine_request_id=row["engine_request_id"],
            error_code=(
                None if row["error_code"] is None else ExecutionErrorCode(row["error_code"])
            ),
            stop_evidence_kind=(
                StopEvidenceKind.NONE if evidence_kind is None else StopEvidenceKind(evidence_kind)
            ),
            stop_evidence_digest=row["evidence_digest"],
            terminated_at=(
                None
                if row["terminated_at_us"] is None
                else from_unix_micros(row["terminated_at_us"])
            ),
            version=row["version"],
            generated_pcm_samples=row["generated_pcm_samples"],
            has_staging_output=row["staging_output_object_key"] is not None,
            has_output_intervals=bool(row["has_output_intervals"]),
            staging_result=result,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SubmissionStoreError(
            f"stored submit intent is not readable: {type(error).__name__}"
        ) from None
