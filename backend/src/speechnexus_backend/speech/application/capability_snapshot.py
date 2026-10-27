"""Stable public capability snapshots and HTTP validators.

Only published facts and explicit deployment policy enter this projection.  The
snapshot identifier and ETag contain no request time, so repeated reads over the
same revisions are byte-stable and a matching ``If-None-Match`` can skip the body.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import rfc8785

from speechnexus_backend.speech.application.effective_configuration import revision_text
from speechnexus_backend.speech.domain.assets import identifier

_MAX_TIMESTAMP_US = 253_402_300_799_999_999
_MAX_REVISION = 9_999_999_999_999_999_999


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """The explicit deployment policy revision and bounded request ceilings."""

    deployment_id: str
    workspace_id: str
    policy_revision: int
    max_audio_bytes: int
    max_text_codepoints: int
    max_segment_ms: int
    max_realtime_session_ms: int
    max_queue_wait_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_id, str) or not 1 <= len(self.deployment_id) <= 256:
            raise ValueError("Capability policy requires a deployment identity")
        identifier(self.workspace_id)
        revision_text(self.policy_revision)
        _integer(self.max_audio_bytes, minimum=1)
        _integer(self.max_text_codepoints, minimum=1, maximum=1_000_000)
        _integer(self.max_segment_ms, minimum=1000, maximum=30_000)
        _integer(self.max_realtime_session_ms, minimum=60_000)
        _integer(self.max_queue_wait_ms, minimum=0)

    @property
    def limits(self) -> dict[str, int]:
        return {
            "max_audio_bytes": self.max_audio_bytes,
            "max_text_codepoints": self.max_text_codepoints,
            "max_segment_ms": self.max_segment_ms,
            "max_realtime_session_ms": self.max_realtime_session_ms,
            "max_queue_wait_ms": self.max_queue_wait_ms,
        }


def _integer(value: int, *, minimum: int, maximum: int = _MAX_REVISION) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid capability limit")
    return value


def _timestamp_text(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_TIMESTAMP_US:
        raise ValueError("Invalid capability generation time")
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: Any) -> str:
    try:
        canonical = rfc8785.dumps(value)
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Capability snapshot is not canonical JSON") from None
    return hashlib.sha256(canonical).hexdigest()


def _payload_etag(
    *,
    kind: str,
    deployment_id: str,
    workspace_id: str,
    catalog_revision: int,
    policy_revision: int,
    payload: Mapping[str, Any],
) -> str:
    digest = _digest(
        {
            "kind": kind,
            "deployment_id": deployment_id,
            "workspace_id": workspace_id,
            "catalog_revision": revision_text(catalog_revision),
            "policy_revision": revision_text(policy_revision),
            "payload": payload,
        }
    )
    return f'"sha256:{digest}"'


def catalogue_etag(
    *,
    kind: str,
    policy: CapabilityPolicy,
    workspace_id: str,
    catalog_revision: int,
    payload: Mapping[str, Any],
) -> str:
    """A validator over the stable response facts, never a request timestamp."""
    identifier(workspace_id)
    return _payload_etag(
        kind=kind,
        deployment_id=policy.deployment_id,
        workspace_id=workspace_id,
        catalog_revision=catalog_revision,
        policy_revision=policy.policy_revision,
        payload=payload,
    )


def capability_snapshot(
    *,
    models: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
    voices: Sequence[Mapping[str, Any]],
    catalog_revision: int,
    generated_at_us: int,
    policy: CapabilityPolicy,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Build one contract-shaped snapshot from already-published projections."""
    selected_workspace = workspace_id or policy.workspace_id
    identifier(selected_workspace)
    revision_text(catalog_revision)
    stable = {
        "catalog_revision": revision_text(catalog_revision),
        "policy_revision": revision_text(policy.policy_revision),
        "generated_at": _timestamp_text(generated_at_us),
        "workspace_id": selected_workspace,
        "models": [copy.deepcopy(dict(model)) for model in models],
        "profiles": [copy.deepcopy(dict(profile)) for profile in profiles],
        "voices": [copy.deepcopy(dict(voice)) for voice in voices],
        "limits": policy.limits,
    }
    snapshot_id = "cap-" + _digest(stable)[:32]
    return {"snapshot_id": snapshot_id, **stable}


def snapshot_etag(
    *,
    policy: CapabilityPolicy,
    workspace_id: str,
    catalog_revision: int,
    snapshot: Mapping[str, Any],
) -> str:
    stable = {key: value for key, value in snapshot.items() if key not in {"snapshot_id"}}
    return catalogue_etag(
        kind="capabilities",
        policy=policy,
        workspace_id=workspace_id,
        catalog_revision=catalog_revision,
        payload=stable,
    )


def matches_if_none_match(header: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison for a GET/HEAD validator."""
    if header is None:
        return False
    target = etag.removeprefix("W/").strip()
    for candidate in header.split(","):
        normalized = candidate.strip()
        if normalized == "*":
            return True
        if normalized.removeprefix("W/").strip() == target:
            return True
    return False
