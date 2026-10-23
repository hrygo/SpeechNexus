"""Bounded asset orchestration; object I/O never runs inside metadata transactions."""

from __future__ import annotations

import codecs
import hashlib
import uuid
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Protocol, cast

from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetPolicy,
    AssetPurpose,
    AssetRecord,
    Inspection,
    MediaFacts,
    UploadIntent,
    UploadResult,
    VerifiedObject,
    byte_range,
    hash_bytes,
)


@dataclass(frozen=True, slots=True)
class DeletionTicket:
    ledger_id: str
    asset_id: str
    state: str
    object: VerifiedObject | None


class AssetMetadata(Protocol):
    def reserve(
        self,
        intent: UploadIntent,
        *,
        asset_id: str,
        object_key: str,
        inspection: Inspection,
        policy: AssetPolicy,
    ) -> UploadResult: ...

    def begin_write(self, asset_id: str, writer_id: str) -> UploadResult: ...

    def publish(self, proof: VerifiedObject) -> UploadResult: ...

    def get(self, asset_id: str) -> tuple[AssetRecord | None, int]: ...

    def object(self, asset_id: str) -> VerifiedObject | None: ...

    def pending(self, limit: int) -> list[str]: ...

    def expired(self, limit: int) -> list[str]: ...

    def prepare_expiry(self, asset_id: str, ledger_id: str) -> DeletionTicket | None: ...

    def complete_deletion(self, ledger_id: str) -> bool: ...


class ContentReader(Protocol):
    def read(self, size: int) -> bytes: ...


class AssetObjects(Protocol):
    def check_writes_available(self) -> None: ...

    def put_verified(self, asset: AssetRecord, body: BinaryIO) -> VerifiedObject: ...

    def inspect_original(self, asset: AssetRecord) -> VerifiedObject | None: ...

    def open(
        self, proof: VerifiedObject, offset: int, length: int
    ) -> AbstractContextManager[ContentReader]: ...

    def delete_version(self, proof: VerifiedObject) -> None: ...


class AssetInspector(Protocol):
    def inspect(self, purpose: AssetPurpose, body: BinaryIO, policy: AssetPolicy) -> Inspection: ...


class TextAssetInspector:
    """Actual UTF-8 text validation. Audio inspection is an explicit B03 input."""

    def inspect(self, purpose: AssetPurpose, body: BinaryIO, policy: AssetPolicy) -> Inspection:
        if purpose != "text_input":
            return Inspection(available=False)
        body.seek(0)
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        characters = 0
        try:
            while chunk := body.read(65536):
                text = decoder.decode(chunk)
                if "\x00" in text:
                    return Inspection(error_code="invalid_asset_text")
                characters += len(text)
                if characters > policy.max_text_characters:
                    return Inspection(error_code="asset_text_too_long")
            characters += len(decoder.decode(b"", final=True))
            if characters > policy.max_text_characters:
                return Inspection(error_code="asset_text_too_long")
            return Inspection(facts=MediaFacts("text/plain; charset=utf-8"))
        except UnicodeError:
            return Inspection(error_code="invalid_asset_text")
        finally:
            body.seek(0)


@contextmanager
def prepared_body(source: BinaryIO, policy: AssetPolicy) -> Iterator[tuple[BinaryIO, int, str]]:
    # TemporaryFile's rollover is private/unlinked on Linux. No client path is
    # used, and the context removes bytes on every return/error/disconnect.
    with SpooledTemporaryFile(max_size=min(policy.max_bytes, 1024 * 1024), mode="w+b") as body:
        size = 0
        sha = hashlib.sha256()
        while chunk := source.read(65536):
            if not isinstance(chunk, bytes):
                raise AssetError("invalid_asset_stream")
            size += len(chunk)
            if size > policy.max_bytes:
                raise AssetError("asset_too_large")
            sha.update(chunk)
            body.write(chunk)
        body.seek(0)
        yield cast(BinaryIO, body), size, "sha256:" + sha.hexdigest()


@dataclass(frozen=True, slots=True)
class AssetDownload:
    asset: AssetRecord
    proof: VerifiedObject
    offset: int
    length: int
    partial: bool


