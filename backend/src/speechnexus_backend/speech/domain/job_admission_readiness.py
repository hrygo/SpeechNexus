"""The closed vocabulary of the Job admission readiness report.

``GET /v1/nexus/jobs/admission-readiness`` answers one verdict about one instant:
``status`` is ``admitting`` if and only if ``cause`` is empty, and that holds if and
only if a create sent right then would be admitted. The report names at most one
refusal class because a create answers exactly one public code: a set of causes
could only add a precedence the create path does not have (#845).

Two invariants are owned here, and both are pinned by
``tests/contract/test_job_admission_readiness.py`` in both directions:

* the cause enum is exactly the set of public create codes that mean "this
  deployment refuses every create" (``CAUSE_CODES.values()`` equals
  ``interfaces.http.job_errors.DEPLOYMENT_STATE_CODES``), so a class cannot be
  missing from the report and a cause cannot name a code the create operation does
  not declare;
* every cause names exactly one declared code, so the report and a create cannot
  answer different questions about the same deployment state.

The projection that reads those facts lives in the admission layer and calls
``decide_create_admission`` -- the same function a create calls -- so the report
never compares stored values a second time. This module owns only the vocabulary,
the report value and the mapping from a refusal code to its cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from speechnexus_backend.speech.domain.admission import (
    CAPACITY_REJECTION_CODES,
    PUBLIC_ADMISSION_CODES,
    AdmissionQuota,
)
from speechnexus_backend.speech.domain.public_jobs import identifier
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_EXPIRED as READINESS_EXPIRED,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_EXPIRING as READINESS_EXPIRING,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_UNOBSERVED as READINESS_UNOBSERVED,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_VALID as READINESS_VALID,
)

# The public codes the report points an operator at. The admission vocabulary is the
# single source for the three admission-owned answers; the capacity, workspace and
# storage answers are declared by the HTTP Job error table, which the interface
# layer owns, so they are named here and pinned there by the contract test.
ENGINE_NOT_READY_CODE = PUBLIC_ADMISSION_CODES["readiness_expired"]
ENGINE_STATE_MISMATCH_CODE = PUBLIC_ADMISSION_CODES["reconciliation_required"]
ENGINE_RESOURCE_DISABLED_CODE = PUBLIC_ADMISSION_CODES["policy_or_resource_disabled"]
CAPACITY_EXHAUSTED_CODE = "job_capacity_exhausted"
WORKSPACE_NOT_FOUND_CODE = "workspace_not_found"
WORKSPACE_DISABLED_CODE = "workspace_disabled"
WORKSPACE_NOT_CONFIGURED_CODE = "workspace_not_configured"
STORAGE_UNAVAILABLE_CODE = "job_unavailable"


class AdmissionReadinessStatus(StrEnum):
    """Whether a create would be admitted, as the one value a poller asserts on."""

    ADMITTING = "admitting"
    DEGRADED = "degraded"


class AdmissionReadinessCause(StrEnum):
    """Why a create would not be admitted, one class per operator action."""

    ENGINE_RECEIPT_EXPIRED = "engine_receipt_expired"
    ENGINE_STATE_MISMATCH = "engine_state_mismatch"
    ENGINE_RESOURCE_DISABLED = "engine_resource_disabled"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_DISABLED = "workspace_disabled"
    WORKSPACE_NOT_CONFIGURED = "workspace_not_configured"
    STORAGE_UNAVAILABLE = "storage_unavailable"


# cause -> the one public code a create answers for it.
CAUSE_CODES: dict[AdmissionReadinessCause, str] = {
    AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED: ENGINE_NOT_READY_CODE,
    AdmissionReadinessCause.ENGINE_STATE_MISMATCH: ENGINE_STATE_MISMATCH_CODE,
    AdmissionReadinessCause.ENGINE_RESOURCE_DISABLED: ENGINE_RESOURCE_DISABLED_CODE,
    AdmissionReadinessCause.CAPACITY_EXHAUSTED: CAPACITY_EXHAUSTED_CODE,
    AdmissionReadinessCause.WORKSPACE_NOT_FOUND: WORKSPACE_NOT_FOUND_CODE,
    AdmissionReadinessCause.WORKSPACE_DISABLED: WORKSPACE_DISABLED_CODE,
    AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED: WORKSPACE_NOT_CONFIGURED_CODE,
    AdmissionReadinessCause.STORAGE_UNAVAILABLE: STORAGE_UNAVAILABLE_CODE,
}


# The public Job code each cause answers, inverted once so the projection reads a
# refusal the create path produced instead of comparing facts a second time.
CAUSE_BY_PUBLIC_CODE: dict[str, AdmissionReadinessCause] = {
    code: cause for cause, code in CAUSE_CODES.items()
}

# The four states a receipt can be classified into live with the classification
# itself; only the two "there is no usable receipt at all" states are specific to
# this report, so the spellings cannot drift apart.
READINESS_MISSING = "missing"
READINESS_INVALID = "invalid"

DEFAULT_READINESS_WARNING_SECONDS = 21_600
"""A quarter of the 24 h default receipt TTL: one renewal interval of warning.

