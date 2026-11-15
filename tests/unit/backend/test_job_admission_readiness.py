"""The admission report classifies the refusal cause instead of naming "unavailable".

An elapsed engine readiness receipt refuses every public Job create while the
process-health surface stays green (#845). The report's whole value is that its
causes are a closed, machine-readable enum with one operator action each, so these
cases pin the classification rather than a message: an elapsed receipt, exhausted
capacity, an unreadable store and an unprovisioned quota must never collapse into
one answer, and a window that still admits work (`expiring`) must not be reported
as a refusal.

The last group drives the same classification through ``PublicJobs`` with an
injected store, so the application's storage-failure fallback -- a report, not an
exception -- is pinned without a database.
"""

from __future__ import annotations

import pytest

from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_admission_readiness import (
    CAPACITY_EXHAUSTED_CODE,
    ENGINE_NOT_READY_CODE,
    STORAGE_UNAVAILABLE_CODE,
    WORKSPACE_NOT_CONFIGURED_CODE,
    AdmissionReadiness,
    AdmissionReadinessCause,
    AdmissionReadinessFacts,
    AdmissionReadinessStatus,
    ReadyInstanceFacts,
    WorkspaceQuotaFacts,
    classify_admission_readiness,
    storage_unavailable_readiness,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.domain.runtime_intents import RuntimeBinding
from speechnexus_backend.speech.domain.runtime_observations import VerifiedRuntimeReadiness

NOW_US = 1_700_000_000_000_000
DAY_US = 86_400_000_000
WORKSPACE = "workspace-default"


def _binding() -> RuntimeBinding:
    return RuntimeBinding(
        pool_id="asr-batch",
        target_node_id="host-1",
        gateway_id="gateway-1",
        approved_template_id="template-asr-1",
        binding_revision_id="binding-revision-1",
        manifest_id="manifest-asr-1",
        config_digest="sha256:" + "a" * 64,
    )


def _receipt(*, verified_at_us: int, expires_at_us: int) -> VerifiedRuntimeReadiness:
    return VerifiedRuntimeReadiness(
        binding=_binding(),
        instance_id="instance-a",
        instance_generation=1,
        fencing_token=1,
        topology_digest="sha256:" + "b" * 64,
        qualification_digest="sha256:" + "c" * 64,
        evidence_digest="sha256:" + "d" * 64,
        verified_at_us=verified_at_us,
        expires_at_us=expires_at_us,
    )


def _valid_instance() -> ReadyInstanceFacts:
    return ReadyInstanceFacts(
        instance_id="instance-a",
        instance_generation=1,
        readiness=_receipt(verified_at_us=NOW_US - DAY_US, expires_at_us=NOW_US + DAY_US),
        has_readiness_document=True,
        max_slots=4,
        held_slots=0,
        quarantined_slots=0,
    )


def _quota(*, max_attempts: int = 8, held: int = 0, quarantined: int = 0) -> WorkspaceQuotaFacts:
    return WorkspaceQuotaFacts(
        workspace_id=WORKSPACE,
        max_attempts=max_attempts,
        held_attempts=held,
        quarantined_attempts=quarantined,
    )


_DEFAULT = object()


def _facts(
    *,
    instances: tuple[ReadyInstanceFacts, ...] | object = _DEFAULT,
    quota: WorkspaceQuotaFacts | object | None = _DEFAULT,
    now_us: int = NOW_US,
) -> AdmissionReadinessFacts:
    # ``None`` is a meaningful value for both facts (no instance registered, no
    # quota row), so the sentinel distinguishes "use the healthy default" from
    # "model an absent row".
    return AdmissionReadinessFacts(
        workspace_id=WORKSPACE,
        now_us=now_us,
        instances=(_valid_instance(),) if instances is _DEFAULT else instances,  # type: ignore[arg-type]
        quota=_quota() if quota is _DEFAULT else quota,  # type: ignore[arg-type]
    )


def _causes(report: AdmissionReadiness) -> list[str]:
    return [cause.value for cause in report.causes]


def test_a_valid_receipt_and_free_capacity_report_that_a_create_is_admitted() -> None:
    report = classify_admission_readiness(_facts())

    assert report.status is AdmissionReadinessStatus.ADMITTING
    assert report.admitting
    assert report.causes == ()
    # An admitting report carries no sentence: nothing is degraded to explain.
    assert report.detail is None
    assert [instance.readiness_state for instance in report.instances] == ["valid"]
    assert report.instances[0].remaining_seconds == DAY_US // 1_000_000
    assert report.usable_instances == 1


def test_an_expiring_receipt_still_admits_and_is_not_a_cause() -> None:
    # `expiring` is a reporting threshold inside the warning window, never an
    # admission rule: only an elapsed receipt refuses work.
    report = classify_admission_readiness(
        _facts(
            instances=(
                ReadyInstanceFacts(
                    instance_id="instance-a",
                    instance_generation=1,
                    readiness=_receipt(
                        verified_at_us=NOW_US - DAY_US, expires_at_us=NOW_US + 3_600_000_000
                    ),
                    has_readiness_document=True,
                    max_slots=4,
                    held_slots=0,
                    quarantined_slots=0,
                ),
            )
        )
    )

    assert report.status is AdmissionReadinessStatus.ADMITTING
    assert [instance.readiness_state for instance in report.instances] == ["expiring"]


def test_an_elapsed_receipt_is_the_engine_cause_and_names_the_renewal() -> None:
    report = classify_admission_readiness(
        _facts(
            instances=(
                ReadyInstanceFacts(
                    instance_id="instance-a",
                    instance_generation=1,
                    readiness=_receipt(
                        verified_at_us=NOW_US - 2 * DAY_US, expires_at_us=NOW_US - 1
                    ),
                    has_readiness_document=True,
                    max_slots=4,
                    held_slots=0,
                    quarantined_slots=0,
                ),
            )
        )
    )

    assert report.status is AdmissionReadinessStatus.DEGRADED
    assert _causes(report) == ["engine_receipt_expired"]
    assert report.instances[0].readiness_state == "expired"
    assert report.instances[0].remaining_seconds == -1
    # The sentence names the create's code and the renewal, so an operator reading
    # the report is not sent to another command.
    assert ENGINE_NOT_READY_CODE in (report.detail or "")
    assert "speechnexus-register-engine --renew-readiness" in (report.detail or "")
    assert "speechnexus-engine-readiness" in (report.detail or "")


@pytest.mark.parametrize(
    ("document_present", "state"),
    [(False, "missing"), (True, "invalid")],
)
def test_a_receipt_admission_cannot_read_is_the_engine_cause(
    document_present: bool, state: str
) -> None:
    # A receipt that is absent and one that no longer decodes are both unusable for
    # admission; the report keeps the two states apart for the operator.
    report = classify_admission_readiness(
        _facts(
            instances=(
                ReadyInstanceFacts(
                    instance_id="instance-a",
                    instance_generation=1,
                    readiness=None,
                    has_readiness_document=document_present,
                    max_slots=4,
                    held_slots=0,
                    quarantined_slots=0,
                ),
            )
        )
    )

    assert _causes(report) == ["engine_receipt_expired"]
    assert report.instances[0].readiness_state == state
    assert report.instances[0].readiness_expires_at_us is None
    assert report.instances[0].remaining_seconds is None


def test_no_ready_instance_is_the_engine_cause_and_says_so() -> None:
    report = classify_admission_readiness(_facts(instances=()))

    assert _causes(report) == ["engine_receipt_expired"]
    assert report.instances == ()
    assert "No ready engine instance is registered" in (report.detail or "")


def test_one_usable_instance_of_two_keeps_the_report_admitting() -> None:
    # Admission needs one instance that can take the request; a stale sibling is
    # reported in `instances` without degrading the verdict.
    stale = ReadyInstanceFacts(
        instance_id="instance-b",
        instance_generation=2,
        readiness=_receipt(verified_at_us=NOW_US - 2 * DAY_US, expires_at_us=NOW_US - 1),
        has_readiness_document=True,
        max_slots=4,
        held_slots=0,
        quarantined_slots=0,
    )
    report = classify_admission_readiness(_facts(instances=(_valid_instance(), stale)))

    assert report.status is AdmissionReadinessStatus.ADMITTING
    assert [instance.readiness_state for instance in report.instances] == ["valid", "expired"]
    assert report.usable_instances == 1


def test_an_exhausted_quota_is_the_capacity_cause_not_a_storage_one() -> None:
    report = classify_admission_readiness(
        _facts(quota=_quota(max_attempts=2, held=1, quarantined=1))
    )

    assert _causes(report) == ["capacity_exhausted"]
    assert CAPACITY_EXHAUSTED_CODE in (report.detail or "")
    assert STORAGE_UNAVAILABLE_CODE not in (report.detail or "")
    assert report.quota is not None and report.quota.exhausted


def test_an_exhausted_instance_slot_is_the_capacity_cause() -> None:
    report = classify_admission_readiness(
        _facts(
            instances=(
                ReadyInstanceFacts(
                    instance_id="instance-a",
                    instance_generation=1,
                    readiness=_receipt(
                        verified_at_us=NOW_US - DAY_US, expires_at_us=NOW_US + DAY_US
                    ),
                    has_readiness_document=True,
                    max_slots=4,
                    held_slots=3,
                    quarantined_slots=1,
                ),
            )
        )
    )

    assert _causes(report) == ["capacity_exhausted"]
    assert "instance-a" in (report.detail or "")


def test_a_half_held_instance_is_not_a_capacity_cause() -> None:
    # One free slot admits a one-slot request: only "no single-slot request fits"
    # is capacity exhaustion.
    report = classify_admission_readiness(
        _facts(
            instances=(
                ReadyInstanceFacts(
                    instance_id="instance-a",
                    instance_generation=1,
                    readiness=_receipt(
                        verified_at_us=NOW_US - DAY_US, expires_at_us=NOW_US + DAY_US
                    ),
                    has_readiness_document=True,
                    max_slots=4,
                    held_slots=3,
                    quarantined_slots=0,
                ),
            )
        )
    )

    assert report.status is AdmissionReadinessStatus.ADMITTING


def test_an_unprovisioned_workspace_is_its_own_cause() -> None:
    report = classify_admission_readiness(_facts(quota=None))

    assert _causes(report) == ["workspace_not_provisioned"]
    assert WORKSPACE_NOT_CONFIGURED_CODE in (report.detail or "")
    assert report.quota is None


def test_two_refusals_are_reported_together_in_a_stable_order() -> None:
    # A deployment both past its window and out of quota must not hide one cause
    # behind the other: a create answers whichever it reaches first, while the
    # report names the classes.
    report = classify_admission_readiness(
        _facts(
            instances=(
                ReadyInstanceFacts(
                    instance_id="instance-a",
                    instance_generation=1,
                    readiness=_receipt(
                        verified_at_us=NOW_US - 2 * DAY_US, expires_at_us=NOW_US - 1
                    ),
                    has_readiness_document=True,
                    max_slots=4,
                    held_slots=0,
                    quarantined_slots=0,
                ),
            ),
            quota=_quota(max_attempts=1, held=1),
        )
    )

    assert _causes(report) == ["engine_receipt_expired", "capacity_exhausted"]


def test_an_unreadable_store_reports_only_the_storage_cause() -> None:
    # With no facts read, nothing else may be claimed: reporting "admitting"
    # because nothing was observed is the same lie as a green healthcheck.
    report = storage_unavailable_readiness(workspace_id=WORKSPACE, now_us=NOW_US)

    assert report.status is AdmissionReadinessStatus.DEGRADED
    assert _causes(report) == ["storage_unavailable"]
    assert STORAGE_UNAVAILABLE_CODE in (report.detail or "")
    assert report.instances == ()
    assert report.quota is None


class _FactsStore:
    """One injected store: optionally refusing the readiness read."""

    def __init__(self, facts: AdmissionReadinessFacts | None) -> None:
        self._facts = facts
        self.reads = 0

    def admission_readiness_facts(self, workspace_id: str) -> AdmissionReadinessFacts:
        self.reads += 1
        if self._facts is None:
            raise PublicJobUnavailableError
        return self._facts

    def now_us(self) -> int:
        return NOW_US


def _jobs(store: _FactsStore) -> PublicJobs:
    return PublicJobs(store, store, readiness=store)  # type: ignore[arg-type]


def test_the_application_reports_a_store_outage_instead_of_raising() -> None:
    jobs = _jobs(_FactsStore(None))

    report = jobs.admission_readiness()

    assert _causes(report) == ["storage_unavailable"]
    assert report.workspace_id == WORKSPACE
    assert STORAGE_UNAVAILABLE_CODE in (report.detail or "")


def test_the_application_classifies_the_facts_it_read() -> None:
    store = _FactsStore(_facts(quota=_quota(max_attempts=1, held=1)))
    jobs = _jobs(store)

    report = jobs.admission_readiness()

    assert _causes(report) == ["capacity_exhausted"]
    assert store.reads == 1


def test_the_application_refuses_a_malformed_workspace_identity() -> None:
    # A caller error is not a degraded deployment: the identity check happens
    # before the store is read.
    store = _FactsStore(_facts())

    with pytest.raises(ValueError, match="Invalid public Job identity"):
        _jobs(store).admission_readiness(workspace_id="Not A Workspace")

    assert store.reads == 0


def test_a_deployment_without_the_read_port_refuses_instead_of_guessing() -> None:
    # An assembly that never wired the report cannot produce a verdict. Claiming one
    # (either `admitting` or a cause) would be a fabricated observation, so the
    # application raises the storage refusal and the route answers the declared 503.
    jobs = PublicJobs(_FactsStore(_facts()), _FactsStore(_facts()))  # type: ignore[arg-type]

    with pytest.raises(PublicJobUnavailableError):
        jobs.admission_readiness()


def test_every_cause_has_one_public_code() -> None:
    # The enum is the machine-readable contract; a cause without a code would be a
    # sentence pretending to be a signal.
    from speechnexus_backend.speech.domain.job_admission_readiness import CAUSE_CODES

    assert set(CAUSE_CODES) == set(AdmissionReadinessCause)
    assert len(set(CAUSE_CODES.values())) == len(AdmissionReadinessCause)