class AssetService:
    def __init__(
        self,
        metadata: AssetMetadata,
        objects: AssetObjects,
        inspector: AssetInspector,
        policy: AssetPolicy,
    ) -> None:
        self._metadata = metadata
        self._objects = objects
        self._inspector = inspector
        self.policy = policy

    def upload(
        self,
        source: BinaryIO,
        *,
        workspace_id: str,
        purpose: AssetPurpose,
        ttl_seconds: int | None = None,
        expected_sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> UploadResult:
        if idempotency_key is not None and not 8 <= len(idempotency_key) <= 128:
            raise AssetError("invalid_idempotency_key")
        proposed_id = "asset-" + uuid.uuid4().hex
        key_digest = hash_bytes((idempotency_key or proposed_id).encode("utf-8"))
        with prepared_body(source, self.policy) as (body, size, sha):
            intent = UploadIntent(
                workspace_id, purpose, size, sha, key_digest, ttl_seconds, expected_sha256
            )
            error = intent.content_rejection()
            inspection = (
                Inspection(error_code=error)
                if error
                else self._inspector.inspect(purpose, body, self.policy)
            )
            result = self._metadata.reserve(
                intent,
                asset_id=proposed_id,
                object_key=f"private/assets/{proposed_id}/{uuid.uuid4().hex}",
                inspection=inspection,
                policy=self.policy,
            )
            if result.asset is None or result.asset.status != "staging":
                return result
            asset = result.asset
            if result.outcome in ("unknown", "conflict", "rejected"):
                return result
            if asset.phase != "reserved":
                return self.reconcile(asset.asset_id)
            self._objects.check_writes_available()
            started = self._metadata.begin_write(asset.asset_id, "writer-" + uuid.uuid4().hex)
            if started.outcome != "created" or started.asset is None:
                return UploadResult(started.outcome, started.asset or asset, started.error_code)
            body.seek(0)
            try:
                proof = self._objects.put_verified(started.asset, body)
            except AssetError:
                # A PUT may have reached storage. Do not issue another PUT or
                # pretend failed/absent from a lost response; inspect the original.
                return UploadResult("unknown", started.asset, "asset_upload_unknown")
            published = self._metadata.publish(proof)
            return UploadResult(
                published.outcome, published.asset or started.asset, published.error_code
            )

    def reconcile(self, asset_id: str) -> UploadResult:
        asset, _ = self._metadata.get(asset_id)
        if asset is None:
            raise AssetError("asset_not_found")
        if asset.status == "ready":
            return UploadResult("replayed", asset)
        if asset.status == "rejected":
            return UploadResult("rejected", asset, asset.error_code)
        if asset.phase == "reserved":
            return UploadResult("in_progress", asset, "operation_in_progress")
        proof = self._objects.inspect_original(asset)
        if proof is None:
            return UploadResult("unknown", asset, "asset_upload_unknown")
        proof.require_original(asset)
        return self._metadata.publish(proof)

    def metadata(self, asset_id: str) -> AssetRecord:
        asset, now = self._metadata.get(asset_id)
        if asset is None:
            raise AssetError("asset_not_found")
        if asset.status in ("expired", "deleted") or asset.expires_at_us <= now:
            raise AssetError("asset_unavailable")
        return asset

    def download(self, asset_id: str, range_header: str | None = None) -> AssetDownload:
        asset = self.metadata(asset_id)
        if asset.status != "ready":
            raise AssetError("asset_unavailable")
        proof = self._metadata.object(asset_id)
        if proof is None:
            raise AssetError("asset_unavailable")
        proof.require_original(asset)
        offset, length, partial = byte_range(range_header, asset.bytes)
        return AssetDownload(asset, proof, offset, length, partial)

    def content(self, download: AssetDownload) -> Generator[bytes]:
        remaining = download.length
        with self._objects.open(download.proof, download.offset, download.length) as reader:
            while remaining:
                chunk = reader.read(min(65536, remaining))
                if not chunk or len(chunk) > remaining:
                    raise AssetError("asset_content_incomplete")
                remaining -= len(chunk)
                yield chunk

    def maintain(self, limit: int = 10) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("Asset maintenance must be bounded")
        for asset_id in self._metadata.pending(limit):
            try:
                self.reconcile(asset_id)
            except AssetError:
                continue  # Persistent original identity remains for the next poll.
        for asset_id in self._metadata.expired(limit):
            try:
                ticket = self._metadata.prepare_expiry(asset_id, "ledger-" + uuid.uuid4().hex)
                if ticket is None or ticket.state != "queued":
                    continue
                if ticket.object is not None:
                    self._objects.delete_version(ticket.object)
                self._metadata.complete_deletion(ticket.ledger_id)
            except AssetError:
                continue  # Persistent ledger; no success without confirmed deletion.
