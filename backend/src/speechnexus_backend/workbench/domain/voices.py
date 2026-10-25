"""Published voice-catalogue values read from the speech base's public protocol.

The workbench does not own voice identity, revision numbering, validation or publication:
those belong to the speech base, and the workbench never renumbers a revision, re-derives a
status or promotes an unpublished draft. These values carry the base's published facts into
the workbench verbatim so a workspace can pin a real revision instead of trusting an
unvalidated identifier from a client. No HTTP, database, object or engine state is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

MAX_DISPLAY_NAME_LENGTH = 128
MAX_LANGUAGE_LENGTH = 35
MAX_AUDIO_FORMAT_LENGTH = 64
MAX_CURSOR_LENGTH = 512
MAX_LIST_LIMIT = 100

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_AUDIO_FORMAT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")
# The shared wire shape: a monotonic decimal revision carried as a string.
_REVISION = re.compile(r"^[1-9][0-9]{0,18}$")
# The base's frozen public enums, carried through unchanged and never widened here.
STATUSES = frozenset({"published", "retired", "disabled"})
SOURCE_TYPES = frozenset({"designed", "cloned", "builtin"})


def _identifier(value: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid voice identifier")


def _revision(value: str) -> None:
    if type(value) is not str or _REVISION.fullmatch(value) is None:
        raise ValueError("invalid voice revision")


def _text(value: str, maximum: int) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or "\x00" in value
    ):
        raise ValueError("invalid voice text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("unsupported voice character") from None


def _languages(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or not values or len(set(values)) != len(values):
        raise ValueError("invalid voice languages")
    for value in values:
        if type(value) is not str or _LANGUAGE.fullmatch(value) is None:
            raise ValueError("invalid voice language")
        if len(value) > MAX_LANGUAGE_LENGTH:
            raise ValueError("invalid voice language")


def _formats(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or not values or len(set(values)) != len(values):
        raise ValueError("invalid voice audio formats")
    for value in values:
        if type(value) is not str or _AUDIO_FORMAT.fullmatch(value) is None:
            raise ValueError("invalid voice audio format")
        if len(value) > MAX_AUDIO_FORMAT_LENGTH:
            raise ValueError("invalid voice audio format")


def _status(value: str) -> None:
    if type(value) is not str or value not in STATUSES:
        raise ValueError("invalid voice revision status")


def _moment(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("voice timestamp must be timezone aware")


def validate_catalogue_scope(
    *, workspace_id: str, language: str | None, limit: int, after: str | None
) -> None:
    """Reject an unsupported catalogue query before any call leaves the workbench."""
    _identifier(workspace_id)
    if language is not None:
        if type(language) is not str or _LANGUAGE.fullmatch(language) is None:
            raise ValueError("invalid voice language")
        if len(language) > MAX_LANGUAGE_LENGTH:
            raise ValueError("invalid voice language")
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError("invalid voice listing limit")
    if after is not None:
        _text(after, MAX_CURSOR_LENGTH)


def validate_voice_reference(*, voice_id: str, revision: str) -> None:
    """A pinned voice revision is an identity plus the base's own revision number."""
    _identifier(voice_id)
    _revision(revision)


@dataclass(frozen=True, slots=True)
class VoiceSummary:
    """One published revision as the base summarises it for a deployment listing."""

    voice_id: str
    revision: str
    display_name: str
    source_type: str
    status: str
    languages: tuple[str, ...]
    audio_formats: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.voice_id)
        _revision(self.revision)
        _text(self.display_name, MAX_DISPLAY_NAME_LENGTH)
        if type(self.source_type) is not str or self.source_type not in SOURCE_TYPES:
            raise ValueError("invalid voice source type")
        _status(self.status)
        _languages(self.languages)
        _formats(self.audio_formats)


@dataclass(frozen=True, slots=True)
class VoiceRevision:
    """One immutable published revision with the binding the base pinned for it."""

    voice_id: str
    revision: str
    display_name: str
    status: str
    languages: tuple[str, ...]
    audio_formats: tuple[str, ...]
    model_binding_id: str
    model_binding_revision: str
    published_at: datetime
    retired_at: datetime | None = None
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.voice_id)
        _revision(self.revision)
        _text(self.display_name, MAX_DISPLAY_NAME_LENGTH)
        _status(self.status)
        _languages(self.languages)
        _formats(self.audio_formats)
        _identifier(self.model_binding_id)
        _revision(self.model_binding_revision)
        _moment(self.published_at)
        if self.retired_at is not None:
            _moment(self.retired_at)
        if self.disabled_reason is not None:
            _text(self.disabled_reason, MAX_DISPLAY_NAME_LENGTH)
        if self.status == "retired" and self.retired_at is None:
            raise ValueError("a retired voice revision carries its retirement moment")
        if self.status != "retired" and self.retired_at is not None:
            raise ValueError("only a retired voice revision carries a retirement moment")
        if self.status == "disabled" and self.disabled_reason is None:
            raise ValueError("a disabled voice revision carries its reason")
        if self.status != "disabled" and self.disabled_reason is not None:
            raise ValueError("only a disabled voice revision carries a reason")


@dataclass(frozen=True, slots=True)
class VoiceCataloguePage:
    """One page of the base's listing; the cursor is the base's own opaque value."""

    items: tuple[VoiceSummary, ...]
    has_more: bool
    next_cursor: str | None

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            type(item) is not VoiceSummary for item in self.items
        ):
            raise ValueError("invalid voice catalogue items")
        if type(self.has_more) is not bool:
            raise ValueError("invalid voice catalogue paging flag")
        if self.next_cursor is not None:
            _text(self.next_cursor, MAX_CURSOR_LENGTH)
        if self.has_more and self.next_cursor is None:
            raise ValueError("a continued voice listing carries its next cursor")
