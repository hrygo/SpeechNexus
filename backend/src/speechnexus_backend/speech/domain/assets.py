"""Asset identities, publication facts and retention rules; no storage SDKs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Literal

type AssetPurpose = Literal["asr_input", "voice_reference", "text_input"]
type AssetStatus = Literal["staging", "ready", "rejected", "expired", "deleted"]
type UploadPhase = Literal["reserved", "writing", "settled"]
MAX_SAFE = 2**53 - 1
MAX_TTL = 31536000
MAX_ASSET_BYTES = 4 * 1024 * 1024
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid asset identity")
    return value


def digest(value: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError("Invalid asset digest")
    return value


def counter(value: int, *, minimum: int = 0, maximum: int = MAX_SAFE) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid asset counter")
    return value


def hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class AssetPolicy:
    default_ttl_seconds: int | None
    max_bytes: int = MAX_ASSET_BYTES
    max_text_characters: int = 1000000

    def __post_init__(self) -> None:
        if self.default_ttl_seconds is not None:
            counter(self.default_ttl_seconds, minimum=60, maximum=MAX_TTL)
        # This publication unit deliberately fits a single S3 PUT. Supporting
        # multipart uploads also requires durable per-part cleanup identities.
        counter(self.max_bytes, minimum=1, maximum=MAX_ASSET_BYTES)
        counter(self.max_text_characters, minimum=1, maximum=1000000)


@dataclass(frozen=True, slots=True)
class UploadIntent:
    workspace_id: str
    purpose: AssetPurpose
    bytes: int
    sha256: str
    key_digest: str = field(repr=False)
    requested_ttl_seconds: int | None = None
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        identifier(self.workspace_id)
        if self.purpose not in ("asr_input", "voice_reference", "text_input"):
            raise ValueError("Invalid asset purpose")
        counter(self.bytes)
        digest(self.sha256)
        digest(self.key_digest)
        if self.requested_ttl_seconds is not None:
            counter(self.requested_ttl_seconds, minimum=60, maximum=MAX_TTL)
        if self.expected_sha256 is not None:
            digest(self.expected_sha256)

    @property
    def request_digest(self) -> str:
        # This subset is RFC 8785: fixed ASCII keys/values, safe integers and null.
        # Omitted TTL stays null; replay never replaces it with a changed policy.
        document = {
            "workspace_id": self.workspace_id,
            "purpose": self.purpose,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "requested_ttl_seconds": self.requested_ttl_seconds,
            "expected_sha256": self.expected_sha256,
        }
        return hash_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())

    def content_rejection(self) -> str | None:
        if self.bytes == 0:
            return "asset_empty"
        if self.expected_sha256 is not None and self.expected_sha256 != self.sha256:
            return "asset_digest_mismatch"
        return None


@dataclass(frozen=True, slots=True)
class MediaFacts:
    media_type: str
    duration_ms: int | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None

    def require_for(self, purpose: AssetPurpose) -> None:
        if not isinstance(self.media_type, str) or not 1 <= len(self.media_type) <= 128:
            raise ValueError("Invalid asset media type")
        if purpose == "text_input":
            if any(
                value is not None
                for value in (self.duration_ms, self.sample_rate_hz, self.channels)
            ):
                raise ValueError("Text assets cannot carry audio facts")
        else:
            if self.duration_ms is None or self.sample_rate_hz is None or self.channels is None:
                raise ValueError("Ready audio requires actual media facts")
            counter(self.duration_ms, minimum=1)
            counter(self.sample_rate_hz, minimum=8000, maximum=192000)
            counter(self.channels, minimum=1, maximum=8)


@dataclass(frozen=True, slots=True)
class Inspection:
    facts: MediaFacts | None = None
    error_code: str | None = None
    available: bool = True

    def __post_init__(self) -> None:
        if self.available:
            if (self.facts is None) == (self.error_code is None):
                raise ValueError("Inspection needs either validated facts or a rejection")
        elif self.facts is not None or self.error_code is not None:
            raise ValueError("Unavailable inspection cannot invent a content result")


@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_id: str
    workspace_id: str
    purpose: AssetPurpose
    status: AssetStatus
    bytes: int
    sha256: str
    media: MediaFacts
    created_at_us: int
    expires_at_us: int
    version: int
    phase: UploadPhase
    object_key: str = field(repr=False)
    request_digest: str = field(repr=False)
    writer_id: str | None = field(default=None, repr=False)
    error_code: str | None = None

    def __post_init__(self) -> None:
        identifier(self.asset_id)
        identifier(self.workspace_id)
        digest(self.sha256)
        digest(self.request_digest)
        counter(self.bytes)
        counter(self.created_at_us)
        counter(self.expires_at_us)
        if self.expires_at_us <= self.created_at_us:
            raise ValueError("Asset expiry must follow its creation")
        counter(self.version, minimum=1, maximum=2**63 - 1)
        if self.purpose not in ("asr_input", "voice_reference", "text_input"):
            raise ValueError("Invalid asset purpose")
        if self.status not in ("staging", "ready", "rejected", "expired", "deleted"):
            raise ValueError("Invalid asset state")
        if self.phase not in ("reserved", "writing", "settled"):
            raise ValueError("Invalid upload phase")
        if not re.fullmatch(
            rf"private/assets/{re.escape(self.asset_id)}/[0-9a-f]{{32}}", self.object_key
        ):
            raise ValueError("Asset key must be server-generated and scoped")
        if self.writer_id is not None:
            identifier(self.writer_id)
        if self.phase == "writing" and self.writer_id is None:
            raise ValueError("An external write needs its original writer identity")
        if self.status == "ready":
            if (
                self.bytes < 1
                or self.phase != "settled"
                or self.writer_id is None
                or self.error_code is not None
            ):
                raise ValueError("Ready asset requires a settled nonempty publication")
            self.media.require_for(self.purpose)

    def content_available(self, now_us: int) -> bool:
        return self.status == "ready" and self.expires_at_us > counter(now_us)


@dataclass(frozen=True, slots=True)
class VerifiedObject:
    asset_id: str
    object_key: str = field(repr=False)
    version_id: str = field(repr=False)
    bytes: int
    sha256: str
    writer_id: str

    def __post_init__(self) -> None:
        identifier(self.asset_id)
        identifier(self.writer_id)
        digest(self.sha256)
        counter(self.bytes, minimum=1)
        if (
            not isinstance(self.version_id, str)
            or not self.version_id
            or self.version_id == "null"
            or len(self.version_id) > 1024
        ):
            raise ValueError("Asset object needs an immutable storage version")

    def require_original(self, asset: AssetRecord) -> None:
        if (self.asset_id, self.object_key, self.bytes, self.sha256, self.writer_id) != (
            asset.asset_id,
            asset.object_key,
            asset.bytes,
            asset.sha256,
            asset.writer_id,
        ):
            raise ValueError("Object proof must match the original upload")


@dataclass(frozen=True, slots=True)
class UploadResult:
    outcome: Literal["created", "replayed", "rejected", "conflict", "unknown", "in_progress"]
    asset: AssetRecord | None = None
    error_code: str | None = None


class AssetError(Exception):
    """Safe application error; never carries content, credentials or SDK diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def reserve_asset(
    intent: UploadIntent,
    inspection: Inspection,
    policy: AssetPolicy,
    *,
    asset_id: str,
    object_key: str,
    now_us: int,
) -> AssetRecord:
    ttl = intent.requested_ttl_seconds or policy.default_ttl_seconds
    if ttl is None:
        raise AssetError("asset_retention_not_configured")
    if not inspection.available:
        raise AssetError("asset_inspection_unavailable")
    facts = inspection.facts or MediaFacts("application/octet-stream")
    if inspection.facts is not None:
        facts.require_for(intent.purpose)
    return AssetRecord(
        asset_id,
        intent.workspace_id,
        intent.purpose,
        "rejected" if inspection.error_code else "staging",
        intent.bytes,
        intent.sha256,
        facts,
        now_us,
        now_us + ttl * 1000000,
        1,
        "settled" if inspection.error_code else "reserved",
        object_key,
        intent.request_digest,
        error_code=inspection.error_code,
    )