A reporting threshold only: an ``expiring`` receipt still admits work, so it is
never a cause. Shared with ``speechnexus-engine-readiness`` so the console report
and this surface classify the same window.
"""

READINESS_REPORT_CODE = "speechnexus-engine-readiness"
RENEW_COMMAND = "speechnexus-register-engine --renew-readiness"
REGISTER_COMMAND = "speechnexus-register-engine"


class UnclassifiedRefusalError(Exception):
    """An admission code outside the closed report vocabulary.

    The contract declares which refusals this surface classifies: request-shaped
    answers, single-request answers and the 500 ``admission_state_invalid`` defect
    are deliberately not causes. Reaching one here means the probe produced an
    answer the report cannot name, which is a defect in the probe rather than a
    verdict to publish.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"unclassified admission refusal: {code!r}")
        self.code = code


def cause_for_refusal(code: str) -> AdmissionReadinessCause:
    """Name the cause behind one internal admission refusal code.

    The internal vocabulary is folded to the public code first
    (``PUBLIC_ADMISSION_CODES``) and the public code to its cause
    (``CAUSE_BY_PUBLIC_CODE``), so both directions use the shipped tables instead
    of a second list that could drift.
    """
    # The capacity class is the one group with no entry in the internal->public
    # table: a create answers it as the 429 ``job_capacity_exhausted`` code, so the
    # report folds it through that same declared code instead of restating it.
    if code in CAPACITY_REJECTION_CODES:
        public = "job_capacity_exhausted"
    else:
        public = PUBLIC_ADMISSION_CODES.get(code, code)
    cause = CAUSE_BY_PUBLIC_CODE.get(public)
    if cause is None:
        raise UnclassifiedRefusalError(code)
    return cause


@dataclass(frozen=True, slots=True)
class ReadinessInstanceReport:
    """The public per-instance projection: receipt window plus slot headroom."""

    instance_id: str
    instance_generation: int
    readiness_state: str
    readiness_expires_at_us: int | None
    remaining_seconds: int | None
    max_slots: int
    held_slots: int
    quarantined_slots: int

    @property
    def receipt_usable(self) -> bool:
        return self.readiness_state in (READINESS_VALID, READINESS_EXPIRING)


@dataclass(frozen=True, slots=True)
class AdmissionReadiness:
    """One immutable verdict plus the facts that produced it.

    ``status`` and ``cause`` are one answer split in two: ``admitting`` holds if
    and only if ``cause`` is ``None``, which holds if and only if
    ``decide_create_admission`` admitted the probe command built from these rows.
    """

    status: AdmissionReadinessStatus
    cause: AdmissionReadinessCause | None
    workspace_id: str
    now_us: int
    detail: str | None
    instances: tuple[ReadinessInstanceReport, ...]
    quota: AdmissionQuota | None

    def __post_init__(self) -> None:
        if (self.status is AdmissionReadinessStatus.ADMITTING) != (self.cause is None):
            raise ValueError("An admitting verdict is exactly the absent cause")
        if self.status is AdmissionReadinessStatus.DEGRADED and self.detail is None:
            raise ValueError("A degraded verdict names its cause and recovery")

    @property
    def admitting(self) -> bool:
        return self.status is AdmissionReadinessStatus.ADMITTING


