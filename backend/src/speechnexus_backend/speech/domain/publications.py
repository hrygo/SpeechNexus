"""Append-only publication identities and the catalog revision they define.

A publication is the only fact that makes a prepared profile, binding or voice
revision visible to the public API. Draft revisions and default snapshots stay
private until an explicit publication record exists for that exact revision.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

import rfc8785

PublicationKind = Literal["profile", "binding", "voice"]

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_MAX_TIMESTAMP_US = 253_402_300_799_999_999


def publication_digest(value: Any) -> str:
    """Canonical digest with an explicit failure instead of a lenient fallback."""
    try:
        return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid publication document") from None


@dataclass(frozen=True, slots=True)
class Publication:
    """One immutable published revision; republication cannot rewrite it."""

    kind: PublicationKind
    object_id: str
    revision: int
    revision_id: str
    published_at_us: int

    def __post_init__(self) -> None:
        if self.kind not in ("profile", "binding", "voice"):
            raise ValueError("Unknown publication kind")
        for value in (self.object_id, self.revision_id):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError("Invalid publication identity")
        if type(self.revision) is not int or not 1 <= self.revision <= 9_999_999_999_999_999_999:
            raise ValueError("Invalid publication revision")
        if (
            type(self.published_at_us) is not int
            or not 0 <= self.published_at_us <= _MAX_TIMESTAMP_US
        ):
            raise ValueError("Invalid publication timestamp")

    @property
    def reference(self) -> dict[str, object]:
        return {"id": self.object_id, "revision": self.revision}


def catalog_revision(publications: Iterable[Publication]) -> str:
    """One stable revision over the whole published set, independent of order.

    Duplicate publications for the same triple collapse, so re-reading storage
    whose rows arrive in a different order cannot change the public revision.
    """
    ordered = sorted(
        {(item.kind, item.object_id, item.revision, item.revision_id) for item in publications}
    )
    return publication_digest([list(entry) for entry in ordered])
