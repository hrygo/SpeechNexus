"""What may prove a VoiceDesign instance released its GPU memory.

A container exit or a terminated engine process is release evidence. The engine
sleep endpoint is not: on the verified vLLM-Omni image ``POST /v1/omni/sleep``
answered ``SUCCESS`` with ``freed_bytes=0`` and the next inference timed out
after 300 s, so the acknowledgment neither released memory nor kept the endpoint
usable. Callers must stop the container or terminate the process and keep the
capacity until that stop is verified elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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
    """A stop receipt that may release only from verified termination evidence."""

    submit_intent_id: str
    release: VoiceDesignReleaseEvidence

    def __post_init__(self) -> None:
        if type(self.submit_intent_id) is not str or not 1 <= len(self.submit_intent_id) <= 128:
            raise ValueError("invalid VoiceDesign stop receipt identity")
        self.release.require_release()
