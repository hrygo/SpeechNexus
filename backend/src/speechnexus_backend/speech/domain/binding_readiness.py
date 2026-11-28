"""One binding's readiness answer, and the only place its three facts are decided.

``/v1/models`` reports ``engine_ready``, ``quality_qualified`` and
``admission_state`` for every published binding. Before this module existed those
three values were the ``ReadinessFacts`` defaults (``false``/``false``/
``unavailable``), so a listed model could never look ready no matter what the
deployment had proved, and the public transcription route collapsed every refusal
into one unattributable code (#1295).

The three answers are decided here once, because two exits ask the same question:
the catalogue projection and the admission path. Two implementations would drift
first on the boundary (``>=`` versus ``>``) and then on the meaning of "ready", the
same way the CLI and the admission path once disagreed about one receipt (#1246).

``engine_ready``
    A successful prepare plus a health probe, and nothing else. In this codebase
    that pair is *recorded* by the runtime readiness receipt: the receipt is
    written by the renewal that ran the prepare and signed the probe
    (``runtime_instance_intents.readiness_document``), and it is valid only while
    it is unexpired and no later void applies to it. The receipt is therefore the
    evidence, never a manual flag -- there is no operator-set boolean here, and
    this module rejects one: :class:`EngineFacts` has no ``operator_approved``
    field to set.

``quality_qualified``
    The binding's own quality evidence: a recorded qualification run whose
    measured word-error rate is within the threshold that run declared. Three
    values, each with one meaning:

    * ``qualified`` -- evidence exists and the measured rate is within threshold.
      ``quality_qualified`` is ``true``; the gate is satisfied.
    * ``unverified`` -- **no evidence at all**. ``quality_qualified`` is ``false``
      and the flag is *raised*, not blocking: phase 1 keeps an experimental
      binding callable and says out loud that its quality is unproven. This is a
      deliberate choice, not a missing check -- :class:`QualityEvidence` requires
      the caller to state which of the three it means, so "absent" can never be
      spelled as a silently passing constant.
    * ``failed`` -- evidence exists but the measured rate is outside the declared
      threshold, or the evidence the binding cites cannot be read. This *does*
      block: a run that measured the binding and failed is a fact about the
      deployment, and admitting on top of it would contradict the measurement.

``admission_state``
    Derived from the two above, in one function (:func:`admission_state`), so the
    mapping cannot be re-implemented at a call site. A binding is ``available``
    only when the engine is ready and quality is not ``failed``; everything else
    is ``unavailable``. ``queueable``/``capacity_full`` remain runtime capacity
    answers, which a per-binding projection cannot know (the contract keeps
    admission a per-workspace live answer).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

AVAILABLE: Final = "available"
UNAVAILABLE: Final = "unavailable"

QUALITY_QUALIFIED: Final = "qualified"
QUALITY_UNVERIFIED: Final = "unverified"
QUALITY_FAILED: Final = "failed"

QualityBasis = Literal["qualified", "unverified", "failed"]
QualityState = str


@dataclass(frozen=True, slots=True)
class EngineFacts:
    """What the runtime readiness receipt says about the instance generation.

    ``prepare_probe_succeeded`` is the receipt's own outcome: the renewal that
    wrote it completed the prepare step and signed a health observation. The two
    ``receipt_*`` fields are the window facts, and ``voided_at_us`` is the latest
    void that applies to this generation (``None`` when no void exists). There is
    deliberately no operator-set field: readiness is measured, never declared.
    """

    prepare_probe_succeeded: bool
    receipt_expires_at_us: int | None
    voided_at_us: int | None
    observed_at_us: int

    def __post_init__(self) -> None:
        if type(self.prepare_probe_succeeded) is not bool:
            raise ValueError("Engine readiness must be an explicit boolean")
        for name in ("receipt_expires_at_us", "voided_at_us", "observed_at_us"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Invalid {name}")


@dataclass(frozen=True, slots=True)
class QualityEvidence:
    """The binding's recorded quality run, stated in one of the three shapes.

    A caller must name the basis, so "no evidence" cannot be confused with "passed
    by default". ``measured_wer`` and ``threshold_wer`` are word-error rates in
    ``[0, 1]`` and are required exactly when evidence exists.
    """

    basis: QualityState
    measured_wer: float | None = None
    threshold_wer: float | None = None
    observed_at_us: int | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.basis not in (QUALITY_QUALIFIED, QUALITY_UNVERIFIED, QUALITY_FAILED):
            raise ValueError("Unknown quality basis")
        if self.basis == QUALITY_UNVERIFIED:
            if self.measured_wer is not None or self.threshold_wer is not None:
                raise ValueError("Unverified quality must not carry a measurement")
            return
        if not 0.0 <= (self.measured_wer or -1.0) <= 1.0:
            raise ValueError("Invalid measured word-error rate")
        if not 0.0 <= (self.threshold_wer or -1.0) <= 1.0:
            raise ValueError("Invalid quality threshold")


@dataclass(frozen=True, slots=True)
class BindingReadiness:
    """The projection one binding exposes, with the facts that decided it."""

    engine_ready: bool
    quality_qualified: bool
    admission_state: str
    quality_basis: str
    engine_reason: str | None = None
    quality_detail: str | None = None
    observed_at_us: int = 0

    def __post_init__(self) -> None:
        if type(self.engine_ready) is not bool or type(self.quality_qualified) is not bool:
            raise ValueError("Readiness flags must be explicit booleans")
        if self.admission_state not in (AVAILABLE, UNAVAILABLE):
            raise ValueError("Unknown admission state")

    @property
    def blocks_admission(self) -> bool:
        """Whether this binding must refuse a create right now."""
        return self.admission_state != AVAILABLE


def engine_ready(facts: EngineFacts, *, now_us: int) -> tuple[bool, str | None]:
    """Whether a successful prepare plus health probe backs this generation.

    The void comparison delegates to :func:`void_invalidates_receipt` so that this
    answer and the admission transaction cannot disagree about the same instant.
    """
    from speechnexus_backend.speech.domain.readiness_void import void_invalidates_receipt

    if not facts.prepare_probe_succeeded:
        return False, "prepare_probe_not_succeeded"
    if facts.receipt_expires_at_us is None:
        return False, "receipt_absent"
    if facts.receipt_expires_at_us <= now_us:
        return False, "receipt_expired"
    if void_invalidates_receipt(
        voided_at_us=facts.voided_at_us,
        receipt_verified_at_us=facts.observed_at_us,
    ):
        return False, "receipt_voided"
    return True, None


def quality_qualified(evidence: QualityEvidence) -> tuple[bool, str | None]:
    """Whether the recorded quality run qualifies, and why not when it does not."""
    if evidence.basis == QUALITY_QUALIFIED:
        return True, None
    if evidence.basis == QUALITY_UNVERIFIED:
        return False, "quality_evidence_absent"
    return False, evidence.detail or "quality_below_threshold"


def admission_state(*, engine: bool, quality: bool, quality_basis: QualityState) -> str:
    """The single mapping point from the two facts to one public answer.

    ``unverified`` quality does not block phase 1; a run that measured the binding
    and failed does (#1295). ``available`` therefore means "the engine is ready and
    no measurement contradicts admitting work here".
    """
    if not engine:
        return UNAVAILABLE
    if quality_basis == QUALITY_FAILED:
        return UNAVAILABLE
    return AVAILABLE


def binding_readiness(
    *,
    engine: EngineFacts,
    quality: QualityEvidence,
    now_us: int,
) -> BindingReadiness:
    """Assemble one binding's answer; every caller goes through this function."""
    ready, engine_reason = engine_ready(engine, now_us=now_us)
    qualified, quality_detail = quality_qualified(quality)
    return BindingReadiness(
        engine_ready=ready,
        quality_qualified=qualified,
        admission_state=admission_state(
            engine=ready, quality=qualified, quality_basis=quality.basis
        ),
        quality_basis=quality.basis,
        engine_reason=engine_reason,
        quality_detail=quality_detail,
        observed_at_us=now_us,
    )


__all__ = [
    "AVAILABLE",
    "QUALITY_FAILED",
    "QUALITY_QUALIFIED",
    "QUALITY_UNVERIFIED",
    "UNAVAILABLE",
    "BindingReadiness",
    "EngineFacts",
    "QualityEvidence",
    "admission_state",
    "binding_readiness",
    "engine_ready",
    "quality_qualified",
]
