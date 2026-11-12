"""Recorded model binding audit events and their bounded keyset page; no storage.

The audit table stores only ``kind=binding`` lifecycle events, so every row this
domain accepts names a model binding as its resource. A row carries the event
identity, the recorded actor facts, the exact action, the revision the change
touched, when it happened, the catalog policy revision in force, and the summary
of changed fields. Commands, idempotency keys, intent digests and evidence
identities stay outside this projection because the list contract does not
declare them.

Ordering is the stored ``(occurred_at_us DESC, event_id DESC)`` pair. ``event_id``
is unique, so the pair is a total order and a page boundary is a position in that
key frontier, never an offset into a sort over mutable columns. A page is only
accepted when every row is strictly older than the cursor frontier and, inside
the page, strictly older than its predecessor; a page that is not strictly
descending means the ordering was not actually applied and is refused instead of
being silently re-sorted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import counter, identifier

type AuditActorType = Literal["anonymous", "shared_credential"]

AUDIT_ACTOR_TYPES: tuple[AuditActorType, ...] = ("anonymous", "shared_credential")
# The single resource family the append-only table records; the storage layer
# constrains ``kind`` to ``binding``.
AUDIT_RESOURCE_TYPE = "model_binding"
MAX_AUDIT_LIMIT = 100
MAX_OCCURRED_AT_US = 253_402_300_799_999_999
MAX_POLICY_REVISION = 9_999_999_999_999_999_999
MAX_REQUEST_ID = 128
MAX_ACTION = 128
MAX_CREDENTIAL_LABEL = 128
MAX_CHANGE_FIELDS = 128
MAX_FIELD_POINTER = 256
MAX_REASON = 2000
# Mirrors the contract's RFC 6901 pointer pattern so a stored payload cannot
# become a response field name.
_FIELD_POINTER = re.compile(r"/(?:[^/~]|~0|~1)+(?:/(?:[^/~]|~0|~1)+)*\Z")


class AuditEventUnavailableError(Exception):
    """Authoritative audit events could not be read or trusted."""


@dataclass(frozen=True, slots=True)
class AuditChangeSummary:
    """Changed field pointers and an optional reason; never before/after values."""

    changed_fields: tuple[str, ...]
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.changed_fields, tuple) or not (
            1 <= len(self.changed_fields) <= MAX_CHANGE_FIELDS
        ):
            raise ValueError("Invalid audit change summary")
        if len(set(self.changed_fields)) != len(self.changed_fields):
            raise ValueError("Audit changed fields must be unique")
        for field in self.changed_fields:
            if (
                not isinstance(field, str)
                or not 2 <= len(field) <= MAX_FIELD_POINTER
                or not _FIELD_POINTER.fullmatch(field)
            ):
                raise ValueError("Invalid audit changed field pointer")
        if self.reason is not None and (
            not isinstance(self.reason, str) or len(self.reason) > MAX_REASON
        ):
            raise ValueError("Invalid audit change reason")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One recorded event; every field came from a named authoritative column."""

    event_id: str
    actor_type: AuditActorType
    credential_label: str | None
    request_id: str
    action: str
    resource_type: str
    resource_id: str
    occurred_at_us: int
    policy_revision: int
    change_summary: AuditChangeSummary | None

    def __post_init__(self) -> None:
        identifier(self.event_id)
        if not isinstance(self.actor_type, str) or self.actor_type not in AUDIT_ACTOR_TYPES:
            raise ValueError("Invalid audit actor type")
        if self.credential_label is not None and (
            not isinstance(self.credential_label, str)
            or len(self.credential_label) > MAX_CREDENTIAL_LABEL
        ):
            raise ValueError("Invalid audit credential label")
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= MAX_REQUEST_ID:
            raise ValueError("Invalid audit request identity")
        if not isinstance(self.action, str) or not 1 <= len(self.action) <= MAX_ACTION:
            raise ValueError("Invalid audit action")
        if self.resource_type != AUDIT_RESOURCE_TYPE:
            raise ValueError("Invalid audit resource type")
        identifier(self.resource_id)
        counter(self.occurred_at_us, maximum=MAX_OCCURRED_AT_US)
        if (
            isinstance(self.policy_revision, bool)
            or not isinstance(self.policy_revision, int)
            or not 1 <= self.policy_revision <= MAX_POLICY_REVISION
        ):
            raise ValueError("Invalid audit policy revision")
        if self.change_summary is not None and not isinstance(
            self.change_summary, AuditChangeSummary
        ):
            raise ValueError("Invalid audit change summary")


@dataclass(frozen=True, slots=True)
class AuditEventPosition:
    """The composite keyset position a page resumes strictly after."""

    occurred_at_us: int
    event_id: str

    def __post_init__(self) -> None:
        counter(self.occurred_at_us, maximum=MAX_OCCURRED_AT_US)
        identifier(self.event_id)


def position(event: AuditEvent) -> AuditEventPosition:
    return AuditEventPosition(event.occurred_at_us, event.event_id)


@dataclass(frozen=True, slots=True)
class AuditEventQuery:
    """One bounded page request; the action filter is part of the cursor scope."""

    limit: int
    after: AuditEventPosition | None = None
    action: str | None = None

    def __post_init__(self) -> None:
        counter(self.limit, minimum=1, maximum=MAX_AUDIT_LIMIT)
        if self.after is not None and not isinstance(self.after, AuditEventPosition):
            raise ValueError("Invalid audit cursor position")
        if self.action is not None and (
            not isinstance(self.action, str) or len(self.action) > MAX_ACTION
        ):
            raise ValueError("Invalid audit action filter")


@dataclass(frozen=True, slots=True)
class AuditEventPage:
    items: tuple[AuditEvent, ...]
    next_position: AuditEventPosition | None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, AuditEvent) for item in self.items
        ):
            raise ValueError("Invalid audit event page")
        if self.next_position is not None and (
            not self.items or self.next_position != position(self.items[-1])
        ):
            raise ValueError("An audit page cursor must name the last emitted row")
