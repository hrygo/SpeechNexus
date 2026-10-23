"""Erase private command content only under a trusted, execution-scoped deletion permit."""

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionCleanupPendingError,
    InvalidExecutionRequestError,
    LeaseState,
    StaleSubmissionVersionError,
    SubmissionIdentity,
    SubmissionIntent,
    SubmissionStoreError,
    contract_instant_text,
)


class CommandRedactionConflictError(SubmissionStoreError):
    code = "idempotency_conflict"
    recovery = "query_original"


def _check_deletion_scope(deletion_job_id: str, evidence_digest: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", deletion_job_id):
        raise InvalidExecutionRequestError("invalid command deletion identity")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_digest):
        raise InvalidExecutionRequestError("invalid command deletion evidence digest")


@dataclass(frozen=True, slots=True)
class RedactExecutionCommand:
    submit_intent_id: str
    expected_version: int
    deletion_job_id: str
    deletion_evidence_digest: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", self.submit_intent_id):
            raise InvalidExecutionRequestError("invalid command deletion identity")
        _check_deletion_scope(self.deletion_job_id, self.deletion_evidence_digest)
        if type(self.expected_version) is not int or not 1 <= self.expected_version < 2**63:
            raise InvalidExecutionRequestError("command deletion needs a stored version")


@dataclass(frozen=True, slots=True)
class CommandDeletionTombstone:
    deletion_job_id: str
    deletion_evidence_digest: str
    redacted_at: datetime

    def __post_init__(self) -> None:
        contract_instant_text(self.redacted_at)
        _check_deletion_scope(self.deletion_job_id, self.deletion_evidence_digest)


@dataclass(frozen=True, slots=True)
class CommandRedactionResult:
    intent: SubmissionIntent
    tombstone: CommandDeletionTombstone
    replayed: bool


@dataclass(frozen=True, slots=True)
class AuthorizedCommandRedaction:
    """Issued by a trusted coordinator after checking its durable job/dependency scope.

    This value is not a signature and cannot authenticate wire input. The application
    must obtain it from its explicit authorization port, never construct it from a DTO.
    """

    request: RedactExecutionCommand
    identity: SubmissionIdentity

    def __post_init__(self) -> None:
        if self.request.submit_intent_id != self.identity.submit_intent_id:
            raise InvalidExecutionRequestError("deletion permit targets another execution")

    def apply(
        self,
        current: SubmissionIntent,
        *,
        lease_state: LeaseState,
        document_present: bool,
        previous: CommandDeletionTombstone | None,
        at: datetime,
    ) -> CommandRedactionResult:
        request = self.request
        if current.identity != self.identity:
            raise InvalidExecutionRequestError("deletion permit changes the frozen execution")
        if document_present == (previous is not None):
            raise SubmissionStoreError("command body and deletion tombstone disagree")
        if (
            lease_state is not LeaseState.RELEASED
            or not current.execution_state.is_terminal
            or current.cleanup_state not in {CleanupState.COMPLETE, CleanupState.NOT_REQUIRED}
        ):
            raise ExecutionCleanupPendingError("command deletion awaits cleanup and lease release")
        if previous is not None:
            if previous.redacted_at > current.updated_at or previous.redacted_at > at:
                raise SubmissionStoreError("command deletion tombstone is ahead of its execution")
            if (previous.deletion_job_id, previous.deletion_evidence_digest) != (
                request.deletion_job_id,
                request.deletion_evidence_digest,
            ):
                raise CommandRedactionConflictError("command has another deletion tombstone")
            return CommandRedactionResult(current, previous, replayed=True)
        if current.version != request.expected_version:
            raise StaleSubmissionVersionError("execution changed before command deletion")
        if current.version >= 2**63 - 1:
            raise SubmissionStoreError("execution version is exhausted")
        contract_instant_text(at)
        if at < current.updated_at:
            raise SubmissionStoreError("command deletion clock precedes the recorded execution")
        updated = deepcopy(current)
        updated._touch(at)
        return CommandRedactionResult(
            updated,
            CommandDeletionTombstone(request.deletion_job_id, request.deletion_evidence_digest, at),
            replayed=False,
        )
