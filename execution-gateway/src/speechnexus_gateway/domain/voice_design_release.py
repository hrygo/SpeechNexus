"""What may prove a VoiceDesign instance released its GPU memory.

A container exit or a terminated engine process is release evidence. The engine
sleep endpoint is not: on the verified vLLM-Omni image ``POST /v1/omni/sleep``
answered ``SUCCESS`` with ``freed_bytes=0`` and the next inference timed out
after 300 s, so the acknowledgment neither released memory nor kept the endpoint
usable. Callers must stop the container or terminate the process and keep the
capacity until that stop is verified elsewhere.

This is the one voice-design release gate, and it writes nothing: a receipt wraps the
node's checkpoint-backed stop fact and validates it here, and the write path that turns a
verified stop into an ``execution_stop_evidence`` row is the recovery sweep's
``whole_instance_stop_evidence`` converter plus ``record_stop`` -- not this module. The
receipt's ``verified_stop`` hands that converter exactly the fact it validated, so both
paths write the same ``evidence_id``/``evidence_digest``.

The idle reclaim decision does *not* read this receipt: it reads the node's checkpoint
projection directly (``NodeStopEvidenceSource.release_evidence``) and takes the release
fact from it, because reclaim runs before any execution row exists to convert. What the
two paths share is the node fact, not a Python object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from speechnexus_gateway.domain.recovery import (
    InstanceTerminationEvidence,
    whole_instance_stop_evidence,
)
from speechnexus_gateway.domain.submission import (
    StopEvidenceKind,
    SubmissionIntent,
    VerifiedStopEvidence,
)


class ReleaseEvidenceKind(StrEnum):
    """How a stop was attempted; only process/container termination can release."""

    ENGINE_SLEEP_ACK = "engine_sleep_ack"
    PROCESS_EXIT = "process_exit"
    CONTAINER_STOP = "container_stop"


class ReleaseEvidenceError(ValueError):
    """A claim that cannot release a VoiceDesign reservation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VoiceDesignReleaseEvidence:
    """One observed stop attempt and the exact facts that make it usable.

    ``freed_bytes`` is recorded for an engine sleep acknowledgment only so that a
    zero-byte answer stays visible as *not* released. It never releases by itself.
    """

    kind: ReleaseEvidenceKind
    observed_at: datetime
    exited: bool = False
    release_confirmed: bool = False
    freed_bytes: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, ReleaseEvidenceKind)
            or not isinstance(self.observed_at, datetime)
            or type(self.exited) is not bool
            or type(self.release_confirmed) is not bool
            or (
                self.freed_bytes is not None
                and (type(self.freed_bytes) is not int or self.freed_bytes < 0)
            )
        ):
            raise ValueError("invalid VoiceDesign release evidence")
        if self.kind is not ReleaseEvidenceKind.ENGINE_SLEEP_ACK and self.freed_bytes is not None:
            raise ValueError("only an engine sleep acknowledgment reports freed bytes")

    @classmethod
    def engine_sleep_ack(
        cls, *, observed_at: datetime, status: str, freed_bytes: int
    ) -> VoiceDesignReleaseEvidence:
        """Record the engine answer without promoting it to release evidence."""
        if type(status) is not str or not status:
            raise ValueError("an engine sleep acknowledgment requires its status")
        return cls(
            kind=ReleaseEvidenceKind.ENGINE_SLEEP_ACK,
            observed_at=observed_at,
            exited=False,
            release_confirmed=False,
            freed_bytes=freed_bytes,
        )

    @property
    def is_release_evidence(self) -> bool:
        """Only a confirmed exit of the process or container releases memory."""
        return (
            self.kind in {ReleaseEvidenceKind.PROCESS_EXIT, ReleaseEvidenceKind.CONTAINER_STOP}
            and self.exited
            and self.release_confirmed
        )

    def require_release(self) -> None:
        """Refuse anything that is not a verified process/container termination."""
        if self.kind is ReleaseEvidenceKind.ENGINE_SLEEP_ACK:
            raise ReleaseEvidenceError("engine_sleep_is_not_release")
        if not self.is_release_evidence:
            raise ReleaseEvidenceError("release_unconfirmed")


@dataclass(frozen=True, slots=True)
class VoiceDesignStopReceipt:
    """A stop receipt that may release only from verified termination evidence.

    Production builds it from the node controller's checkpoint-backed stop fact, so the
    release kind is always a process/container termination. ``termination`` is the source
    fact the node reported, retained so the receipt can hand the shared converter exactly
    what it validated instead of a re-derived summary.
    """

    submit_intent_id: str
    release: VoiceDesignReleaseEvidence
    termination: InstanceTerminationEvidence | None = None

    def __post_init__(self) -> None:
        if type(self.submit_intent_id) is not str or not 1 <= len(self.submit_intent_id) <= 128:
            raise ValueError("invalid VoiceDesign stop receipt identity")
        self.release.require_release()
        if self.termination is not None and not self.termination.is_complete:
            raise ReleaseEvidenceError("termination_unconfirmed")

    @classmethod
    def from_instance_termination(
        cls, *, submit_intent_id: str, evidence: InstanceTerminationEvidence | None
    ) -> VoiceDesignStopReceipt:
        """Wrap one whole-instance termination the node controller reported.

        Nothing weaker becomes a receipt: a missing fact, a partial confirmation or a
        producer that answered with anything but a whole-instance termination raises
        instead of releasing a voice-design reservation.
        """
        if evidence is None or not evidence.is_complete:
            raise ReleaseEvidenceError("termination_unconfirmed")
        if evidence.evidence_kind not in {
            StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
            StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED,
        }:
            raise ReleaseEvidenceError("termination_unconfirmed")
        return cls(
            submit_intent_id,
            VoiceDesignReleaseEvidence(
                kind=ReleaseEvidenceKind.CONTAINER_STOP,
                observed_at=evidence.observed_at,
                exited=True,
                release_confirmed=True,
            ),
            evidence,
        )

    def verified_stop(self, intent: SubmissionIntent) -> VerifiedStopEvidence | None:
        """Return the shared stop-evidence row for ``intent``, or nothing.

        The conversion is the one the recovery sweep already uses, so a voice-design
        release never introduces a second evidence identity, digest or column.
        """
        if self.termination is None:
            return None
        return whole_instance_stop_evidence(intent, self.termination)