def _detail(
    cause: AdmissionReadinessCause,
    *,
    workspace_id: str,
    workspace_state: str | None,
    quota: AdmissionQuota | None,
    instances: tuple[ReadinessInstanceReport, ...] = (),
) -> str:
    code = CAUSE_CODES[cause]
    if cause is AdmissionReadinessCause.WORKSPACE_NOT_FOUND:
        return (
            f"Workspace {workspace_id} has no catalog row, so a create answers {code} "
            "before admission is consulted."
        )
    if cause is AdmissionReadinessCause.WORKSPACE_DISABLED:
        return (
            f"Workspace {workspace_id} is not active (state {workspace_state!r}), so a "
            f"create answers {code}."
        )
    if cause is AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED:
        return (
            "The workspace has no quota row, so admission has nothing to charge and a "
            f"create answers {code}; registering an engine for this workspace writes one."
        )
    if cause is AdmissionReadinessCause.CAPACITY_EXHAUSTED:
        if (
            quota is not None
            and quota.held_attempts + quota.quarantined_attempts >= quota.max_attempts
        ):
            return (
                "The workspace quota holds every attempt "
                f"(held {quota.held_attempts} + quarantined {quota.quarantined_attempts} of "
                f"{quota.max_attempts}); a create answers {code} until one is released."
            )
        return (
            "No ready engine instance can take another slot, so a create answers "
            f"{code} until an instance becomes ready or a slot is released."
        )
    if cause is AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED:
        # One cause class covers two different recoveries, so the sentence follows the
        # per-instance state instead of restating the enum member's name: a receipt
        # nobody re-signed is not an elapsed window, and an operator who reads "outside
        # its window" would look in the wrong place (#1263).
        if any(instance.readiness_state == READINESS_UNOBSERVED for instance in instances):
            return (
                "No health probe has re-signed this instance's readiness receipt inside the "
                f"freshness window, so a create answers {code}. A receipt states what was "
                "true when it was signed, not that the engine is still there: check the "
                f"renewal task and the engine, then run `{RENEW_COMMAND}` (the read-only "
                f"report `{READINESS_REPORT_CODE}` prints observed_seconds_ago)."
            )
        return (
            "The ready instance a create would pin holds a readiness receipt outside its "
            f"window, so a create answers {code}; re-sign it with `{RENEW_COMMAND}` (the "
            f"read-only report `{READINESS_REPORT_CODE}` prints the remaining window)."
        )
    if cause is AdmissionReadinessCause.ENGINE_STATE_MISMATCH:
        return (
            "The pinned instance's stored intent no longer matches admission's expectation, "
            f"so a create answers {code}; reconcile or re-register it with "
            f"`{REGISTER_COMMAND}`."
        )
    if cause is AdmissionReadinessCause.STORAGE_UNAVAILABLE:
        return (
            "The coordination store could not be read, so admission cannot be evaluated and "
            f"a create answers {code}."
        )
    return (
        "The pinned instance, its pool, its node or the approved recipe is not active, so a "
        f"create answers {code} until it is re-enabled or re-approved."
    )


def admitted_readiness(
    *,
    workspace_id: str,
    now_us: int,
    instances: tuple[ReadinessInstanceReport, ...],
    quota: AdmissionQuota | None,
) -> AdmissionReadiness:
    return AdmissionReadiness(
        AdmissionReadinessStatus.ADMITTING,
        None,
        workspace_id,
        now_us,
        None,
        instances,
        quota,
    )


def refused_readiness(
    cause: AdmissionReadinessCause,
    *,
    workspace_id: str,
    now_us: int,
    instances: tuple[ReadinessInstanceReport, ...] = (),
    quota: AdmissionQuota | None = None,
    workspace_state: str | None = None,
) -> AdmissionReadiness:
    return AdmissionReadiness(
        AdmissionReadinessStatus.DEGRADED,
        cause,
        workspace_id,
        now_us,
        _detail(
            cause,
            workspace_id=workspace_id,
            workspace_state=workspace_state,
            quota=quota,
            instances=instances,
        ),
        instances,
        quota,
    )


def storage_unavailable_readiness(
    *,
    workspace_id: str,
    now_us: int,
) -> AdmissionReadiness:
    """The report when the store could not be read at all.

    ``storage_unavailable`` is the only cause: with no facts there is nothing else
    this surface may claim, and reporting ``admitting`` because nothing was
    observed would be the same lie as a green healthcheck over a broken surface.
    """
    identifier(workspace_id)
    return refused_readiness(
        AdmissionReadinessCause.STORAGE_UNAVAILABLE, workspace_id=workspace_id, now_us=now_us
    )
