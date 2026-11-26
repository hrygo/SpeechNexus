"""One judgement of one stored readiness receipt.

Three callers ask the same question about the same stored receipt: the admission
transaction (may this instance admit work right now?), the operator report
(``speechnexus-engine-readiness``) and ``GET /v1/nexus/jobs/admission-readiness``.
Asking it in two read paths let the CLI answer ``valid`` while admission answered
``invalid`` for the same row (#1246): the CLI classified the window alone and the
HTTP path additionally compared the receipt's identity against the immutable plan.

The judgement therefore lives here, once, and every caller reads its answer. The
verdict always names *which* fact decided it:

``missing``
    No receipt is stored for the intent at all.
``invalid``
    The receipt does not decode, or its identity disagrees with the immutable
    runtime plan (``reason`` names the disagreeing fields).
``valid`` / ``expiring`` / ``expired`` / ``unobserved``
    The declared window, and -- when a freshness rule is supplied -- how recently
    a health probe re-signed it.

``reason`` is for operators and logs. It never reaches a public response, where a
collapsed code is the contract, and it is what turns "invalid" into something an
operator can act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeIntent,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    VerifiedRuntimeReadiness,
)

READINESS_MISSING: Final = "missing"
READINESS_INVALID: Final = "invalid"


@dataclass(frozen=True, slots=True)
class ReadinessVerdict:
    """The single answer both exits report, plus why it holds."""

    state: str
    expires_at_us: int | None
    remaining_us: int | None
    reason: str | None = None


def mismatched_identity_fields(
    receipt: VerifiedRuntimeReadiness,
    request: RuntimeIntent,
    plan: ApprovedRuntimePlan,
) -> tuple[str, ...]:
    """The identity fields where the stored receipt disagrees with its plan.

    Naming the fields is the difference between "invalid" and an operator action:
    a ``fencing_token`` mismatch means the receipt was signed for another pool
    epoch, while a ``topology_digest`` mismatch means the plan itself changed.
    """
    mismatched: list[str] = []
    if receipt.binding != request.binding:
        mismatched.append("binding")
    if receipt.binding != plan.binding:
        mismatched.append("plan_binding")
    if receipt.instance_id != request.instance_id:
        mismatched.append("instance_id")
    if receipt.instance_generation != request.instance_generation:
        mismatched.append("instance_generation")
    if receipt.fencing_token != request.fencing_token:
        mismatched.append("fencing_token")
    if receipt.topology_digest != plan.topology_digest:
        mismatched.append("topology_digest")
    if receipt.qualification_digest != plan.qualification_digest:
        mismatched.append("qualification_digest")
    if request.action != "prepare":
        mismatched.append("action")
    return tuple(mismatched)


def readiness_verdict(
    receipt: VerifiedRuntimeReadiness | None,
    *,
    request: RuntimeIntent | None,
    plan: ApprovedRuntimePlan | None,
    now_us: int,
    warning_us: int,
    freshness_us: int | None = None,
    reason: str | None = None,
) -> ReadinessVerdict:
    """Classify one decoded receipt (or its absence) against one clock.

    ``request``/``plan`` are the stored immutable facts admission compares
    against. Passing ``None`` asks the window question alone, which only the
    renewal path does: it re-signs the receipt it is looking at, so there is no
    second identity to check yet.
    """
    if receipt is None:
        return ReadinessVerdict(
            READINESS_MISSING,
            None,
            None,
            reason or "no readiness receipt is stored for the prepare intent",
        )
    if request is not None and plan is not None:
        mismatched = mismatched_identity_fields(receipt, request, plan)
        if mismatched:
            # No window is reported: a receipt that describes another plan admits
            # nothing, and printing its expiry would read as capacity that exists.
            return ReadinessVerdict(
                READINESS_INVALID,
                None,
                None,
                "the stored receipt does not describe this runtime plan: "
                + ", ".join(mismatched)
                + " disagree",
            )
    state = receipt.readiness_state(
        now_us,
        warning_us=warning_us,
        freshness_us=freshness_us,
    )
    return ReadinessVerdict(
        state,
        receipt.expires_at_us,
        receipt.expires_at_us - now_us,
        None,
    )
