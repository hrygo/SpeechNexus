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

This module holds the vocabulary only. The projection that reads the facts
``decide_admission`` reads is the admission layer's job, and it must reuse that
predicate rather than compare the stored values a second time.
"""

from __future__ import annotations

from enum import StrEnum

from speechnexus_backend.speech.domain.admission import PUBLIC_ADMISSION_CODES

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
