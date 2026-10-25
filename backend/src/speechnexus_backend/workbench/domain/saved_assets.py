"""Workbench-owned copies of committed speech artifacts; no HTTP, object or database state.

A saved asset pins the source Artifact identity, its public receipt and the digest of the
bytes the workbench copied. The copy is independent of the base's retention window, so the
project references this committed copy instead of treating the source TTL as permanent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

MAX_DISPLAY_NAME_LENGTH = 128
MAX_MEDIA_TYPE_LENGTH = 128
MAX_OBJECT_KEY_LENGTH = 256

# Mirrors the frozen public Identifier/Sha256Digest wire shapes at the domain boundary; the
# generated contract still owns transport validation, and never feeds these values back.
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _identifier(value: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid saved asset identifier")


def _text(value: str, maximum: int) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or "\x00" in value
    ):
        raise ValueError("invalid saved asset text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("unsupported saved asset character") from None


def saved_object_key(project_id: str, sha256: str) -> str:
    """Content-addressed inside the project prefix, so a replay never orphans an object."""
    _identifier(project_id)
    if type(sha256) is not str or _DIGEST.fullmatch(sha256) is None:
        raise ValueError("invalid saved asset digest")
    return f"{project_id}/{sha256[7:]}"


def project_object_prefix(project_id: str) -> str:
    """One prefix per project: project deletion can always find its remaining objects."""
    _identifier(project_id)
    return f"{project_id}/"


def validate_saved_asset_request(
    *, source_artifact_id: str, source_receipt_id: str, display_name: str | None
) -> None:
    """Reject an unsupported request before any download, object write or transaction."""
    _identifier(source_artifact_id)
    _identifier(source_receipt_id)
    if display_name is not None:
        _text(display_name, MAX_DISPLAY_NAME_LENGTH)


@dataclass(frozen=True, slots=True)
class SavedAssetCopy:
    """Verified copy facts; the object zone's own addressing stays outside the domain."""

    source_artifact_id: str
    source_receipt_id: str
    media_type: str
    bytes: int
    sha256: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        validate_saved_asset_request(
            source_artifact_id=self.source_artifact_id,
            source_receipt_id=self.source_receipt_id,
            display_name=self.display_name,
        )
        _text(self.media_type, MAX_MEDIA_TYPE_LENGTH)
        if type(self.bytes) is not int or not 1 <= self.bytes <= 2**53 - 1:
            raise ValueError("saved asset byte count must be a positive safe integer")
        if type(self.sha256) is not str or _DIGEST.fullmatch(self.sha256) is None:
            raise ValueError("invalid saved asset digest")


@dataclass(frozen=True, slots=True)
class SavedAsset:
    """A committed copy; the timestamp comes from the committing transaction."""

    id: str
    project_id: str
    object_key: str
    copy: SavedAssetCopy
    created_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.id)
        _identifier(self.project_id)
        _text(self.object_key, MAX_OBJECT_KEY_LENGTH)
        if type(self.copy) is not SavedAssetCopy:
            raise ValueError("saved asset requires verified copy facts")
        if not isinstance(self.created_at, datetime):
            raise ValueError("saved asset requires a transaction timestamp")
        expected = saved_object_key(self.project_id, self.copy.sha256)
        if self.object_key != expected:
            raise ValueError("saved asset object key must match its project and digest")
