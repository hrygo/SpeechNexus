"""Actual non-owner PG reads and HTTP composition, with synthetic shared groups."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from speechnexus_contracts.admin_http import validate_response
from tests.fake.readiness import StaticProbe
from tests.integration.test_asset_http import configuration
from tests.integration.test_assets import Assets
from tests.integration.test_assets import assets as assets
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.domain.workspaces import (
    WorkspaceNotFoundError,
    WorkspaceQuery,
    WorkspaceUnavailableError,
)
from speechnexus_backend.speech.infrastructure.asset_schema import asset_runtime_grants
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.workspaces_postgres import PostgresWorkspaceReads

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
PREFIX = "/v1/nexus/workspaces"


@pytest.fixture
def reads(database: JobDatabase) -> Iterator[PostgresWorkspaceReads]:
    for grant in asset_runtime_grants(database.schema, database.role):
        database.query(grant)
    assert database.pool is not None
    yield PostgresWorkspaceReads(database.pool)


def insert(database: JobDatabase, identity: str, *, state: str = "active") -> None:
    database.query(
        "INSERT INTO speech_workspaces (workspace_id,display_name,workspace_state,"
        "metadata_revision,policy_revision,created_at_us) "
        "VALUES (%s,%s,%s,7,9007199254740993,1)",
        (identity, "Synthetic group", state),
    )


def test_authoritative_default_and_lossless_versions_survive_pool_rebuild(
    database: JobDatabase, reads: PostgresWorkspaceReads
) -> None:
    before = database.query("SELECT * FROM speech_workspaces ORDER BY workspace_id")
    service = WorkspaceService(reads)
    default = service.get("workspace-default")
    assert default.workspace_id == before[0]["workspace_id"]
    assert default.created_at_us == before[0]["created_at_us"]
    insert(database, "workspace-disabled", state="disabled")
    record = service.get("workspace-disabled")
    assert record.status == "disabled" and record.policy_revision == 2**53 + 1
    with pytest.raises(WorkspaceNotFoundError):
        service.get("workspace-absent")
    pool = speech_pool(
        make_conninfo(database.url, user=database.role, password="synthetic_job_test"),
        schema=database.schema,
    )
    pool.open()
    try:
        assert WorkspaceService(PostgresWorkspaceReads(pool)).get("workspace-disabled") == record
        with pool.connection() as connection:
            row = connection.execute(
                "SELECT current_user AS principal, "
                "pg_catalog.has_schema_privilege(%s,'CREATE') AS ddl",
                (database.schema,),
            ).fetchone()
            assert row is not None and row["principal"] == database.role and row["ddl"] is False
    finally:
        pool.close()
    assert (
        database.query("SELECT * FROM speech_workspaces WHERE workspace_id='workspace-default'")
        == before
    )


def test_pagination_is_ordered_bounded_keyset_without_snapshot_claims(
    database: JobDatabase, reads: PostgresWorkspaceReads
) -> None:
    insert(database, "workspace-aaa")
    insert(database, "workspace-zzz")
    service = WorkspaceService(reads)
    page = service.list(WorkspaceQuery(1))
    assert [row.workspace_id for row in page.items] == ["workspace-aaa"]
    assert page.next_id == "workspace-aaa"
    # A later insertion behind the key does not rewind an in-progress listing.
    insert(database, "workspace-000")
    insert(database, "workspace-bbb")
    next_page = service.list(WorkspaceQuery(2, page.next_id))
    assert [row.workspace_id for row in next_page.items] == ["workspace-bbb", "workspace-default"]
    final_page = service.list(WorkspaceQuery(2, next_page.next_id))
    assert [row.workspace_id for row in final_page.items] == ["workspace-zzz"]
    assert final_page.next_id is None


def test_adapter_transaction_rejects_even_an_otherwise_granted_write(
    database: JobDatabase, reads: PostgresWorkspaceReads
) -> None:
    before = database.query("SELECT * FROM speech_workspaces")
    with pytest.raises(WorkspaceUnavailableError):
        reads._read(
            "UPDATE speech_workspaces SET metadata_revision=metadata_revision+1 RETURNING "
            "workspace_id,display_name,workspace_state,metadata_revision,policy_revision,created_at_us",
            (),
        )
    assert database.query("SELECT * FROM speech_workspaces") == before
    assert reads.get("workspace-default") is not None


def test_missing_default_is_missing_authority_instead_of_a_synthesized_record(
    database: JobDatabase, reads: PostgresWorkspaceReads
) -> None:
    database.query("DELETE FROM speech_workspaces WHERE workspace_id='workspace-default'")
    assert WorkspaceService(reads).list(WorkspaceQuery(10)).items == ()
    with pytest.raises(WorkspaceNotFoundError):
        WorkspaceService(reads).get("workspace-default")
    assert database.query("SELECT * FROM speech_workspaces") == []


@pytest.mark.parametrize(
    "change",
    ["display_name=''", "metadata_revision=9007199254740992", "created_at_us=9007199254740992"],
)
def test_unrepresentable_rows_fail_closed(
    database: JobDatabase, reads: PostgresWorkspaceReads, change: str
) -> None:
    # Static synthetic corruption cases, never user-provided SQL fragments.
    database.query("UPDATE speech_workspaces SET " + change)
    with pytest.raises(WorkspaceUnavailableError):
        reads.get("workspace-default")


def test_waiting_for_a_table_lock_is_bounded_and_does_not_write(
    database: JobDatabase, reads: PostgresWorkspaceReads
) -> None:
    assert database.pool is not None
    blocker = database.blocker()
    try:
        blocker.execute("LOCK TABLE speech_workspaces IN ACCESS EXCLUSIVE MODE")
        start = time.monotonic()
        with pytest.raises(WorkspaceUnavailableError):
            PostgresWorkspaceReads(database.pool, timeout_seconds=0.05).get("workspace-default")
        assert time.monotonic() - start < 3
    finally:
        blocker.rollback()
        blocker.close()
    assert reads.get("workspace-default") is not None


async def test_actual_configured_root_shares_pool_and_retains_asset_routes(assets: Assets) -> None:
    settings = configuration(assets)
    insert(assets.db, "workspace-aaa", state="disabled")
    for _ in range(2):
        app = create_app(
            settings=settings,
            readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
        )
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            before = assets.db.query("SELECT * FROM speech_workspaces ORDER BY workspace_id")
            response = await client.get(PREFIX)
            assert response.status_code == 200
            validate_response("listWorkspaces", 200, {"body": response.json()})
            assert response.json()["data"][0]["id"] == before[0]["workspace_id"]
            assert response.json()["data"][0]["metadata_revision"] == before[0]["metadata_revision"]
            get = await client.get(PREFIX + "/workspace-default")
            expected_default = next(
                row for row in response.json()["data"] if row["id"] == "workspace-default"
            )
            assert get.json() == expected_default
            created = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                microseconds=before[0]["created_at_us"]
            )
            assert response.json()["data"][0]["created_at"] == created.isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z")
            assert response.json()["data"][0]["policy_revision"] == str(
                before[0]["policy_revision"]
            )
            assert response.json()["data"][0]["status"] == "disabled"
            first = (await client.get(PREFIX, params={"limit": 1})).json()
            assert first["has_more"] is True and first["data"][0]["id"] == "workspace-aaa"
            second = (
                await client.get(PREFIX, params={"limit": 1, "cursor": first["next_cursor"]})
            ).json()
            assert second["data"] == [expected_default] and second["next_cursor"] is None
            assert (await client.get(PREFIX + "/workspace-missing")).status_code == 404
            assert (await client.get(PREFIX, params={"cursor": "malformed"})).status_code == 400
            assert (
                await client.get(PREFIX, params=[("limit", "1"), ("limit", "2")])
            ).status_code == 400
            assert (
                await client.get(PREFIX, headers={"Origin": "https://unapproved.invalid"})
            ).status_code == 403
            assert (
                assets.db.query("SELECT * FROM speech_workspaces ORDER BY workspace_id") == before
            )
            upload = await client.post(
                "/v1/nexus/assets",
                files={"file": ("synthetic.txt", b"Synthetic group read regression")},
                data={"purpose": "text_input"},
            )
            assert upload.status_code == 201
            assert (await client.get(upload.headers["Location"])).status_code == 200


def test_native_tcp_serves_the_owned_runtime_and_strict_read_boundary(assets: Assets) -> None:
    settings = configuration(assets).model_copy(
        update={
            "asset_allowed_hosts": ("127.0.0.1",),
            "process_role": "api",
            "instance_id": "api-workspace-tcp",
        }
    )
    app = create_app(settings=settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="critical", access_log=False, proxy_headers=False, server_header=False
        )
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            serving = workers.submit(server.run, sockets=[sock])
            try:
                deadline = time.monotonic() + 5
                while not server.started and not serving.done() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert server.started, "the workspace API did not complete startup"
                before = assets.db.query("SELECT * FROM speech_workspaces ORDER BY workspace_id")
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}",
                    timeout=5,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    listing = client.get(
                        PREFIX, headers={"X-Request-ID": "synthetic-workspace-tcp"}
                    )
                    assert listing.status_code == 200
                    validate_response(
                        "listWorkspaces",
                        200,
                        {"body": listing.json(), "headers": dict(listing.headers)},
                    )
                    assert listing.headers["X-Request-ID"] == "synthetic-workspace-tcp"
                    assert listing.json()["data"][0]["id"] == "workspace-default"
                    assert (
                        client.get(PREFIX + "/workspace-default").json()
                        == listing.json()["data"][0]
                    )
                    assert client.get(PREFIX + "?limit=1&limit=2").status_code == 400
                    assert (
                        client.get(
                            PREFIX, headers={"Origin": "https://unapproved.invalid"}
                        ).status_code
                        == 403
                    )
                    assert (
                        client.request(
                            "GET", PREFIX, content=b"Synthetic disallowed body"
                        ).status_code
                        == 400
                    )
                    assert client.get(PREFIX + "/workspace-missing").status_code == 404
                    assert (
                        client.post(PREFIX, json={"display_name": "Synthetic"}).status_code == 405
                    )
                    assert client.get("/readyz").status_code == 200
                assert (
                    assets.db.query("SELECT * FROM speech_workspaces ORDER BY workspace_id")
                    == before
                )
            finally:
                server.should_exit = True
                serving.result(timeout=10)
    finally:
        sock.close()
