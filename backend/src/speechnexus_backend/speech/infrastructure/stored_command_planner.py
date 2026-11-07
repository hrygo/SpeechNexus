"""Plan a public Job submission from the command frozen at admission.

The admission transaction is the only producer of executable commands: it writes
``admission_operations.request_document`` (the complete business command),
``admission_requests`` (the admission envelope: attempt, stage, lease and
fencing facts) and the lease row in one commit. This planner replays exactly
those rows. It never reads a current binding revision, never fills a default and
never derives a new ``submit_intent_id``: a document that does not already agree
with the admission identity is refused instead of repaired.

Voice generation kinds keep an operation anchor without an attempt or lease; they
are non-dispatchable and are excluded by the queue before this planner runs. When
such a row does reach the planner it is refused rather than admitted again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from speechnexus_contracts.execution import parse

from speechnexus_backend.speech.domain.admission import AdmissionRequest
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchJob,
    DispatchProtocolError,
    DispatchRequestUnavailableError,
    DispatchUnavailableError,
    ExecutionSubmission,
    identifier,
)
from speechnexus_backend.speech.infrastructure.admission_codec import (
    request_with_business_digest,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.transcription_submit import deadline_text

_DIGEST_PREFIX = "sha256:"
_CANCELLING_LEASE_STATES = frozenset({"quarantined"})
_DISPATCHABLE_LEASE_STATES = frozenset({"reserved", "executing"})
_COMMAND_KINDS = {"asr": "asr", "tts": "tts", "voice_validation": "voiceclone"}


@dataclass(frozen=True, slots=True)
class _OperationRow:
    operation_id: str
    workspace_id: str
    request_digest: str
    request_document: str | None
    current_attempt_id: str


@dataclass(frozen=True, slots=True)
class _AttemptRow:
    attempt_id: str
    workspace_id: str
    binding_revision_id: str
    policy_revision_id: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class _RequestRow:
    request_document: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class _LeaseRow:
    lease_id: str
    stage_id: str
    pool_id: str
    host_node_id: str
    gateway_id: str
    instance_id: str
    instance_generation: int
    fencing_token: int
    deadline_at_us: int
    lease_state: str
    cleanup_state: str


@dataclass(frozen=True, slots=True)
class _StoredAdmission:
    operation: _OperationRow
    attempt: _AttemptRow
    request: _RequestRow
    lease: _LeaseRow


def _int(value: Any) -> int:
    if type(value) is not int:
        raise DispatchProtocolError("Stored admission identity is invalid")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise DispatchProtocolError("Stored admission identity is invalid")
    return value


def _digest(document: str) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _document(value: str | None, digest: str) -> dict[str, Any]:
    if not value:
        raise DispatchRequestUnavailableError("The original command is unavailable")
    if _digest(value) != digest:
        raise DispatchProtocolError("The stored command digest does not match its document")
    try:
        document = json.loads(value)
    except ValueError, TypeError, RecursionError:
        raise DispatchProtocolError("The stored command is not a JSON object") from None
    if not isinstance(document, dict):
        raise DispatchProtocolError("The stored command is not a JSON object")
    return document


def _gateway_request(document: Mapping[str, Any]) -> dict[str, Any]:
    value = document.get("gateway_request")
    if not isinstance(value, dict):
        raise DispatchProtocolError("The stored command has no complete gateway request")
    try:
        gateway = parse("SubmitExecutionRequest", value)
    except Exception:
        # The contract validation error carries no content; do not surface it.
        raise DispatchProtocolError(
            "The stored gateway request is outside the frozen contract"
        ) from None
    return cast(dict[str, Any], gateway)


def submission_from_admission(
    job: DispatchJob,
    submit_intent_id: str,
    admission: _StoredAdmission,
) -> ExecutionSubmission:
    """Verify one stored admission against itself, then return its exact command."""
    identifier(submit_intent_id)
    operation, attempt, request, lease = (
        admission.operation,
        admission.attempt,
        admission.request,
        admission.lease,
    )
    command_document = _document(operation.request_document, operation.request_digest)
    envelope_document = _document(request.request_document, request.request_digest)
    try:
        envelope: AdmissionRequest = request_with_business_digest(
            envelope_document, operation.request_digest
        )
    except ValueError, TypeError, KeyError:
        raise DispatchProtocolError("The stored admission envelope is invalid") from None

    gateway = _gateway_request(command_document)
    command = gateway.get("command")
    if not isinstance(command, dict):
        raise DispatchProtocolError("The stored command is not a complete execution command")

    if len(envelope.stages) != 1:
        raise DispatchProtocolError("A public Job admission must describe exactly one stage")
    stage = envelope.stages[0]
    authority = next((a for a in envelope.authorities if a.pool_id == stage.pool_id), None)
    if authority is None:
        raise DispatchProtocolError("The stored admission has no authority for its stage")

    if lease.lease_state in _CANCELLING_LEASE_STATES:
        # A quarantined execution has an unknown outcome; dispatching would
        # create a second execution for the same attempt.
        raise DispatchUnavailableError("The admission lease is quarantined")
    if lease.lease_state not in _DISPATCHABLE_LEASE_STATES or lease.cleanup_state != "pending":
        raise DispatchProtocolError("The admission lease cannot accept a submit")

    expected = {
        "submit_intent_id": submit_intent_id,
        "operation_id": job.job_id,
        "attempt_id": operation.current_attempt_id,
        "stage_id": stage.stage_id,
        "lease_id": stage.lease_id,
        "workspace_id": operation.workspace_id,
        "pool_id": stage.pool_id,
        "host_node_id": stage.host_node_id,
        "gateway_id": stage.gateway_id,
        "instance_id": stage.instance_id,
        "binding_revision_id": attempt.binding_revision_id,
        "policy_revision_id": attempt.policy_revision_id,
        "fencing_token": authority.fencing_token,
        "deadline_at": deadline_text(envelope.deadline_at_us),
    }
    actual = {
        "submit_intent_id": gateway.get("submit_intent_id"),
        "operation_id": gateway.get("operation_id"),
        "attempt_id": gateway.get("attempt_id"),
        "stage_id": gateway.get("stage_id"),
        "lease_id": gateway.get("lease_id"),
        "workspace_id": gateway.get("workspace_id"),
        "pool_id": gateway.get("pool_id"),
        "host_node_id": gateway.get("host_node_id"),
        "gateway_id": gateway.get("gateway_id"),
        "instance_id": gateway.get("instance_id"),
        "binding_revision_id": gateway.get("binding_revision_id"),
        "policy_revision_id": gateway.get("policy_revision_id"),
        "fencing_token": gateway.get("fencing_token"),
        "deadline_at": gateway.get("deadline_at"),
    }
    if actual != expected:
        raise DispatchProtocolError("The stored command disagrees with its admission identity")

    # The admission row is the authority for stage/lease identity; the envelope
    # and the executable lease must agree with it without exception.
    if (
        operation.operation_id != job.job_id
        or operation.workspace_id != job.workspace_id
        or attempt.attempt_id != operation.current_attempt_id
        or attempt.workspace_id != operation.workspace_id
        or request.request_digest != attempt.request_digest
        or stage.pool_id != lease.pool_id
        or stage.host_node_id != lease.host_node_id
        or stage.gateway_id != lease.gateway_id
        or stage.instance_id != lease.instance_id
        or stage.instance_generation != lease.instance_generation
        or stage.stage_id != lease.stage_id
        or stage.lease_id != lease.lease_id
        or authority.fencing_token != lease.fencing_token
        or lease.deadline_at_us != envelope.deadline_at_us
        or gateway.get("instance_generation") != stage.instance_generation
        or _int(lease.instance_generation) != stage.instance_generation
        or _int(lease.fencing_token) != authority.fencing_token
    ):
        raise DispatchProtocolError("The stored command disagrees with its admission rows")

    if command.get("kind") != _COMMAND_KINDS.get(job.kind):
        raise DispatchProtocolError("The stored command belongs to another Job kind")

    return ExecutionSubmission(submit_intent_id, cast(Mapping[str, Any], gateway))


class StoredCommandPlanner:
    """Read one frozen admission in a bounded read-only transaction."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Stored command plans require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    async def plan(self, job: DispatchJob, submit_intent_id: str) -> ExecutionSubmission:
        return await asyncio.to_thread(self._plan, job, submit_intent_id)

    def _plan(self, job: DispatchJob, submit_intent_id: str) -> ExecutionSubmission:
        return submission_from_admission(job, submit_intent_id, self._read(job))

    def _read(self, job: DispatchJob) -> _StoredAdmission:
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout',%s,true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                operations = connection.execute(
                    "SELECT operation_id, workspace_id, request_digest, request_document, "
                    "current_attempt_id FROM admission_operations WHERE operation_id = %s",
                    (job.job_id,),
                ).fetchall()
                if len(operations) != 1:
                    raise DispatchRequestUnavailableError(
                        "The Job has no complete admission operation"
                    )
                operation = _OperationRow(
                    _text(operations[0]["operation_id"]),
                    _text(operations[0]["workspace_id"]),
                    _text(operations[0]["request_digest"]),
                    operations[0]["request_document"],
                    _text(operations[0]["current_attempt_id"]),
                )
                if operation.workspace_id != job.workspace_id:
                    raise DispatchProtocolError("The admission workspace is not this Job")
                attempts = connection.execute(
                    "SELECT attempt_id, workspace_id, binding_revision_id, policy_revision_id, "
                    "request_digest FROM admission_attempts "
                    "WHERE operation_id = %s AND attempt_id = %s",
                    (operation.operation_id, operation.current_attempt_id),
                ).fetchall()
                if len(attempts) != 1:
                    raise DispatchRequestUnavailableError(
                        "The Job admission has no attempt to dispatch"
                    )
                attempt = _AttemptRow(
                    _text(attempts[0]["attempt_id"]),
                    _text(attempts[0]["workspace_id"]),
                    _text(attempts[0]["binding_revision_id"]),
                    _text(attempts[0]["policy_revision_id"]),
                    _text(attempts[0]["request_digest"]),
                )
                requests = connection.execute(
                    "SELECT request_document, request_digest FROM admission_requests "
                    "WHERE operation_id = %s AND attempt_id = %s",
                    (operation.operation_id, attempt.attempt_id),
                ).fetchall()
                if len(requests) != 1:
                    raise DispatchRequestUnavailableError(
                        "The Job admission has no stored request to plan"
                    )
                request = _RequestRow(
                    _text(requests[0]["request_document"]),
                    _text(requests[0]["request_digest"]),
                )
                leases = connection.execute(
                    "SELECT lease_id, stage_id, host_node_id, instance_id, instance_generation, "
                    "pool_id, gateway_id, fencing_token, deadline_at_us, lease_state, "
                    "cleanup_state FROM admission_leases "
                    "WHERE operation_id = %s AND attempt_id = %s ORDER BY lease_id",
                    (operation.operation_id, attempt.attempt_id),
                ).fetchall()
                if len(leases) != 1:
                    raise DispatchRequestUnavailableError(
                        "The Job admission has no single lease to dispatch"
                    )
                lease = _LeaseRow(
                    _text(leases[0]["lease_id"]),
                    _text(leases[0]["stage_id"]),
                    _text(leases[0]["pool_id"]),
                    _text(leases[0]["host_node_id"]),
                    _text(leases[0]["gateway_id"]),
                    _text(leases[0]["instance_id"]),
                    _int(leases[0]["instance_generation"]),
                    _int(leases[0]["fencing_token"]),
                    _int(leases[0]["deadline_at_us"]),
                    _text(leases[0]["lease_state"]),
                    _text(leases[0]["cleanup_state"]),
                )
                return _StoredAdmission(operation, attempt, request, lease)
        except DispatchRequestUnavailableError, DispatchProtocolError, DispatchUnavailableError:
            raise
        except Exception:
            raise DispatchUnavailableError("The stored admission cannot be read") from None


__all__ = ["StoredCommandPlanner", "submission_from_admission"]
