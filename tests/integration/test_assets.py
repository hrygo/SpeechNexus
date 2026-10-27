"""Actual PG + versioned MinIO publication, uncertainty and retention behavior."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import local
from typing import Any, cast

import psycopg
import pytest
from minio import Minio
from minio.commonconfig import ENABLED
from minio.versioningconfig import VersioningConfig
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.exc import ProgrammingError
from tests.integration.test_coordination_foundation import (
    UpgradeSandbox,
)
from tests.integration.test_coordination_foundation import (
    upgrade_sandbox as upgrade_sandbox,
)
from tests.integration.test_production_jobs import (
    FaultConnection,
    JobDatabase,
    database,  # noqa: F401 -- shared real PG migration/role fixture
)
from tests.support.migrations import HEAD

from speechnexus_backend.speech.application.assets import AssetService, TextAssetInspector
from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetPolicy,
    AssetRecord,
    UploadIntent,
    VerifiedObject,
    hash_bytes,
)
from speechnexus_backend.speech.infrastructure.asset_schema import asset_runtime_grants
from speechnexus_backend.speech.infrastructure.assets_minio import MinioAssetObjects
from speechnexus_backend.speech.infrastructure.assets_postgres import PostgresAssetMetadata
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
BODY = "Synthetic text: 合成内容。\n".encode()


@dataclass
class Assets:
    db: JobDatabase
    service: AssetService
    metadata: PostgresAssetMetadata
    objects: MinioAssetObjects
    admin: Minio
    bucket: str

    def upload(self, body: bytes = BODY, **kw: Any) -> Any:
        return self.service.upload(
            io.BytesIO(body), workspace_id="workspace-default", purpose="text_input", **kw
        )

    def age(self, asset_id: str) -> None:
        self.db.query(
            "UPDATE speech_assets SET created_at_us = "
            "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - 61000000, "
            "expires_at_us = CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - 1 "
            "WHERE asset_id = %s",
            (asset_id,),
        )

    def versions(self) -> list[Any]:
        return list(self.admin.list_objects(self.bucket, recursive=True, include_version=True))


@pytest.fixture
def assets(database: JobDatabase) -> Iterator[Assets]:  # noqa: F811 -- pytest fixture
    variables = (
        "SPEECHNEXUS_TEST_MINIO_ENDPOINT",
        "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY",
        "SPEECHNEXUS_TEST_MINIO_SECRET_KEY",
    )
    values = [os.environ.get(key) for key in variables]
    if not all(values):
        pytest.fail("Asset integration requires an explicit isolated MinIO endpoint and test keys")
    endpoint, access, secret = cast(tuple[str, str, str], tuple(values))
    assert endpoint.startswith("http://"), "The isolated synthetic fixture uses explicit local HTTP"
    admin = Minio(
        endpoint.removeprefix("http://"),
        access_key=access,
        secret_key=secret,
        secure=False,
        region="us-east-1",
    )
    bucket = "sn-assets-" + uuid.uuid4().hex
    admin.make_bucket(bucket)
    admin.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
    for grant in asset_runtime_grants(database.schema, database.role):
        database.query(grant)
    assert database.pool is not None
    objects = MinioAssetObjects(endpoint, bucket, access, secret)
    metadata = PostgresAssetMetadata(database.pool, namespace_digest=objects.namespace_digest)
    service = AssetService(metadata, objects, TextAssetInspector(), AssetPolicy(60))
    try:
        yield Assets(database, service, metadata, objects, admin, bucket)
    finally:
        objects.close()
        for item in admin.list_objects(bucket, recursive=True, include_version=True):
            admin.remove_object(bucket, item.object_name, version_id=item.version_id)
        admin.remove_bucket(bucket)


def test_real_publication_pinned_content_range_and_process_rebuild(assets: Assets) -> None:
    result = assets.upload(idempotency_key="original-key")
    assert result.outcome == "created" and result.asset.status == "ready"
    asset = result.asset
    assert asset.sha256 == hash_bytes(BODY) and asset.bytes == len(BODY)
    assert assets.metadata.object(asset.asset_id) is not None
    assert len(assets.versions()) == 1
    rebuilt = AssetService(assets.metadata, assets.objects, TextAssetInspector(), AssetPolicy(None))
    assert b"".join(rebuilt.content(rebuilt.download(asset.asset_id))) == BODY
    assert b"".join(rebuilt.content(rebuilt.download(asset.asset_id, "bytes=1-4"))) == BODY[1:5]
    assert b"".join(rebuilt.content(rebuilt.download(asset.asset_id, "bytes=-4"))) == BODY[-4:]
    # Replay resolves original effective TTL before changed/unavailable defaults.
    replay = rebuilt.upload(
        io.BytesIO(BODY),
        workspace_id="workspace-default",
        purpose="text_input",
        idempotency_key="original-key",
    )
    assert replay.outcome == "replayed" and replay.asset == asset
    assert len(assets.versions()) == 1


@pytest.mark.parametrize("change", ["body", "purpose", "ttl", "expected"])
def test_same_idempotency_key_changed_request_never_adds_content(
    assets: Assets, change: str
) -> None:
    original = assets.upload(idempotency_key="original-key")
    options: dict[str, Any] = {
        "workspace_id": "workspace-default",
        "purpose": "text_input",
        "idempotency_key": "original-key",
    }
    body = BODY
    if change == "body":
        body += b"different"
    elif change == "purpose":
        options["purpose"] = "asr_input"
    elif change == "ttl":
        options["ttl_seconds"] = 120
    else:
        options["expected_sha256"] = hash_bytes(BODY)
    result = assets.service.upload(io.BytesIO(body), **options)
    assert result.outcome == "conflict" and result.asset == original.asset
    assert len(assets.versions()) == 1


@pytest.mark.parametrize(
    "body,error",
    [(b"", "asset_empty"), (b"\xff", "invalid_asset_text"), (b"nul\0text", "invalid_asset_text")],
)
def test_rejection_is_durable_and_writes_no_object(assets: Assets, body: bytes, error: str) -> None:
    result = assets.upload(body, idempotency_key="original-key")
    assert result.outcome == "rejected" and result.asset.status == "rejected"
    assert result.error_code == error
    assert assets.upload(body, idempotency_key="original-key").asset == result.asset
    assert assets.versions() == []
    assert assets.metadata.object(result.asset.asset_id) is None
    with pytest.raises(AssetError, match="asset_unavailable"):
        assets.service.download(result.asset.asset_id)


def test_digest_mismatch_and_missing_inspector_do_not_fake_media(assets: Assets) -> None:
    result = assets.upload(expected_sha256=hash_bytes(b"other"))
    assert result.error_code == "asset_digest_mismatch"
    with pytest.raises(AssetError, match="asset_inspection_unavailable"):
        assets.service.upload(
            io.BytesIO(b"synthetic audio"), workspace_id="workspace-default", purpose="asr_input"
        )
    assert assets.versions() == []
    assert len(assets.db.query("SELECT * FROM speech_assets")) == 1


def test_bounded_receive_and_unconfigured_retention_do_not_reserve(assets: Assets) -> None:
    service = AssetService(
        assets.metadata, assets.objects, TextAssetInspector(), AssetPolicy(None, 8)
    )
    with pytest.raises(AssetError, match="asset_too_large"):
        service.upload(io.BytesIO(BODY), workspace_id="workspace-default", purpose="text_input")
    with pytest.raises(AssetError, match="asset_retention_not_configured"):
        service.upload(io.BytesIO(b"text"), workspace_id="workspace-default", purpose="text_input")
    explicit = service.upload(
        io.BytesIO(b"text"), workspace_id="workspace-default", purpose="text_input", ttl_seconds=60
    )
    assert explicit.asset is not None and explicit.asset.status == "ready"
    assert len(assets.db.query("SELECT * FROM speech_assets")) == 1


def test_versioning_is_required_before_any_external_write(assets: Assets) -> None:
    assets.admin.set_bucket_versioning(assets.bucket, VersioningConfig("Suspended"))
    with pytest.raises(AssetError, match="asset_storage_versioning_required"):
        assets.upload(idempotency_key="original-key")
    row = assets.db.query("SELECT * FROM speech_assets")[0]
    assert row["upload_phase"] == "reserved" and row["writer_id"] is None
    assert assets.versions() == []
    assets.admin.set_bucket_versioning(assets.bucket, VersioningConfig(ENABLED))
    assert assets.upload(idempotency_key="original-key").asset.status == "ready"


def test_concurrent_same_identity_has_one_writer_and_no_io_in_metadata_transaction(
    assets: Assets, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = assets.objects.put_verified
    puts: list[str] = []
    assert assets.db.pool is not None
    connection_context = assets.db.pool.connection
    thread_state = local()

    @contextmanager
    def tracked_connection(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with connection_context(*args, **kwargs) as connection:
            thread_state.depth = getattr(thread_state, "depth", 0) + 1
            try:
                yield connection
            finally:
                thread_state.depth -= 1

    monkeypatch.setattr(assets.db.pool, "connection", tracked_connection)

    def checked_put(asset: AssetRecord, body: Any) -> VerifiedObject:
        # Other concurrent uploads may hold their own short transaction. Only
        # the calling upload must release its metadata connection before I/O.
        assert getattr(thread_state, "depth", 0) == 0
        puts.append(asset.writer_id or "")
        return original(asset, body)

    monkeypatch.setattr(assets.objects, "put_verified", checked_put)
    blocker = assets.db.blocker()
    blocker.execute("SELECT * FROM speech_workspaces FOR UPDATE")
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(assets.upload, idempotency_key="concurrent-key") for _ in range(2)
            ]
            try:
                assets.db.await_waiter()
            finally:
                blocker.commit()
            results = [future.result(timeout=10) for future in futures]
    finally:
        blocker.close()
    assert len({result.asset.asset_id for result in results}) == 1
    assert len(puts) == len(assets.versions()) == 1
    assert assets.upload(idempotency_key="concurrent-key").asset.status == "ready"


@pytest.mark.parametrize("stored", [False, True])
def test_lost_object_reply_queries_original_without_another_put(
    assets: Assets, monkeypatch: pytest.MonkeyPatch, stored: bool
) -> None:
    original = assets.objects.put_verified
    puts = 0

    def lost(asset: AssetRecord, body: Any) -> VerifiedObject:
        nonlocal puts
        puts += 1
        if stored:
            original(asset, body)
        raise AssetError("synthetic_lost_object_reply")

    monkeypatch.setattr(assets.objects, "put_verified", lost)
    first = assets.upload(idempotency_key="uncertain-key")
    assert first.outcome == "unknown" and first.asset.phase == "writing"
    retried = assets.upload(idempotency_key="uncertain-key")
    assert retried.outcome == ("created" if stored else "unknown")
    assert puts == 1
    assert len(assets.versions()) == int(stored)
    assets.age(first.asset.asset_id)
    assets.service.maintain()
    ledger = assets.db.query("SELECT * FROM speech_content_deletions")[0]
    assert ledger["deletion_state"] == ("complete" if stored else "blocked")
    assert assets.versions() == []


def test_retention_blocks_live_references_then_deletes_only_pinned_version(assets: Assets) -> None:
    asset = assets.upload().asset
    proof = assets.metadata.object(asset.asset_id)
    assert proof is not None
    # An unrelated later storage version is never swept by the original ledger.
    newer = assets.admin.put_object(assets.bucket, proof.object_key, io.BytesIO(b"other"), 5)
    assets.db.query(
        "INSERT INTO speech_asset_references (reference_id,asset_id,consumer_kind,consumer_id,"
        "consumer_revision,created_at_us) VALUES ('reference-test',%s,'job','job-test','1',%s)",
        (asset.asset_id, asset.created_at_us),
    )
    assets.age(asset.asset_id)
    with pytest.raises(AssetError, match="asset_unavailable"):
        assets.service.download(asset.asset_id)
    assets.service.maintain()
    assert assets.db.query("SELECT deletion_state FROM speech_content_deletions") == [
        {"deletion_state": "blocked"}
    ]
    assert len(assets.versions()) == 2
    assets.db.query("UPDATE speech_asset_references SET released_at_us = created_at_us + 1")
    assets.service.maintain()
    assert assets.metadata.get(asset.asset_id)[0].status == "deleted"  # type: ignore[union-attr]
    assert [(v.version_id, v.size) for v in assets.versions()] == [(newer.version_id, 5)]
    ledger = assets.db.query("SELECT * FROM speech_content_deletions")[0]
    assert (
        ledger["deletion_state"] == "complete" and ledger["storage_version_id"] == proof.version_id
    )
    assets.service.maintain()
    assert len(assets.db.query("SELECT * FROM speech_content_deletions")) == 1


def test_cleanup_lost_ack_is_not_reported_complete(
    assets: Assets, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = assets.upload().asset
    assets.age(asset.asset_id)
    original = assets.objects.delete_version

    def lost(proof: VerifiedObject) -> None:
        original(proof)
        raise AssetError("asset_cleanup_unconfirmed")

    monkeypatch.setattr(assets.objects, "delete_version", lost)
    assets.service.maintain()
    assert assets.versions() == []
    assert assets.db.query("SELECT deletion_state FROM speech_content_deletions")[0] == {
        "deletion_state": "queued"
    }
    monkeypatch.setattr(assets.objects, "delete_version", original)
    assets.service.maintain()
    assert assets.db.query("SELECT deletion_state FROM speech_content_deletions")[0] == {
        "deletion_state": "complete"
    }


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE speech_assets SET sha256 = 'changed'",
        "DELETE FROM speech_assets",
        "UPDATE speech_assets SET expires_at_us = expires_at_us + 1",
        "UPDATE speech_asset_objects SET storage_version_id = 'changed'",
        "DELETE FROM speech_asset_events",
        "UPDATE speech_workspaces SET workspace_state = 'disabled'",
        "INSERT INTO speech_asset_references (reference_id) VALUES ('forbidden')",
        "CREATE TABLE forbidden (id INTEGER)",
        "UPDATE speech_asset_storage_authority SET namespace_digest = 'changed'",
        "DELETE FROM speech_asset_storage_authority",
    ],
)
def test_fresh_asset_role_cannot_rewrite_identity_or_evidence(
    assets: Assets, statement: str
) -> None:
    assets.upload()
    assert assets.db.pool is not None
    with assets.db.pool.connection() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)
        connection.rollback()


@pytest.mark.parametrize("stage", ["reserve", "begin", "publish"])
@pytest.mark.parametrize("fault", ["before", "after"])
def test_pg_lost_commit_reply_never_reexecutes_write(
    assets: Assets, stage: str, fault: str
) -> None:
    def configure(connection: FaultConnection) -> None:
        connection.execute(f'SET search_path TO "{assets.db.schema}", pg_catalog, pg_temp')
        connection.commit()

    with ConnectionPool[FaultConnection](
        make_conninfo(assets.db.url, user=assets.db.role, password="synthetic_job_test"),
        connection_class=FaultConnection,
        kwargs={"row_factory": dict_row},
        configure=configure,
        min_size=1,
        max_size=1,
        timeout=3,
    ) as pool:
        metadata = PostgresAssetMetadata(
            cast(SpeechPool, pool), namespace_digest=assets.objects.namespace_digest
        )
        intent = UploadIntent(
            "workspace-default", "text_input", len(BODY), hash_bytes(BODY), hash_bytes(b"fault-key")
        )
        inspection = TextAssetInspector().inspect("text_input", io.BytesIO(BODY), AssetPolicy(60))
        kwargs = {
            "asset_id": "asset-fault",
            "object_key": "private/assets/asset-fault/" + "a" * 32,
            "inspection": inspection,
            "policy": AssetPolicy(60),
        }
        if stage != "reserve":
            assert metadata.reserve(intent, **kwargs).outcome == "created"  # type: ignore[arg-type]
        if stage == "publish":
            begun = metadata.begin_write("asset-fault", "writer-fault")
            assert begun.asset is not None
            proof = assets.objects.put_verified(begun.asset, io.BytesIO(BODY))
        with pool.connection() as connection:
            armed = connection
        armed.admin_url, armed.fault = assets.db.url, fault
        if stage == "reserve":
            result = metadata.reserve(intent, **kwargs)  # type: ignore[arg-type]
        elif stage == "begin":
            result = metadata.begin_write("asset-fault", "writer-fault")
        else:
            result = metadata.publish(proof)
        assert result.outcome == "unknown"
        rows = assets.db.query("SELECT * FROM speech_assets")
        assert len(rows) == (0 if stage == "reserve" and fault == "before" else 1)
        if rows:
            assert rows[0]["upload_phase"] == (
                "settled"
                if stage == "publish" and fault == "after"
                else "writing"
                if stage == "publish" or (stage == "begin" and fault == "after")
                else "reserved"
            )
        assert len(assets.versions()) == int(stage == "publish")
        # Read-only recovery of an original publication can settle the binding;
        # an absent write-start must never silently authorize another PUT.
        if stage == "publish":
            assert assets.service.reconcile("asset-fault").asset.status == "ready"  # type: ignore[union-attr]
            assert len(assets.versions()) == 1


def test_asset_upgrade_is_atomic_and_preserves_existing_coordination_history(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0002_coordination")
    acl = sandbox.admin.execute(
        "SELECT relname,relacl FROM pg_class WHERE relnamespace = %s::regnamespace "
        "AND relname IN ('coordination_jobs','coordination_outbox') ORDER BY relname",
        (sandbox.schema,),
    ).fetchall()
    sandbox.admin.execute("CREATE TABLE speech_content_deletions (sentinel BIGINT)")
    with pytest.raises(ProgrammingError) as failure:
        sandbox.upgrade()
    assert getattr(failure.value.orig, "sqlstate", None) == "42P07"
    assert sandbox.version() == "0002_coordination"
    tables = sandbox.admin.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
        "AND table_name LIKE 'speech_%%'",
        (sandbox.schema,),
    ).fetchall()
    assert tables == [{"table_name": "speech_content_deletions"}]
    sandbox.admin.execute("DROP TABLE speech_content_deletions")
    sandbox.upgrade()
    sandbox.upgrade()
    assert sandbox.version() == HEAD
    assert sandbox.admin.execute("SELECT count(*) AS n FROM coordination_jobs").fetchone()["n"] == 1
    assert (
        sandbox.admin.execute("SELECT count(*) AS n FROM coordination_outbox").fetchone()["n"] == 1
    )
    assert sandbox.admin.execute("SELECT workspace_id FROM speech_workspaces").fetchall() == [
        {"workspace_id": "workspace-default"}
    ]
    assert (
        sandbox.admin.execute(
            "SELECT relname,relacl FROM pg_class WHERE relnamespace = %s::regnamespace "
            "AND relname IN ('coordination_jobs','coordination_outbox') ORDER BY relname",
            (sandbox.schema,),
        ).fetchall()
        == acl
    )


def test_blocked_expiry_does_not_starve_later_deletable_content(
    assets: Assets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = assets.objects.put_verified

    def never_confirmed(asset: AssetRecord, body: Any) -> VerifiedObject:
        raise AssetError("asset_upload_unknown")

    monkeypatch.setattr(assets.objects, "put_verified", never_confirmed)
    unknown = assets.upload().asset
    monkeypatch.setattr(assets.objects, "put_verified", original)
    referenced, deletable = assets.upload().asset, assets.upload().asset
    assets.db.query(
        "INSERT INTO speech_asset_references (reference_id,asset_id,consumer_kind,consumer_id,"
        "consumer_revision,created_at_us) VALUES ('reference-test',%s,'job','job-test','1',%s)",
        (referenced.asset_id, referenced.created_at_us),
    )
    for asset in (unknown, referenced, deletable):
        assets.age(asset.asset_id)
    for _ in range(3):
        assets.service.maintain(limit=1)
    ledgers = assets.db.query("SELECT asset_id,deletion_state FROM speech_content_deletions")
    assert {r["asset_id"]: r["deletion_state"] for r in ledgers} == {
        unknown.asset_id: "blocked",
        referenced.asset_id: "blocked",
        deletable.asset_id: "complete",
    }
    assert len(assets.versions()) == 1


def test_unknown_recovery_does_not_starve_a_later_verifiable_original(
    assets: Assets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, calls = assets.objects.put_verified, 0

    def lost(asset: AssetRecord, body: Any) -> VerifiedObject:
        nonlocal calls
        calls += 1
        if calls == 3:
            original(asset, body)
        raise AssetError("asset_upload_unknown")

    monkeypatch.setattr(assets.objects, "put_verified", lost)
    results = [assets.upload() for _ in range(3)]
    assert all(result.outcome == "unknown" for result in results)
    for _ in range(3):
        assets.service.maintain(limit=1)
    states = [assets.metadata.get(result.asset.asset_id)[0] for result in results]
    assert [state.status for state in states if state is not None] == [
        "staging",
        "staging",
        "ready",
    ]
    assert calls == 3 and len(assets.versions()) == 1
