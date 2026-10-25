"""Server-private opaque keyset tokens; binding is query consistency, not identity."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from speechnexus_backend.workbench.application.listings import (
    ListingKind,
    ListingPosition,
    validate_listing,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class InvalidCursorError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid pagination cursor")


@dataclass(frozen=True, slots=True)
class CursorCodec:
    deployment_id: str
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        try:
            valid = (
                type(self.deployment_id) is str
                and 1 <= len(self.deployment_id) <= 128
                and all(ord(c) >= 32 for c in self.deployment_id)
                and type(self.key) is bytes
                and 32 <= len(self.key) <= 4096
            )
            if not valid:
                raise ValueError
            self.deployment_id.encode("utf-8")
        except ValueError, UnicodeError:
            raise ValueError("invalid cursor configuration") from None

    def _context(self, kind: ListingKind, parent_id: str) -> bytes:
        return (
            json.dumps(
                [self.deployment_id, kind, parent_id], ensure_ascii=True, separators=(",", ":")
            ).encode("ascii")
            + b"\0"
        )

    def encode(self, position: ListingPosition) -> str:
        validate_listing(position.kind, position.parent_id, 1, position)
        if position.kind not in {
            "projects",
            "transcripts",
            "books",
            "chapters",
            "render_units",
        }:
            raise ValueError("invalid cursor position")
        identity = position.item_id.encode("ascii")
        elapsed = position.created_at.astimezone(UTC) - EPOCH
        micros = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
        payload = b"\x01" + struct.pack(">q", micros) + identity
        signature = hmac.digest(
            self.key, self._context(position.kind, position.parent_id) + payload, hashlib.sha256
        )
        return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")

    def decode(self, token: str, *, kind: ListingKind, parent_id: str) -> ListingPosition:
        try:
            if type(token) is not str or not 1 <= len(token) <= 512:
                raise ValueError
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            if canonical != token or not 42 <= len(raw) <= 169:
                raise ValueError
            payload, signature = raw[:-32], raw[-32:]
            expected = hmac.digest(
                self.key, self._context(kind, parent_id) + payload, hashlib.sha256
            )
            if payload[0] != 1 or not hmac.compare_digest(signature, expected):
                raise ValueError
            identity = payload[9:].decode("ascii")
            stamp = EPOCH + timedelta(microseconds=struct.unpack(">q", payload[1:9])[0])
            position = ListingPosition(kind, parent_id, stamp, identity)
            validate_listing(kind, parent_id, 1, position)
            return position
        except ValueError, UnicodeError, OverflowError, struct.error:
            raise InvalidCursorError from None
