"""Short PostgreSQL asset transactions; no object I/O and no uncertain-write retry."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.assets import DeletionTicket
from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetPolicy,
    AssetRecord,
    Inspection,
    MediaFacts,
    UploadIntent,
    UploadResult,
    VerifiedObject,
    begin_asset_write,
    digest,
    expiry_blocker,
    identifier,
    publish_asset,
    reserve_asset,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw = cursor
        self.deadline = deadline

    def rows(self, sql: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AssetError("asset_metadata_deadline")
        self.raw.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        self.raw.execute(sql, args)
        return self.raw.fetchall() if self.raw.description else []

    def clock(self) -> int:
        return cast(
            int,
            self.rows(
                "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS t"
            )[0]["t"],
        )

    def asset(self, asset_id: str, *, lock: bool = False) -> AssetRecord | None:
        rows = self.rows(
            "SELECT * FROM speech_assets WHERE asset_id = %s" + (" FOR UPDATE" if lock else ""),
            (asset_id,),
        )
        return _asset(rows[0]) if rows else None

    def proof(self, asset_id: str) -> VerifiedObject | None:
        rows = self.rows("SELECT * FROM speech_asset_objects WHERE asset_id = %s", (asset_id,))
        if not rows:
            return None
        row = rows[0]
        return VerifiedObject(
            row["asset_id"],
            row["object_key"],
            row["storage_version_id"],
            row["bytes"],
            row["sha256"],
            row["writer_id"],
        )

    def workspace(self, workspace_id: str) -> bool:
        rows = self.rows(
            "SELECT workspace_state FROM speech_workspaces WHERE workspace_id = %s FOR UPDATE",
            (workspace_id,),
        )
        if not rows:
            raise AssetError("workspace_not_found")
        return bool(rows[0]["workspace_state"] == "active")

    def save(self, asset: AssetRecord, kind: str, now: int) -> None:
        self.rows(
            "UPDATE speech_assets SET asset_state = %s, upload_phase = %s, writer_id = %s, "
            "error_code = %s, version = %s WHERE asset_id = %s",
            (
                asset.status,
                asset.phase,
                asset.writer_id,
                asset.error_code,
                asset.version,
                asset.asset_id,
            ),
        )
        self.event(asset, kind, now)

    def event(self, asset: AssetRecord, kind: str, now: int) -> None:
        # A fact kind occurs at most once for this immutable asset. Reconciliation
        # may encounter it again, but never rewrites its original timestamp.
        self.rows(
            "INSERT INTO speech_asset_events "
            "(event_id,asset_id,event_kind,asset_version,occurred_at_us) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (asset_id,event_kind) DO NOTHING",
            ("event-" + uuid.uuid4().hex, asset.asset_id, kind, asset.version, now),
        )


def _asset(row: dict[str, Any]) -> AssetRecord:
    return AssetRecord(
        row["asset_id"],
        row["workspace_id"],
        row["purpose"],
        row["asset_state"],
        row["bytes"],
        row["sha256"],
        MediaFacts(row["media_type"], row["duration_ms"], row["sample_rate_hz"], row["channels"]),
        row["created_at_us"],
        row["expires_at_us"],
        row["version"],
        row["upload_phase"],
        row["object_key"],
        row["request_digest"],
        row["writer_id"],
        row["error_code"],
    )


def _replay(asset: AssetRecord, intent: UploadIntent) -> UploadResult:
    if asset.request_digest != intent.request_digest:
        return UploadResult("conflict", asset, "idempotency_key_conflict")
    if asset.status == "rejected":
        return UploadResult("rejected", asset, asset.error_code)
    return UploadResult("replayed", asset)


class PostgresAssetMetadata:
    def __init__(
        self, pool: SpeechPool, *, namespace_digest: str, timeout_seconds: float = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Asset metadata requires bounded transaction deadlines")
        self._pool, self._timeout = pool, timeout_seconds
        self._namespace = digest(namespace_digest)

    @contextmanager
    def _transaction(self, *, readonly: bool = False) -> Iterator[_Cursor]:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                if readonly:
                    connection.execute("SET TRANSACTION READ ONLY")
                with connection.cursor() as raw:
                    cursor = _Cursor(raw, deadline)
                    self._require_namespace(cursor, readonly=readonly)
                    yield cursor
                    if time.monotonic() >= deadline:
                        raise AssetError("asset_metadata_deadline")
        except psycopg.errors.UniqueViolation:
            raise  # Only reserve may resolve its original idempotency identity.
        except psycopg.OperationalError, psycopg.InterfaceError:
            raise AssetError("asset_metadata_unknown") from None
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise AssetError("asset_metadata_unavailable") from None

    def check_namespace(self, *, initialize: bool = False) -> None:
        # Bootstraps an immutable fact only for an empty resource authority.
        # Existing assets without a declaration require verified A07 adoption.
        with self._transaction(readonly=not initialize):
            pass

    def _require_namespace(self, cursor: _Cursor, *, readonly: bool) -> None:
        def declared() -> str | None:
            rows = cursor.rows(
                "SELECT namespace_digest FROM speech_asset_storage_authority WHERE binding_id = 1"
            )
            return rows[0]["namespace_digest"] if rows else None

        found = declared()
        if found is None:
            if readonly:
                if cursor.rows("SELECT asset_id FROM speech_assets LIMIT 1"):
                    raise AssetError("asset_storage_adoption_required")
                raise AssetError("asset_storage_namespace_unbound")
            # All new writers enter this declaration check before other asset
            # locks. No object I/O or mutable topology is held in the transaction.
            cursor.rows(
                "SELECT workspace_id FROM speech_workspaces "
                "WHERE workspace_id = 'workspace-default' FOR UPDATE"
            )
            found = declared()
            if found is None:
                if cursor.rows("SELECT asset_id FROM speech_assets LIMIT 1"):
                    raise AssetError("asset_storage_adoption_required")
                cursor.rows(
                    "INSERT INTO speech_asset_storage_authority "
                    "(binding_id,namespace_digest,recorded_at_us) VALUES (1,%s,%s)",
                    (self._namespace, cursor.clock()),
                )
                found = self._namespace
        if found != self._namespace:
            raise AssetError("asset_storage_namespace_mismatch")

    def _original(self, cursor: _Cursor, intent: UploadIntent) -> AssetRecord | None:
        rows = cursor.rows(
            "SELECT a.* FROM speech_asset_commands c JOIN speech_assets a USING (asset_id) "
            "WHERE c.workspace_id = %s AND c.key_digest = %s",
            (intent.workspace_id, intent.key_digest),
        )
        return _asset(rows[0]) if rows else None

    def reserve(
        self,
        intent: UploadIntent,
        *,
        asset_id: str,
        object_key: str,
        inspection: Inspection,
        policy: AssetPolicy,
    ) -> UploadResult:
        try:
            with self._transaction() as cursor:
                # Resolve the original before consulting mutable policy/workspace
                # defaults. The workspace lock serializes new reservations only.
                original = self._original(cursor, intent)
                if original is not None:
                    return _replay(original, intent)
                active = cursor.workspace(intent.workspace_id)
                original = self._original(cursor, intent)
                if original is not None:
                    return _replay(original, intent)
                if not active:
                    raise AssetError("workspace_unavailable")
                now = cursor.clock()
                asset = reserve_asset(
                    intent, inspection, policy, asset_id=asset_id, object_key=object_key, now_us=now
                )
                cursor.rows(
                    "INSERT INTO speech_assets (asset_id,workspace_id,purpose,asset_state,"
                    "upload_phase,bytes,sha256,request_digest,object_key,media_type,duration_ms,"
                    "sample_rate_hz,channels,created_at_us,expires_at_us,error_code) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        asset.asset_id,
                        asset.workspace_id,
                        asset.purpose,
                        asset.status,
                        asset.phase,
                        asset.bytes,
                        asset.sha256,
                        asset.request_digest,
                        asset.object_key,
                        asset.media.media_type,
                        asset.media.duration_ms,
                        asset.media.sample_rate_hz,
                        asset.media.channels,
                        asset.created_at_us,
                        asset.expires_at_us,
                        asset.error_code,
                    ),
                )
                cursor.rows(
                    "INSERT INTO speech_asset_commands "
                    "(workspace_id,key_digest,request_digest,asset_id) VALUES (%s,%s,%s,%s)",
                    (intent.workspace_id, intent.key_digest, intent.request_digest, asset.asset_id),
                )
                cursor.event(asset, "created", now)
                if asset.status == "rejected":
                    cursor.event(asset, "rejected", now)
                return UploadResult(
                    "rejected" if asset.status == "rejected" else "created", asset, asset.error_code
                )
        except psycopg.errors.UniqueViolation:
            with self._transaction(readonly=True) as cursor:
                original = self._original(cursor, intent)
                if original is not None:
                    return _replay(original, intent)
            return UploadResult("conflict", error_code="asset_identity_conflict")
        except AssetError as error:
            if error.code == "asset_metadata_unknown":
                return UploadResult("unknown", error_code=error.code)
            raise

    @contextmanager
    def _locked_asset(self, asset_id: str) -> Iterator[tuple[_Cursor, AssetRecord, bool]]:
        identifier(asset_id)
        with self._transaction() as cursor:
            initial = cursor.asset(asset_id)
            if initial is None:
                raise AssetError("asset_not_found")
            active = cursor.workspace(initial.workspace_id)
            asset = cursor.asset(asset_id, lock=True)
            if asset is None:
                raise AssetError("asset_not_found")
            yield cursor, asset, active

    def begin_write(self, asset_id: str, writer_id: str) -> UploadResult:
        try:
            with self._locked_asset(asset_id) as (cursor, asset, active):
                if not active:
                    raise AssetError("workspace_unavailable")
                if asset.phase != "reserved":
                    return UploadResult("in_progress", asset, "operation_in_progress")
                now = cursor.clock()
                started = begin_asset_write(asset, writer_id, now)
                cursor.save(started, "write_started", now)
                return UploadResult("created", started)
        except AssetError as error:
            if error.code == "asset_metadata_unknown":
                return UploadResult("unknown", error_code=error.code)
            raise

    def publish(self, proof: VerifiedObject) -> UploadResult:
        try:
            with self._locked_asset(proof.asset_id) as (cursor, asset, active):
                now = cursor.clock()
                published = publish_asset(asset, proof, now_us=now, workspace_active=active)
                original = cursor.proof(asset.asset_id)
                if original is not None and original != proof:
                    return UploadResult("conflict", asset, "asset_publication_conflict")
                if original is None:
                    cursor.rows(
                        "INSERT INTO speech_asset_objects (asset_id,object_key,storage_version_id,"
                        "bytes,sha256,writer_id,verified_at_us) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            proof.asset_id,
                            proof.object_key,
                            proof.version_id,
                            proof.bytes,
                            proof.sha256,
                            proof.writer_id,
                            now,
                        ),
                    )
                if published != asset:
                    cursor.save(
                        published,
                        "published"
                        if published.status == "ready"
                        else "expired"
                        if published.status == "expired"
                        else "write_started",
                        now,
                    )
                return UploadResult(
                    "created" if published != asset and published.status == "ready" else "replayed",
                    published,
                )
        except AssetError as error:
            if error.code == "asset_metadata_unknown":
                return UploadResult("unknown", error_code=error.code)
            raise

    def get(self, asset_id: str) -> tuple[AssetRecord | None, int]:
        identifier(asset_id)
        with self._transaction(readonly=True) as cursor:
            return cursor.asset(asset_id), cursor.clock()

    def object(self, asset_id: str) -> VerifiedObject | None:
        identifier(asset_id)
        with self._transaction(readonly=True) as cursor:
            return cursor.proof(asset_id)

    def pending(self, limit: int) -> list[str]:
        return self._candidates(limit, pending=True)

    def expired(self, limit: int) -> list[str]:
        return self._candidates(limit, pending=False)

    def _candidates(self, limit: int, *, pending: bool) -> list[str]:
        if not 1 <= limit <= 100:
            raise ValueError("Asset scans must be bounded")
        predicate = (
            "(upload_phase = 'writing' OR (asset_state = 'staging' AND upload_phase = 'settled'))"
            if pending
            else "asset_state <> 'deleted' AND expires_at_us <= %s"
        )
        # These timestamps are non-authoritative poll hints, independent for
        # recovery and expiry. A permanently blocked original must not consume
        # the first bounded page on every tick and starve all later resources.
        polled = "reconcile_polled_at_us" if pending else "expiry_polled_at_us"
        with self._transaction() as cursor:
            args = (limit,) if pending else (cursor.clock(), limit)
            rows = cursor.rows(
                "SELECT asset_id FROM speech_assets WHERE "
                + predicate
                + f" ORDER BY {polled}, expires_at_us, asset_id LIMIT %s",
                args,
            )
            identities = [row["asset_id"] for row in rows]
            if identities:
                # Lock only the selected hint rows, in canonical identity order.
                # No workspace/pool/resource locks or object I/O occur here.
                cursor.rows(
                    "SELECT asset_id FROM speech_assets WHERE asset_id = ANY(%s) "
                    "ORDER BY asset_id FOR UPDATE",
                    (identities,),
                )
                cursor.rows(
                    f"UPDATE speech_assets SET {polled} = %s WHERE asset_id = ANY(%s)",
                    (cursor.clock(), identities),
                )
            return identities

    def prepare_expiry(self, asset_id: str, ledger_id: str) -> DeletionTicket | None:
        identifier(ledger_id)
        with self._locked_asset(asset_id) as (cursor, asset, _):
            now = cursor.clock()
            if asset.expires_at_us > now or asset.status == "deleted":
                return None
            if asset.status != "expired":
                asset = replace(asset, status="expired", version=asset.version + 1)
                cursor.save(asset, "expired", now)
            refs = cursor.rows(
                "SELECT reference_id FROM speech_asset_references "
                "WHERE asset_id = %s AND released_at_us IS NULL LIMIT 1",
                (asset_id,),
            )
            blocked = expiry_blocker(asset, has_references=bool(refs))
            proof = cursor.proof(asset_id)
            if proof is None and asset.phase == "settled" and asset.writer_id is not None:
                blocked = "asset_object_binding_missing"
            state = "blocked" if blocked else "queued"
            ledgers = cursor.rows(
                "SELECT ledger_id,deletion_state FROM speech_content_deletions "
                "WHERE asset_id = %s FOR UPDATE",
                (asset_id,),
            )
            if ledgers:
                ledger_id = ledgers[0]["ledger_id"]
                if ledgers[0]["deletion_state"] == "complete":
                    return DeletionTicket(ledger_id, asset_id, "complete", proof)
                cursor.rows(
                    "UPDATE speech_content_deletions SET storage_version_id = %s, "
                    "deletion_state = %s, blocked_reason = %s WHERE ledger_id = %s",
                    (proof.version_id if proof else None, state, blocked, ledger_id),
                )
            else:
                cursor.rows(
                    "INSERT INTO speech_content_deletions (ledger_id,asset_id,sha256,"
                    "storage_version_id,reason,deletion_state,blocked_reason,created_at_us) "
                    "VALUES (%s,%s,%s,%s,'retention_expired',%s,%s,%s)",
                    (
                        ledger_id,
                        asset_id,
                        asset.sha256,
                        proof.version_id if proof else None,
                        state,
                        blocked,
                        now,
                    ),
                )
            return DeletionTicket(ledger_id, asset_id, state, proof)

    def complete_deletion(self, ledger_id: str) -> bool:
        identifier(ledger_id)
        # Resolve immutable identity read-only, then take the same workspace ->
        # asset -> ledger lock order as every publication/expiry path.
        with self._transaction(readonly=True) as cursor:
            rows = cursor.rows(
                "SELECT asset_id FROM speech_content_deletions WHERE ledger_id = %s", (ledger_id,)
            )
        if not rows:
            return False
        with self._locked_asset(rows[0]["asset_id"]) as (cursor, asset, _):
            rows = cursor.rows(
                "SELECT deletion_state FROM speech_content_deletions "
                "WHERE ledger_id = %s FOR UPDATE",
                (ledger_id,),
            )
            if rows[0]["deletion_state"] == "complete":
                return True
            if asset.status != "expired" or rows[0]["deletion_state"] != "queued":
                return False
            if (
                cursor.rows(
                    "SELECT reference_id FROM speech_asset_references "
                    "WHERE asset_id = %s AND released_at_us IS NULL LIMIT 1",
                    (asset.asset_id,),
                )
                or asset.phase == "writing"
            ):
                return False
            now = cursor.clock()
            cursor.rows(
                "UPDATE speech_content_deletions SET deletion_state = 'complete', "
                "completed_at_us = %s WHERE ledger_id = %s",
                (now, ledger_id),
            )
            cursor.save(
                replace(asset, status="deleted", version=asset.version + 1), "object_deleted", now
            )
            return True