def begin_asset_write(asset: AssetRecord, writer_id: str, now_us: int) -> AssetRecord:
    if asset.expires_at_us <= now_us or asset.status != "staging":
        raise AssetError("asset_unavailable")
    if asset.phase != "reserved":
        raise AssetError("operation_in_progress")
    return replace(asset, phase="writing", writer_id=writer_id, version=asset.version + 1)


def publish_asset(
    asset: AssetRecord, proof: VerifiedObject, *, now_us: int, workspace_active: bool
) -> AssetRecord:
    proof.require_original(asset)
    if asset.phase not in ("writing", "settled") or asset.status in ("deleted", "rejected"):
        raise AssetError("asset_publication_conflict")
    status = asset.status
    if asset.expires_at_us <= now_us or status == "expired":
        status = "expired"
    elif workspace_active:
        status = "ready"
    if status == asset.status and asset.phase == "settled":
        return asset
    return replace(asset, status=status, phase="settled", version=asset.version + 1)


def expiry_blocker(asset: AssetRecord, *, has_references: bool) -> str | None:
    if has_references:
        return "asset_referenced"
    if asset.phase == "writing":
        return "asset_upload_unknown"
    return None


def byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    counter(size, minimum=1)
    if value is None:
        return 0, size, False
    if len(value) > 128 or not (match := re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)):
        raise AssetError("range_not_satisfiable")
    first, last = match.groups()
    if not first:
        suffix = int(last) if last else 0
        if suffix < 1:
            raise AssetError("range_not_satisfiable")
        start, end = max(0, size - suffix), size - 1
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
        if start >= size or start > end:
            raise AssetError("range_not_satisfiable")
    return start, end - start + 1, True
