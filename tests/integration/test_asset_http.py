"""Actual HTTP -> non-owner PG -> versioned MinIO; synthetic bytes only."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from minio.commonconfig import ENABLED
from minio.versioningconfig import VersioningConfig
from psycopg.conninfo import make_conninfo
from pydantic import AnyHttpUrl, SecretStr
from speechnexus_contracts import parse
from tests.fake.readiness import StaticProbe
from tests.integration.test_assets import Assets
from tests.integration.test_assets import assets as assets
from tests.integration.test_production_jobs import database as database

from speechnexus_backend.app import create_app
from speechnexus_backend.asset_runtime import AssetRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetRecord,
    VerifiedObject,
    hash_bytes,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
BODY = "Actual HTTP synthetic text: 合成内容。\n".encode()


def configuration(storage: Assets, *, owner: bool = False) -> Settings:
    endpoint = os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"]
    dsn = (
        storage.db.url
        if owner
        else make_conninfo(storage.db.url, user=storage.db.role, password="synthetic_job_test")
    )
    return Settings(
        environment="test",
        process_role="job-worker",
        node_id="node-assets-test",
        instance_id="worker-assets-test",
        database_url=SecretStr(dsn),
        database_schema=storage.db.schema,
        object_storage_endpoint=AnyHttpUrl(endpoint),
        object_storage_healthcheck_url=AnyHttpUrl(endpoint + "/minio/health/live"),
        object_storage_bucket=storage.bucket,
        object_storage_access_key=SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"]),
        object_storage_secret_key=SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"]),
        asset_default_ttl_seconds=60,
        asset_allowed_hosts=("test",),
    )


@pytest.fixture
async def http_assets(assets: Assets) -> AsyncIterator[tuple[Assets, AsyncClient]]:
    app = create_app(
        settings=configuration(assets),
        assets=assets.service,
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield assets, client


async def test_public_upload_replay_range_expiry_and_private_metadata(http_assets: Any) -> None:
    storage, client = http_assets
    fields = {"purpose": "text_input", "sha256": hash_bytes(BODY)}
    headers = {"Idempotency-Key": "http-original-key"}
    first = await client.post(
        "/v1/nexus/assets",
        files={"file": ("../../private.txt", BODY)},
        data=fields,
        headers=headers,
    )
    assert first.status_code == 201
    record = parse("Asset", first.json())
    assert record["status"] == "ready" and record["sha256"] == hash_bytes(BODY)
    assert record["bytes"] == len(BODY)
    for private in (
        "object_key",
        "storage_version_id",
        "writer_id",
        "request_digest",
        "private.txt",
    ):
        assert private not in first.text
    location = first.headers["Location"]
    again = await client.post(
        "/v1/nexus/assets", files={"file": ("other-name.txt", BODY)}, data=fields, headers=headers
    )
    assert again.status_code == 201 and again.json() == first.json()
    changed = await client.post(
        "/v1/nexus/assets",
        files={"file": ("sample.txt", BODY + b"other")},
        data=fields,
        headers=headers,
    )
    assert changed.status_code == 409 and changed.headers["Location"] == location
    assert len(storage.versions()) == 1
    metadata = await client.get(location)
    assert metadata.json() == first.json()
    whole = await client.get(location + "/content")
    assert whole.status_code == 200 and whole.content == BODY
    partial = await client.get(location + "/content", headers={"Range": "bytes=1-4"})
    assert partial.status_code == 206 and partial.content == BODY[1:5]
    assert partial.headers["Content-Range"] == f"bytes 1-4/{len(BODY)}"
    invalid = await client.get(location + "/content", headers={"Range": f"bytes={len(BODY)}-"})
    assert invalid.status_code == 416 and invalid.headers["Content-Range"] == f"bytes */{len(BODY)}"
    storage.age(record["id"])
    assert (await client.get(location)).status_code == 410
    assert (await client.get(location + "/content")).status_code == 410
    storage.service.maintain()
    assert storage.versions() == []
    assert storage.db.query("SELECT deletion_state FROM speech_content_deletions") == [
        {"deletion_state": "complete"}
    ]


@pytest.mark.parametrize(
    "body,code",
    [(b"", "asset_empty"), (b"\xff", "invalid_asset_text"), (b"nul\0text", "invalid_asset_text")],
)
async def test_actual_rejected_upload_is_queryable_and_has_no_object(
    http_assets: Any, body: bytes, code: str
) -> None:
    storage, client = http_assets
    response = await client.post(
        "/v1/nexus/assets", files={"file": ("sample.txt", body)}, data={"purpose": "text_input"}
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    rejected = await client.get(response.headers["Location"])
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert len(storage.db.query("SELECT * FROM speech_assets")) == 1
    assert storage.versions() == []


async def test_audio_unavailable_and_unknown_workspace_are_truthful(http_assets: Any) -> None:
    storage, client = http_assets
    audio = await client.post(
        "/v1/nexus/assets", files={"file": ("sample.wav", BODY)}, data={"purpose": "asr_input"}
    )
    assert (
        audio.status_code == 503 and audio.json()["error"]["code"] == "asset_inspection_unavailable"
    )
    workspace = await client.post(
        "/v1/nexus/assets",
        files={"file": ("sample.txt", BODY)},
        data={"purpose": "text_input", "workspace_id": "workspace-missing"},
    )
    assert workspace.status_code == 404
    assert storage.db.query("SELECT * FROM speech_assets") == []
    assert storage.versions() == []


async def test_lost_put_reply_recovers_via_original_location_and_key(
    http_assets: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, client = http_assets
    original, calls = storage.objects.put_verified, 0

    def lost(asset: AssetRecord, body: Any) -> VerifiedObject:
        nonlocal calls
        calls += 1
        original(asset, body)
        raise AssetError("synthetic lost response")

    monkeypatch.setattr(storage.objects, "put_verified", lost)
    headers = {"Idempotency-Key": "http-unknown-key"}
    response = await client.post(
        "/v1/nexus/assets",
        files={"file": ("sample.txt", BODY)},
        data={"purpose": "text_input"},
        headers=headers,
    )
    assert response.status_code == 503 and response.headers["X-Should-Retry"] == "false"
    assert response.json()["error"]["retryable"] is False
    location = response.headers["Location"]
    known = await client.get(location)
    assert known.status_code == 200 and known.json()["status"] == "staging"
    retried = await client.post(
        "/v1/nexus/assets",
        files={"file": ("sample.txt", BODY)},
        data={"purpose": "text_input"},
        headers=headers,
    )
    assert retried.status_code == 201 and retried.json()["status"] == "ready"
    assert retried.headers["Location"] == location and calls == 1
    assert len(storage.versions()) == 1


def test_configured_runtime_rejects_owner_and_uses_actual_non_owner(assets: Assets) -> None:
    with pytest.raises(AssetError, match="asset_runtime_role_rejected"):
        AssetRuntime(configuration(assets, owner=True)).open()
    runtime = AssetRuntime(configuration(assets))
    runtime.open()
    try:
        assert runtime.service.metadata(assets.upload().asset.asset_id).status == "ready"
    finally:
        runtime.close()


@pytest.mark.parametrize("login_kind", ["superuser", "schema_owner", "noinherit_owner_member"])
def test_asset_login_authority_cannot_hide_behind_restricted_startup_role(
    assets: Assets,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    login_kind: str,
) -> None:
    import psycopg
    from psycopg import sql

    db = assets.db
    original_owner = db.admin.info.user
    login, owner = db.role + "_login", db.role + "_owner"
    created_login = created_owner = False
    runtime: AssetRuntime | None = None
    calls: list[str] = []
    try:
        if login_kind == "superuser":
            url, session = db.url, db.query("SELECT SESSION_USER AS name")[0]["name"]
        else:
            db.query(f"CREATE ROLE \"{login}\" LOGIN NOINHERIT PASSWORD 'synthetic_asset_owner'")
            created_login = True
            db.query(f'GRANT "{db.role}" TO "{login}"')
            authority = login
            if login_kind == "noinherit_owner_member":
                db.query(f'CREATE ROLE "{owner}" NOLOGIN')
                created_owner = True
                db.query(f'GRANT "{owner}" TO "{login}"')
                authority = owner
            db.query(f'ALTER SCHEMA "{db.schema}" OWNER TO "{authority}"')
            url = make_conninfo(db.url, user=login, password="synthetic_asset_owner")
            session = login
        url = make_conninfo(
            url, options=f"-c role={db.role}", application_name="synthetic_asset_login_canary"
        )
        with psycopg.connect(url) as probe:
            assert probe.execute("SELECT SESSION_USER, CURRENT_USER").fetchone() == (
                session,
                db.role,
            )
            probe.execute("SET ROLE NONE")
            assert probe.execute("SELECT CURRENT_USER").fetchone() == (session,)
            probe.execute("RESET ROLE")
            assert probe.execute("SELECT CURRENT_USER").fetchone() == (db.role,)
            if login_kind == "noinherit_owner_member":
                assert probe.execute(
                    "SELECT pg_has_role(%s::name,%s::name,'USAGE'), "
                    "pg_has_role(%s::name,%s::name,'MEMBER')",
                    (login, owner, login, owner),
                ).fetchone() == (False, True)
        runtime = AssetRuntime(
            configuration(assets).model_copy(update={"database_url": SecretStr(url)})
        )
        check_namespace = runtime._metadata.check_namespace
        check_objects = runtime._objects.check_writes_available

        def namespace(*args: Any, **kw: Any) -> None:
            calls.append("namespace")
            check_namespace(*args, **kw)

        def objects() -> None:
            calls.append("objects")
            check_objects()

        monkeypatch.setattr(runtime._metadata, "check_namespace", namespace)
        monkeypatch.setattr(runtime._objects, "check_writes_available", objects)
        before = db.query("SELECT * FROM speech_asset_storage_authority")
        with pytest.raises(AssetError, match="asset_runtime_role_rejected") as caught:
            runtime.open()
        assert runtime._pool.closed and calls == []
        assert db.query("SELECT * FROM speech_asset_storage_authority") == before
        assert db.query("SELECT * FROM speech_assets") == [] and assets.versions() == []
        for secret in (url, "synthetic_asset_owner", "synthetic_asset_login_canary"):
            assert secret not in str(caught.value) and secret not in caplog.text
    finally:
        if runtime is not None:
            runtime.close()
        if created_login:
            db.admin.execute(
                sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                    sql.Identifier(db.schema), sql.Identifier(original_owner)
                )
            )
            db.query(f'DROP OWNED BY "{login}"')
            db.query(f'DROP ROLE "{login}"')
        if created_owner:
            db.query(f'DROP OWNED BY "{owner}"')
            db.query(f'DROP ROLE "{owner}"')


def test_installed_worker_replays_pinned_retention_ledger(assets: Assets) -> None:
    asset = assets.upload().asset
    assets.age(asset.asset_id)
    settings = configuration(assets)
    worker_env = {
        **os.environ,
        "SPEECHNEXUS_PROCESS_ROLE": "job-worker",
        "SPEECHNEXUS_NODE_ID": "node-test",
        "SPEECHNEXUS_INSTANCE_ID": "worker-test",
        "SPEECHNEXUS_DATABASE_SCHEMA": assets.db.schema,
        "SPEECHNEXUS_DATABASE_URL": settings.database_url.get_secret_value(),
        "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": str(settings.object_storage_endpoint),
        "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": str(settings.object_storage_healthcheck_url),
        "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": assets.bucket,
        "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
        "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
    }
    result = subprocess.run(
        [sys.executable, "-m", "speechnexus_backend.asset_worker", "--once"],
        env=worker_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert assets.versions() == []
    assert assets.db.query("SELECT deletion_state FROM speech_content_deletions") == [
        {"deletion_state": "complete"}
    ]


def test_different_storage_namespace_cannot_complete_an_original_deletion(assets: Assets) -> None:
    from speechnexus_backend.speech.application.assets import AssetService, TextAssetInspector
    from speechnexus_backend.speech.domain.assets import AssetPolicy
    from speechnexus_backend.speech.infrastructure.assets_minio import MinioAssetObjects
    from speechnexus_backend.speech.infrastructure.assets_postgres import PostgresAssetMetadata

    asset = assets.upload().asset
    assets.age(asset.asset_id)
    original_namespace = assets.objects.namespace_digest
    other_bucket = "sn-other-" + uuid.uuid4().hex
    assets.admin.make_bucket(other_bucket)
    assets.admin.set_bucket_versioning(other_bucket, VersioningConfig(ENABLED))
    other = MinioAssetObjects(
        os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"],
        other_bucket,
        os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
        os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
    )
    assert assets.db.pool is not None
    try:
        wrong = AssetService(
            PostgresAssetMetadata(assets.db.pool, namespace_digest=other.namespace_digest),
            other,
            TextAssetInspector(),
            AssetPolicy(60),
        )
        with pytest.raises(AssetError, match="asset_storage_namespace_mismatch"):
            wrong.maintain()
        assert len(assets.versions()) == 1
        assert assets.db.query("SELECT * FROM speech_content_deletions") == []
        assert assets.db.query("SELECT namespace_digest FROM speech_asset_storage_authority") == [
            {"namespace_digest": original_namespace}
        ]
        # A mismatched origin/bucket is refused before sending even S3 credentials.
        settings = configuration(assets).model_copy(update={"object_storage_bucket": other_bucket})
        with pytest.raises(AssetError, match="asset_storage_namespace_mismatch"):
            AssetRuntime(settings).open()
    finally:
        other.close()
        assets.admin.remove_bucket(other_bucket)
    assets.service.maintain()
    assert assets.versions() == []


def test_existing_unbound_assets_require_verified_adoption_not_an_inferred_bucket(
    assets: Assets,
) -> None:
    # Simulate upgrading an old 0003 authority with existing data but no 0004 fact.
    assets.upload()
    assets.db.query("DELETE FROM speech_asset_storage_authority")  # Test administrator only.
    with pytest.raises(AssetError, match="asset_storage_adoption_required"):
        AssetRuntime(configuration(assets)).open()
    assert assets.db.query("SELECT * FROM speech_asset_storage_authority") == []
    assert len(assets.versions()) == 1


def test_daemon_sigterm_stops_after_observed_retention_work(assets: Assets) -> None:
    asset = assets.upload().asset
    assets.age(asset.asset_id)
    settings = configuration(assets)
    worker_env = {
        **os.environ,
        "SPEECHNEXUS_PROCESS_ROLE": "job-worker",
        "SPEECHNEXUS_NODE_ID": "node-test",
        "SPEECHNEXUS_INSTANCE_ID": "worker-test",
        "SPEECHNEXUS_DATABASE_SCHEMA": assets.db.schema,
        "SPEECHNEXUS_DATABASE_URL": settings.database_url.get_secret_value(),
        "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": str(settings.object_storage_endpoint),
        "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": str(settings.object_storage_healthcheck_url),
        "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": assets.bucket,
        "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
        "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
    }

    worker_env["SPEECHNEXUS_ASSET_MAINTENANCE_INTERVAL_SECONDS"] = "300"
    process = subprocess.Popen(
        [sys.executable, "-m", "speechnexus_backend.asset_worker"],
        env=worker_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = assets.db.query("SELECT deletion_state FROM speech_content_deletions")
            if rows == [{"deletion_state": "complete"}]:
                break
            assert process.poll() is None, "worker exited before its first durable result"
            time.sleep(0.01)
        else:
            pytest.fail("No completed ledger was observed from the actual worker process")
        process.send_signal(signal.SIGTERM)
        _, diagnostic = process.communicate(timeout=5)
        assert process.returncode == 0, diagnostic
        assert assets.versions() == []
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


async def test_configured_app_lifespan_publishes_through_its_owned_runtime(assets: Assets) -> None:
    settings = configuration(assets).model_copy(
        update={"process_role": "api", "instance_id": "api-assets-test"}
    )
    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        uploaded = await client.post(
            "/v1/nexus/assets",
            files={"file": ("sample.txt", BODY)},
            data={"purpose": "text_input"},
        )
        assert uploaded.status_code == 201
        downloaded = await client.get(uploaded.headers["Location"] + "/content")
        assert downloaded.status_code == 200 and downloaded.content == BODY
    assert len(assets.versions()) == 1


@pytest.mark.parametrize(
    "deployment_ttl,requested_ttl,expected_ttl",
    [
        (None, None, 604800),
        ("3600", None, 3600),
        (None, 120, 120),
        ("3600", 120, 120),
        ("60", None, 60),
    ],
)
async def test_process_retention_defaults_overrides_and_replay_survive_reconfiguration(
    assets: Assets,
    monkeypatch: pytest.MonkeyPatch,
    deployment_ttl: str | None,
    requested_ttl: int | None,
    expected_ttl: int,
) -> None:
    def settings_for(value: str | None) -> Settings:
        if value is None:
            monkeypatch.delenv("SPEECHNEXUS_ASSET_DEFAULT_TTL_SECONDS", raising=False)
        else:
            monkeypatch.setenv("SPEECHNEXUS_ASSET_DEFAULT_TTL_SECONDS", value)
        # Rebuild from process configuration, instead of overriding AssetPolicy or
        # injecting the fixture's fixed-TTL service into the real application.
        values = configuration(assets).model_dump(exclude={"asset_default_ttl_seconds"})
        values.update(process_role="api", instance_id="api-retention-test")
        return Settings(**values)

    headers = {"Idempotency-Key": "original-retention-key"}
    fields = {"purpose": "text_input"}
    if requested_ttl is not None:
        fields["ttl_seconds"] = str(requested_ttl)
    app = create_app(settings=settings_for(deployment_ttl))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        first = await client.post(
            "/v1/nexus/assets",
            files={"file": ("synthetic.txt", BODY)},
            data=fields,
            headers=headers,
        )
        assert first.status_code == 201
        original = assets.db.query("SELECT * FROM speech_assets")[0]
        assert original["expires_at_us"] - original["created_at_us"] == expected_ttl * 1000000
        assert (await client.get(first.headers["Location"] + "/content")).content == BODY

    # Includes the old 60-second deployment policy -> new seven-day process
    # default: replay must retain the old expiry and must not add another object.
    changed_default = "7200" if deployment_ttl is None else None
    settings = settings_for(changed_default)
    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        replay = await client.post(
            "/v1/nexus/assets", files={"file": ("replayed.txt", BODY)}, data=fields, headers=headers
        )
        assert replay.status_code == 201 and replay.json() == first.json()
        assert replay.headers["Location"] == first.headers["Location"]
        assert assets.db.query("SELECT * FROM speech_assets") == [original]
        assert len(assets.versions()) == 1
        # Even an explicit TTL equal to the old effective default is a different
        # request from omission. A new configuration cannot reinterpret its key.
        changed_fields = fields | {
            "ttl_seconds": str(expected_ttl if requested_ttl is None else 180)
        }
        conflict = await client.post(
            "/v1/nexus/assets",
            files={"file": ("changed.txt", BODY)},
            data=changed_fields,
            headers=headers,
        )
        assert conflict.status_code == 409
        assert assets.db.query("SELECT * FROM speech_assets") == [original]
        fresh = await client.post(
            "/v1/nexus/assets",
            files={"file": ("fresh.txt", BODY)},
            data={"purpose": "text_input"},
            headers={"Idempotency-Key": "new-retention-key"},
        )
        assert fresh.status_code == 201
        row = assets.db.query(
            "SELECT * FROM speech_assets WHERE asset_id = %s", (fresh.json()["id"],)
        )[0]
        assert settings.asset_default_ttl_seconds is not None
        assert (
            row["expires_at_us"] - row["created_at_us"]
            == settings.asset_default_ttl_seconds * 1000000
        )
        assert len(assets.versions()) == 2
