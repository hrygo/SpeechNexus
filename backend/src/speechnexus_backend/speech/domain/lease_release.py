"""Release only this lease's increments after authoritative execution cleanup."""

from dataclasses import dataclass, replace

from speechnexus_backend.speech.domain.leases import (
    LeaseAccounting,
    LeaseRecord,
    LeaseRejectedError,
)
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.domain.runtime_intents import digest, identifier, integer


@dataclass(frozen=True, slots=True)
class ReleaseLease:
    lease_id: str
    expected_version: int

    def __post_init__(self) -> None:
        identifier(self.lease_id)
        integer(self.expected_version, minimum=1, maximum=MAX_VERSION)


@dataclass(frozen=True, slots=True)
class LeaseStop:
    """B's persisted verified stop, never supplied in a release request."""

    submit_intent_id: str
    kind: str
    evidence_digest: str
    observed_at_us: int

    def __post_init__(self) -> None:
        identifier(self.submit_intent_id)
        digest(self.evidence_digest)
        integer(self.observed_at_us)
        if self.kind not in (
            "engine_abort_ack",
            "engine_terminal_response",
            "instance_terminated_by_controller",
            "operator_verified_instance_terminated",
        ):
            raise ValueError("Unverified lease stop kind")


@dataclass(frozen=True, slots=True)
class SubmitReleaseFacts:
    submit_intent_id: str
    execution_state: str
    cleanup_state: str
    stop: LeaseStop | None = None

    def __post_init__(self) -> None:
        identifier(self.submit_intent_id)
        if self.stop is not None and self.stop.submit_intent_id != self.submit_intent_id:
            raise ValueError("Lease stop belongs to another submit")


def require_complete_stages(required: tuple[str, ...], actual: tuple[str, ...]) -> None:
    if (
        not required
        or len(set(required)) != len(required)
        or len(set(actual)) != len(actual)
        or set(required) != set(actual)
    ):
        raise LeaseRejectedError("reconciliation_required")
    try:
        for stage_id in required:
            identifier(stage_id)
    except ValueError:
        raise LeaseRejectedError("reconciliation_required") from None


def release_already_recorded(lease: LeaseRecord, request: ReleaseLease, now_us: int) -> bool:
    integer(now_us)
    if lease.lease_id != request.lease_id:
        raise LeaseRejectedError("reconciliation_required")
    if lease.lease_state == "released":
        if (
            lease.cleanup_state != "complete"
            or lease.budget_released_at_us is None
            or lease.budget_released_at_us > now_us
        ):
            raise LeaseRejectedError("reconciliation_required")
        return True
    if lease.version != request.expected_version:
        raise LeaseRejectedError("version_conflict")
    if lease.budget_released_at_us is not None:
        raise LeaseRejectedError("reconciliation_required")
    return False


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    lease: LeaseRecord
    accounting: LeaseAccounting
    replayed: bool


def release(
    lease: LeaseRecord,
    accounting: LeaseAccounting,
    request: ReleaseLease,
    *,
    execution: SubmitReleaseFacts | None,
    other_leases_released: bool,
    now_us: int,
) -> ReleaseDecision:
    if release_already_recorded(lease, request, now_us):
        return ReleaseDecision(lease, accounting, True)
    isolated = lease.lease_state == "quarantined"
    if accounting.quota_state == "released" or (
        isolated and accounting.quota_state != "quarantined"
    ):
        raise LeaseRejectedError("reconciliation_required")
    if type(other_leases_released) is not bool:
        raise ValueError("Lease finality must be derived from the locked attempt")
    if execution is None:
        # The original lease lock blocks a future gateway create_submit. An
        # isolated lease must retain its original unknown submission history.
        if isolated:
            raise LeaseRejectedError("execution_cleanup_pending")
    elif execution.execution_state == "failed" and execution.cleanup_state == "not_required":
        # B's native port has proved no dispatch/request ID/staging/output.
        # This authoritative fact cannot discharge a quarantined execution.
        if isolated or execution.stop is not None:
            raise LeaseRejectedError("execution_cleanup_pending")
    else:
        stop = execution.stop
        if (
            execution.execution_state not in ("succeeded", "failed", "aborted")
            or execution.cleanup_state != "complete"
            or stop is None
            or stop.observed_at_us > now_us
            or (
                execution.execution_state == "succeeded" and stop.kind != "engine_terminal_response"
            )
            or (
                isolated
                and (
                    execution.execution_state != "aborted"
                    or stop.kind
                    not in (
                        "instance_terminated_by_controller",
                        "operator_verified_instance_terminated",
                    )
                )
            )
        ):
            raise LeaseRejectedError("execution_cleanup_pending")
    try:
        lease.reserved.require_architecture(accounting.architecture)
        held = accounting.held if isolated else accounting.held.subtract(lease.reserved)
        quarantined = (
            accounting.quarantined.subtract(lease.reserved) if isolated else accounting.quarantined
        )
        held_slots = integer(accounting.held_slots - (0 if isolated else lease.reserved_slots))
        quarantined_slots = integer(
            accounting.quarantined_slots - (lease.reserved_slots if isolated else 0)
        )
        from_held = other_leases_released and accounting.quota_state == "held"
        from_quarantine = other_leases_released and accounting.quota_state == "quarantined"
        held_attempts = integer(accounting.held_attempts - int(from_held))
        quarantined_attempts = integer(accounting.quarantined_attempts - int(from_quarantine))
        if (accounting.quota_state == "held" and accounting.held_attempts < 1) or (
            accounting.quota_state == "quarantined" and accounting.quarantined_attempts < 1
        ):
            raise ValueError("Missing attempt quota")
        versions = tuple(
            integer(value + increment, minimum=1, maximum=MAX_VERSION)
            for value, increment in (
                (accounting.host_version, 1),
                (accounting.instance_version, 1),
                (accounting.attempt_version, int(other_leases_released)),
                (accounting.quota_version, int(other_leases_released)),
                (lease.version, 1),
            )
        )
    except ValueError:
        raise LeaseRejectedError("reconciliation_required") from None
    return ReleaseDecision(
        replace(
            lease,
            lease_state="released",
            cleanup_state="complete",
            version=versions[4],
            budget_released_at_us=now_us,
        ),
        replace(
            accounting,
            held=held,
            quarantined=quarantined,
            held_slots=held_slots,
            quarantined_slots=quarantined_slots,
            held_attempts=held_attempts,
            quarantined_attempts=quarantined_attempts,
            quota_state="released" if other_leases_released else accounting.quota_state,
            host_version=versions[0],
            instance_version=versions[1],
            attempt_version=versions[2],
            quota_version=versions[3],
        ),
        False,
    )
