"""The report and the create must answer one question, not two (#845 review).

An earlier revision of this surface re-derived "is admission open" from its own SQL:
it read pool, quota and receipt columns and compared them itself. That is the #845
defect one layer up -- the report could say ``admitting`` while a create answered
``429 job_capacity_exhausted``, because the two implementations only agreed on the
states the tests covered (a draining pool was the measured counter-example).

The classification now runs ``deployment_refusals``, the same predicate
``require_live_authority`` and ``decide_admission`` raise from, so the cases below are
*equivalence* cases: for one fixture state they call the create path and the report and
assert the two verdicts agree, cause code to public code. A new admission rule that
skips the shared predicate fails here instead of drifting in production.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from speechnexus_backend.speech.domain.admission import (
    PUBLIC_ADMISSION_CODES,
    AdmissionQuota,
    AdmissionRejectedError,
    decide_admission,
    deployment_refusals,
    require_live_authority,
)
from speechnexus_backend.speech.domain.job_admission_readiness import (
    CAUSE_BY_REFUSAL_CODE,
    CAUSE_CODES,
    AdmissionReadiness,
    AdmissionReadinessCause,
    AdmissionReadinessFacts,
    AdmissionReadinessStatus,
    classify_admission_readiness,
    storage_unavailable_readiness,
)
from speechnexus_backend.speech.infrastructure.public_job_admission import rejection_code
from tests.fake.admission import COMMAND, HOSTS, POOLS, QUOTA, RUNTIMES
from tests.fake.runtime_intents import NOW

WORKSPACE = "workspace-test"
# Re-exported for the contract test: one fixture matrix means the HTTP surface is
# pinned against the same facts the equivalence cases use.
__all__ = ["NOW", "WORKSPACE", "_facts", "_receipt"]


def _quota(*, max_attempts: int = 4, held: int = 0, quarantined: int = 0) -> AdmissionQuota:
    return replace(
        QUOTA,
        max_attempts=max_attempts,
        held_attempts=held,
        quarantined_attempts=quarantined,
        version=1,
    )


def _runtime(
    *, hidden_slots: int = 0, quarantined_slots: int = 0, instance_state: str = "ready"
) -> Any:
    (runtime,) = RUNTIMES
    return replace(
        runtime,
        instance=replace(
            runtime.instance,
            held_slots=hidden_slots,
            quarantined_slots=quarantined_slots,
            instance_state=instance_state,
        ),
    )


def _facts(
    *,
    quota: AdmissionQuota | None = None,
    runtimes: tuple[Any, ...] | None = None,
    pools: tuple[Any, ...] | None = None,
    workspace_state: str | None = "active",
    undecodable: tuple[str, ...] = (),
    expired: bool = False,
) -> AdmissionReadinessFacts:
    if expired:
        runtimes = (replace(RUNTIMES[0], readiness=replace(_receipt(), expires_at_us=NOW - 1)),)
    return AdmissionReadinessFacts(
        workspace_id=WORKSPACE,
        workspace_state=workspace_state,
        quota=_quota() if quota is None else quota,
        pools=POOLS if pools is None else pools,
        runtimes=RUNTIMES if runtimes is None else runtimes,
        undecodable=undecodable,
        now_us=NOW,
    )


def _create_code(facts: AdmissionReadinessFacts) -> str | None:
    """The public code a real create answers in this state, ``None`` when admitted.

    The order is the transaction's: ``require_live_authority`` runs first, then
    ``decide_admission``, exactly as ``admit_in_transaction`` calls them.
    """
    quota = facts.quota
    assert quota is not None
    if not facts.runtimes:
        # The create path selects a pinned ready instance before admission runs and
        # answers the capacity refusal when none exists
        # (`public_job_admission._instance`), so the empty case never reaches the
        # decision below.
        return "job_capacity_exhausted"
    try:
        require_live_authority(COMMAND, facts.pools, facts.runtimes, facts.now_us)
        decide_admission(
            COMMAND,
            quota=quota,
            pools=facts.pools,
            hosts=HOSTS,
            runtimes=facts.runtimes,
            now_us=facts.now_us,
        )
    except AdmissionRejectedError as error:
        # The create path's own mapping, so this test cannot disagree with the API.
        return rejection_code(error.code)
    return None


def _receipt() -> Any:
    (runtime,) = RUNTIMES
    return runtime.readiness


# One row per deployment state. `draining` is the review's measured counter-example:
# the create answers 429 while the old report said `admitting`, causes `[]`.
_STATES: dict[str, dict[str, Any]] = {
    "healthy": {},
    "pool_draining": {"pools": (replace(POOLS[0], pool_state="draining"),)},
    "pool_owner_reconciling": {
        "pools": (
            replace(
                POOLS[0],
                ownership=replace(POOLS[0].ownership, owner_state="reconciling_takeover"),
            ),
        ),
    },
    "receipt_elapsed": {
        "runtimes": (replace(RUNTIMES[0], readiness=replace(_receipt(), expires_at_us=NOW - 1)),)
    },
    "quota_full": {"quota": _quota(held=4)},
    "quota_quarantined": {"quota": _quota(held=3, quarantined=1)},
    "slots_full": {"runtimes": (_runtime(hidden_slots=4),)},
    "instance_stopped": {"runtimes": (_runtime(instance_state="stopped"),)},
    "no_ready_instance": {"runtimes": ()},
    "undecodable_instance": {
        "runtimes": (
            replace(RUNTIMES[0], instance=replace(RUNTIMES[0].instance, instance_state="stopped")),
        ),
    },
}


@pytest.mark.parametrize("state", sorted(_STATES))
def test_the_report_answers_exactly_what_a_create_answers(state: str) -> None:
    facts = _facts(**_STATES[state])
    report = classify_admission_readiness(facts)
    create_code = _create_code(facts)
    if create_code is None:
        assert report.admitting, (
            f"{state}: a create succeeds but the report refuses with "
            f"{[cause.value for cause in report.causes]}"
        )
        assert report.causes == ()
        return
    assert not report.admitting, (
        f"{state}: a create answers {create_code} but the report says admitting"
    )
    codes = [CAUSE_CODES[cause] for cause in report.causes]
    assert create_code in codes, (
        f"{state}: a create answers {create_code} while the report names {codes}"
    )


def test_a_draining_pool_is_reported_instead_of_admitting() -> None:
    # The measured counter-example, pinned on its own so a regression names itself.
    facts = _facts(pools=(replace(POOLS[0], pool_state="draining"),))
    report = classify_admission_readiness(facts)

    assert _create_code(facts) == "engine_resource_disabled"
    assert report.status is AdmissionReadinessStatus.DEGRADED
    assert report.causes == (AdmissionReadinessCause.ENGINE_RESOURCE_DISABLED,)


def test_an_elapsed_receipt_is_the_only_class_that_asks_for_renewal() -> None:
    facts = _facts(
        runtimes=(replace(RUNTIMES[0], readiness=replace(_receipt(), expires_at_us=NOW - 1)),)
    )
    report = classify_admission_readiness(facts)

    assert _create_code(facts) == "engine_not_ready"
    assert report.causes == (AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED,)
    assert "speechnexus-register-engine --renew-readiness" in (report.detail or "")


def test_an_undecodable_instance_is_a_state_mismatch_not_an_outage() -> None:
    report = classify_admission_readiness(_facts(runtimes=(), undecodable=("instance-a",)))

    assert report.causes == (AdmissionReadinessCause.ENGINE_STATE_MISMATCH,)
    assert "engine_state_mismatch" in (report.detail or "")


def test_no_ready_instance_is_the_capacity_refusal_the_create_answers() -> None:
    # The create path answers `job_capacity_exhausted` when no ready instance exists;
    # the report names the same class instead of claiming a readiness window.
    report = classify_admission_readiness(_facts(runtimes=()))

    assert report.causes == (AdmissionReadinessCause.CAPACITY_EXHAUSTED,)
    assert "job_capacity_exhausted" in (report.detail or "")


def test_capacity_detail_names_the_counter_that_is_full() -> None:
    report = classify_admission_readiness(_facts(quota=_quota(held=4)))

    assert report.causes == (AdmissionReadinessCause.CAPACITY_EXHAUSTED,)
    assert "held 4" in (report.detail or "")


def test_healthy_facts_are_admitting_with_no_detail() -> None:
    report = classify_admission_readiness(_facts())

    assert report.status is AdmissionReadinessStatus.ADMITTING
    assert report.causes == () and report.detail is None
    assert report.usable_instances == 1


@pytest.mark.parametrize(
    ("workspace_state", "quota", "cause"),
    [
        (None, None, AdmissionReadinessCause.WORKSPACE_NOT_FOUND),
        ("disabled", None, AdmissionReadinessCause.WORKSPACE_DISABLED),
        ("active", None, AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED),
    ],
)
def test_the_three_workspace_refusals_stay_distinct(
    workspace_state: str | None, quota: AdmissionQuota | None, cause: AdmissionReadinessCause
) -> None:
    # Merge them and the operator cannot tell "create the workspace" from "enable it"
    # from "register an engine so a quota row exists" (#845 review).
    facts = AdmissionReadinessFacts(
        workspace_id=WORKSPACE,
        workspace_state=workspace_state,
        quota=quota,
        pools=POOLS,
        runtimes=RUNTIMES,
        undecodable=(),
        now_us=NOW,
    )
    report = classify_admission_readiness(facts)

    assert report.causes == (cause,)
    assert CAUSE_CODES[cause] in (report.detail or "")


def test_storage_unavailable_is_the_only_cause_when_nothing_was_read() -> None:
    report = storage_unavailable_readiness(workspace_id=WORKSPACE, now_us=NOW)

    assert report.causes == (AdmissionReadinessCause.STORAGE_UNAVAILABLE,)
    assert report.instances == () and report.quota is None


def test_every_predicate_code_has_a_reported_cause() -> None:
    # The report may not drop a refusal the create path can produce: a missing entry
    # here is `KeyError` in production, and a cause added for a code the predicate
    # never returns is a class no create can confirm.
    observed: set[str] = set()
    for state in _STATES:
        facts = _facts(**_STATES[state])
        if facts.quota is None:
            continue
        observed.update(
            deployment_refusals(
                pools=facts.pools,
                runtimes=facts.runtimes,
                quota=facts.quota,
                now_us=facts.now_us,
            )
        )
    assert observed <= set(CAUSE_BY_REFUSAL_CODE), (
        "the shared predicate returns a refusal the report cannot name: "
        f"{sorted(observed - set(CAUSE_BY_REFUSAL_CODE))}"
    )
    # ... and the fixture matrix reaches every class the report can produce, so the
    # equivalence cases above are not a subset of the classes an operator can see.
    produced = {CAUSE_BY_REFUSAL_CODE[code] for code in CAUSE_BY_REFUSAL_CODE if code in observed}
    assert produced == {
        AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED,
        AdmissionReadinessCause.ENGINE_STATE_MISMATCH,
        AdmissionReadinessCause.ENGINE_RESOURCE_DISABLED,
        AdmissionReadinessCause.CAPACITY_EXHAUSTED,
    }


def test_cause_codes_are_the_declared_public_vocabulary() -> None:
    # The one place each code is declared, not a copied string.
    for cause, code in CAUSE_CODES.items():
        assert isinstance(code, str) and code, cause
    assert (
        CAUSE_CODES[AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED]
        == (PUBLIC_ADMISSION_CODES["readiness_expired"])
    )
    assert (
        CAUSE_CODES[AdmissionReadinessCause.ENGINE_STATE_MISMATCH]
        == (PUBLIC_ADMISSION_CODES["reconciliation_required"])
    )
    assert (
        CAUSE_CODES[AdmissionReadinessCause.ENGINE_RESOURCE_DISABLED]
        == (PUBLIC_ADMISSION_CODES["policy_or_resource_disabled"])
    )


def test_two_instances_report_admitting_when_one_chain_is_clean() -> None:
    # A create pins one instance per stage, so one usable instance is enough; the
    # worse instance must not turn a working deployment into a degraded verdict.
    (runtime,) = RUNTIMES
    elapsed = replace(runtime, readiness=replace(_receipt(), expires_at_us=NOW - 1))
    report = classify_admission_readiness(_facts(runtimes=(elapsed, runtime)))

    assert report.admitting
    assert report.causes == ()
    assert report.usable_instances == 1


def test_every_instance_refusing_names_the_least_bad_chain() -> None:
    (runtime,) = RUNTIMES
    elapsed = replace(runtime, readiness=replace(_receipt(), expires_at_us=NOW - 1))
    report: AdmissionReadiness = classify_admission_readiness(
        _facts(
            runtimes=(elapsed, replace(runtime, instance=replace(runtime.instance, held_slots=4)))
        )
    )

    # Both chains refuse and neither is clean; the reported chain is the
    # deterministic tie-break (fewest refusals, then the lowest codes), so the
    # verdict names one class instead of hiding the degradation behind a worse set.
    assert report.causes == (AdmissionReadinessCause.CAPACITY_EXHAUSTED,)
