"""Readiness validity is one fact with three states and two refusal codes.

#828: the readiness receipt had a TTL but nothing described how much of it was
left, and the admission refusal it raised (``reconciliation_required``) was folded
into the generic 503 ``job_unavailable`` storage-outage code. The renewal path and
the distinct public code both rest on the domain keeping three things apart:

* a receipt inside its window, inside the warning window, and past it;
* an elapsed receipt (recoverable: re-sign it) from a receipt that describes a
  different identity or a clock-skewed observation (only a new observation
  reconciles those);
* the controller observation path, which keeps its single
  ``reconciliation_required`` code because that code is part of the internal
  coordination contract.

The two codes are pinned in both directions: the admission path names elapsed
validity, the observation path does not.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from speechnexus_backend.speech.domain.runtime_intents import RuntimeRejectedError
from speechnexus_backend.speech.domain.runtime_observations import (
    DEFAULT_READINESS_FRESHNESS_US,
    READINESS_EXPIRED,
    READINESS_EXPIRING,
    READINESS_UNOBSERVED,
    READINESS_VALID,
    VerifiedRuntimeReadiness,
)
from tests.fake.runtime_intents import PLAN
from tests.fake.runtime_observations import OBSERVATION, READINESS

NOW = 1_000_000_000


def _receipt(*, verified: int, expires: int) -> VerifiedRuntimeReadiness:
    return replace(READINESS, verified_at_us=verified, expires_at_us=expires)


@pytest.mark.parametrize(
    ("now_us", "warning_us", "state"),
    [
        (NOW, 0, READINESS_VALID),
        # The warning window is a reporting threshold: the receipt is still valid
        # and admission still accepts it.
        (NOW, 30_000_000, READINESS_VALID),
        (NOW + 40_000_001, 60_000_000, READINESS_EXPIRING),
        (NOW + 60_000_000, 60_000_000, READINESS_EXPIRED),
    ],
)
def test_the_expiry_state_reports_the_remaining_window(
    now_us: int, warning_us: int, state: str
) -> None:
    receipt = _receipt(verified=NOW, expires=NOW + 60_000_000)
    assert receipt.expiry_state(now_us, warning_us=warning_us) == state


def test_an_elapsed_receipt_is_accepted_until_the_instant_it_expires() -> None:
    receipt = _receipt(verified=NOW - 1, expires=NOW + 10)
    # Admission reads one database clock; the instant of expiry is already late.
    assert _require(receipt, now_us=NOW + 9) is None
    assert (
        _require(receipt, now_us=NOW + 10, expired_code="readiness_expired") == "readiness_expired"
    )


def test_an_elapsed_receipt_keeps_its_own_code_on_the_admission_path() -> None:
    receipt = _receipt(verified=NOW - 1, expires=NOW)
    assert _require(receipt, now_us=NOW, expired_code="readiness_expired") == "readiness_expired"


def test_a_receipt_for_another_identity_is_still_reconciliation_required() -> None:
    receipt = _receipt(verified=NOW - 1, expires=NOW + 10)
    for changed in (
        replace(receipt, fencing_token=receipt.fencing_token + 1),
        replace(receipt, instance_generation=receipt.instance_generation + 1),
        replace(receipt, topology_digest="sha256:" + "f" * 64),
        replace(receipt, qualification_digest="sha256:" + "f" * 64),
        replace(receipt, instance_id="instance-other"),
    ):
        # Identity drift is not a renewal: re-signing cannot fix a receipt that
        # describes another instance, so it must not borrow the recoverable code.
        assert (
            _require(changed, now_us=NOW, expired_code="readiness_expired")
            == "reconciliation_required"
        )


def test_a_future_dated_receipt_is_clock_skew_not_expiry() -> None:
    receipt = _receipt(verified=NOW + 1, expires=NOW + 10)
    assert (
        _require(receipt, now_us=NOW, expired_code="readiness_expired") == "reconciliation_required"
    )


def test_the_observation_path_keeps_the_internal_contract_code() -> None:
    # ``decide_runtime_observation`` passes no override: its code crosses the
    # internal coordination-transaction contract, so the new public code must not
    # leak into it.
    receipt = _receipt(verified=NOW - 1, expires=NOW)
    assert _require(receipt, now_us=NOW) == "reconciliation_required"


def _require(
    receipt: VerifiedRuntimeReadiness,
    *,
    now_us: int,
    expired_code: str | None = None,
) -> str | None:
    """The refusal code ``require_valid`` raises, or ``None`` when the receipt passes."""
    arguments = {} if expired_code is None else {"expired_code": expired_code}
    try:
        receipt.require_valid(OBSERVATION, PLAN, now_us, **arguments)
    except RuntimeRejectedError as error:
        return str(error.code)
    return None


@pytest.mark.parametrize(
    ("observed_seconds_ago", "state"),
    [
        (0, READINESS_VALID),
        # Exactly at the freshness bound is still usable: the rule refuses only a
        # receipt older than the window, so a probe on the boundary admits work.
        (15 * 60, READINESS_VALID),
        (15 * 60 + 1, READINESS_UNOBSERVED),
        # A whole day later the declared window may still have hours left; that is
        # exactly the case that kept admitting work against a stopped engine.
        (24 * 60 * 60, READINESS_UNOBSERVED),
    ],
)
def test_a_receipt_nobody_re_signed_is_unobserved_not_valid(
    observed_seconds_ago: int, state: str
) -> None:
    now_us = NOW + 24 * 60 * 60 * 1_000_000
    verified = now_us - observed_seconds_ago * 1_000_000
    receipt = _receipt(verified=verified, expires=verified + 48 * 60 * 60 * 1_000_000)

    assert (
        receipt.readiness_state(
            now_us,
            freshness_us=DEFAULT_READINESS_FRESHNESS_US,
        )
        == state
    )


def test_admission_refuses_a_stale_observation_with_its_own_code() -> None:
    now_us = NOW + 60 * 60 * 1_000_000
    receipt = _receipt(verified=NOW, expires=NOW + 24 * 60 * 60 * 1_000_000)

    # The declared window still has 23 h left, so the elapsed-window code would be
    # a lie: nobody has probed the engine, which is the fact that refuses the work.
    assert receipt.expiry_state(now_us) == READINESS_VALID
    with pytest.raises(RuntimeRejectedError) as raised:
        receipt.require_valid(OBSERVATION, PLAN, now_us)
    assert raised.value.code == "readiness_unobserved"


def test_an_elapsed_window_outranks_a_stale_observation() -> None:
    """One refusal, and the more specific fact names it."""
    now_us = NOW + 25 * 60 * 60 * 1_000_000
    receipt = _receipt(verified=NOW, expires=NOW + 24 * 60 * 60 * 1_000_000)

    assert receipt.readiness_state(now_us, freshness_us=DEFAULT_READINESS_FRESHNESS_US) == (
        READINESS_EXPIRED
    )
    with pytest.raises(RuntimeRejectedError) as raised:
        receipt.require_valid(OBSERVATION, PLAN, now_us, expired_code="readiness_expired")
    assert raised.value.code == "readiness_expired"


def test_a_caller_that_reports_the_declared_window_alone_keeps_its_vocabulary() -> None:
    """The renewal path reports window states; freshness is an admission rule."""
    now_us = NOW + 60 * 60 * 1_000_000
    receipt = _receipt(verified=NOW, expires=NOW + 24 * 60 * 60 * 1_000_000)

    assert receipt.expiry_state(now_us) == READINESS_VALID
    assert (
        receipt.readiness_state(now_us, freshness_us=DEFAULT_READINESS_FRESHNESS_US)
        == READINESS_UNOBSERVED
    )


def test_the_refusal_sentence_names_the_fact_that_actually_refuses() -> None:
    """One cause class, two recoveries: the text has to follow the instance state."""
    from speechnexus_backend.speech.domain.job_admission_readiness import (
        AdmissionReadinessCause,
        ReadinessInstanceReport,
        refused_readiness,
    )

    def report(refusal_code: str | None) -> Any:
        instance = ReadinessInstanceReport(
            "instance-1", 1, "valid", NOW + 86_400_000_000, 86_400, 8, 0, 0
        )
        return refused_readiness(
            AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED,
            workspace_id="workspace-default",
            now_us=NOW,
            instances=(instance,),
            refusal_code=refusal_code,
        )

    unobserved = report("readiness_unobserved")
    expired = report("readiness_expired")
    assert unobserved.cause is expired.cause is AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED
    assert "No health probe has re-signed" in unobserved.detail
    assert "observed_seconds_ago" in unobserved.detail
    # The elapsed-window sentence must not be reused for a receipt nobody re-signed.
    assert "outside its window" in expired.detail
    assert "outside its window" not in unobserved.detail
