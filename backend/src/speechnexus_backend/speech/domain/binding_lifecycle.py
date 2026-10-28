"""Explicit binding revision selection and retirement commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import document_digest, revision_number

type BindingLifecycleMode = Literal["retire", "disable"]
type BindingLifecycleStatus = Literal["published", "retired", "disabled"]


class BindingLifecycleUnavailableError(Exception):
    """The authoritative lifecycle result cannot be established."""


class BindingLifecycleNotFoundError(LookupError):
    """The named revision was never published."""


class BindingLifecycleConflictError(ValueError):
    """A lifecycle command conflicts with the current authoritative state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(value: str | None) -> None:
    if value is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid lifecycle digest")


@dataclass(frozen=True, slots=True)
class _LifecycleCommand:
    binding_id: str
    revision: str
    request_id: str
    actor_type: Literal["anonymous", "shared_credential"]
    credential_label: str | None
    key_digest: str | None
    intent_digest: str

    def __post_init__(self) -> None:
        identifier(self.binding_id)
        revision_number(self.revision)
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128:
            raise ValueError("Invalid lifecycle request identity")
        if self.actor_type not in ("anonymous", "shared_credential"):
            raise ValueError("Invalid lifecycle actor")
        if self.credential_label is not None and (
            not isinstance(self.credential_label, str) or not 1 <= len(self.credential_label) <= 128
        ):
            raise ValueError("Invalid lifecycle credential label")
        _digest(self.key_digest)
        _digest(self.intent_digest)


@dataclass(frozen=True, slots=True)
class BindingRetirementCommand(_LifecycleCommand):
    mode: BindingLifecycleMode = "retire"
    reason: str | None = None

    def __post_init__(self) -> None:
        _LifecycleCommand.__post_init__(self)
        if self.mode not in ("retire", "disable"):
            raise ValueError("Invalid retirement mode")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 2000
        ):
            raise ValueError("Invalid retirement reason")
        if self.mode == "disable" and self.reason is None:
            raise ValueError("Disable requires a reason")


@dataclass(frozen=True, slots=True)
class BindingActivationCommand(_LifecycleCommand):
    expected_current_revision: str | None = None

    def __post_init__(self) -> None:
        _LifecycleCommand.__post_init__(self)
        if self.expected_current_revision is not None:
            revision_number(self.expected_current_revision)


@dataclass(frozen=True, slots=True)
class BindingLifecycleReceipt:
    binding_id: str
    revision: str
    revision_id: str
    definition: bytes
    created_at_us: int
    published_at_us: int
    capacity_manifest_id: str
    qualification_report_id: str
    status: BindingLifecycleStatus
    retired_at_us: int | None
    disabled_reason: str | None
    replayed: bool

    def __post_init__(self) -> None:
        identifier(self.binding_id)
        revision_number(self.revision)
        identifier(self.revision_id)
        document_digest(self.definition)
        if self.status not in ("published", "retired", "disabled"):
            raise ValueError("Invalid lifecycle result status")
        if self.status == "published" and (
            self.retired_at_us is not None or self.disabled_reason is not None
        ):
            raise ValueError("Published binding cannot carry retirement facts")
        if self.status == "retired" and (
            self.retired_at_us is None or self.disabled_reason is not None
        ):
            raise ValueError("Retired binding requires its retirement time")
        if self.status == "disabled" and (
            self.retired_at_us is None or self.disabled_reason is None
        ):
            raise ValueError("Disabled binding requires its reason")
