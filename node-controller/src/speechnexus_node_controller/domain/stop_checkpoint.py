"""Private, monotonic stop checkpoints; neither a budget nor a new stop command."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from speechnexus_node_controller.domain.intents import (
    IntentConflictError,
    InvalidRuntimeRequestError,
    RuntimeActionKind,
    RuntimeInstanceIntentRequest,
    RuntimeStopEvidence,
)
from speechnexus_node_controller.domain.payloads import parse_request, request_payload


@dataclass(frozen=True, slots=True)
class StopCheckpoint:
    """Pin the original target, then retain exit and release confirmations in order."""

    request: RuntimeInstanceIntentRequest
    container_id: str
    exit_code: int | None = None
    exited_at: datetime | None = None
    released_at: datetime | None = None
    # When the runtime accepted our removal request for the pinned target. It is
    # the only durable proof that the disappearance we later observe is ours: a
    # replay after a restart may not attribute someone else's removal to this
    # stop, and a Pod that vanished before we asked stays unresolved.
    deletion_requested_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.request.action is not RuntimeActionKind.STOP:
            raise InvalidRuntimeRequestError("a stop checkpoint requires a stop intent")
        if not isinstance(self.container_id, str) or not 1 <= len(self.container_id) <= 128:
            raise InvalidRuntimeRequestError("a stop checkpoint requires a bounded container ID")
        # An exit code is only ever recorded together with the exit fact, but the
        # exit fact may stand alone: a cluster runtime can watch its Pod leave the
        # API without ever seeing a terminated exit code. The stop evidence then
        # carries ``exit_code: null``, which the instance contract allows.
        if self.exit_code is not None and self.exited_at is None:
            raise InvalidRuntimeRequestError("an exit code requires an exit confirmation")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise InvalidRuntimeRequestError("a stop checkpoint requires an integer exit code")
        if self.released_at is not None and self.exited_at is None:
            raise InvalidRuntimeRequestError("release confirmation requires an earlier exit fact")
        for at in (self.exited_at, self.released_at, self.deletion_requested_at):
            if at is not None and (not isinstance(at, datetime) or at.tzinfo is None):
                raise InvalidRuntimeRequestError("checkpoint timestamps require a timezone")

    @property
    def stage(self) -> str:
        if self.released_at is not None:
            return "released"
        if self.exited_at is not None:
            return "exited"
        return "deletion_requested" if self.deletion_requested_at is not None else "targeted"

    def advance(self, candidate: StopCheckpoint) -> StopCheckpoint:
        """Merge an idempotent write without replacing the first confirmed facts."""
        if candidate.request != self.request or candidate.container_id != self.container_id:
            raise IntentConflictError("the stop checkpoint belongs to another original target")
        if candidate.exited_at is None:
            if self.deletion_requested_at is not None or candidate.deletion_requested_at is None:
                return self
            return replace(self, deletion_requested_at=candidate.deletion_requested_at)
        if self.exited_at is None:
            if candidate.released_at is not None:
                raise InvalidRuntimeRequestError("the exit checkpoint must be committed first")
            requested = self.deletion_requested_at or candidate.deletion_requested_at
            return replace(candidate, deletion_requested_at=requested)
        if candidate.exit_code != self.exit_code:
            raise IntentConflictError("the recorded exit fact cannot be replaced")
        if self.released_at is not None or candidate.released_at is None:
            return self
        return replace(self, released_at=candidate.released_at)

    def evidence(self) -> RuntimeStopEvidence | None:
        if self.released_at is None:
            return None
        return RuntimeStopEvidence(
            verified_at=self.released_at,
            exit_code=self.exit_code,
            runtime_container_id=self.container_id,
        )


def stop_checkpoint_payload(checkpoint: StopCheckpoint) -> dict[str, Any]:
    """The versioned private record; request semantics reuse the runtime contract."""
    return {
        "format_version": 2,
        "request": request_payload(checkpoint.request),
        "container_id": checkpoint.container_id,
        "exit_code": checkpoint.exit_code,
        "exited_at": checkpoint.exited_at.isoformat() if checkpoint.exited_at else None,
        "released_at": checkpoint.released_at.isoformat() if checkpoint.released_at else None,
        "deletion_requested_at": (
            checkpoint.deletion_requested_at.isoformat()
            if checkpoint.deletion_requested_at
            else None
        ),
    }


def parse_stop_checkpoint(payload: Mapping[str, Any]) -> StopCheckpoint:
    fields = {
        "format_version",
        "request",
        "container_id",
        "exit_code",
        "exited_at",
        "released_at",
    }
    fields_with_deletion = fields | {"deletion_requested_at"}
    version = payload.get("format_version")
    if type(version) is not int or not isinstance(payload["request"], Mapping):
        raise InvalidRuntimeRequestError("the stop checkpoint record shape is not supported")
    # Version 1 records predate the deletion fact; they stay readable so an
    # upgraded controller can still finish the stop a previous one pinned.
    if version == 1 and set(payload) == fields:
        deletion_requested_at = None
    elif version == 2 and set(payload) == fields_with_deletion:
        deletion_requested_at = _timestamp(payload["deletion_requested_at"])
    else:
        raise InvalidRuntimeRequestError("the stop checkpoint record shape is not supported")
    return StopCheckpoint(
        request=parse_request(payload["request"]),
        container_id=payload["container_id"],
        exit_code=payload["exit_code"],
        exited_at=_timestamp(payload["exited_at"]),
        released_at=_timestamp(payload["released_at"]),
        deletion_requested_at=deletion_requested_at,
    )


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRuntimeRequestError("the stop checkpoint timestamp is invalid")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidRuntimeRequestError("the stop checkpoint timestamp is invalid") from error
