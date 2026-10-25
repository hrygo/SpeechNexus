"""Startup replays the accepted-deletion ledger before the runtime serves content."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from uuid import uuid4

import httpx
import psycopg
import pytest
from minio import Minio
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_http_management import acceptance
from tests.integration.test_workbench_runtime import serve
from tests.integration.test_workbench_saved_assets_cleanup import (
    BUCKET,
    CONTENT,
    DIGEST,
    Objects,
)

from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.application.saved_assets import ObjectZoneUnavailableError
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.domain.deletion import DeletionTarget
from speechnexus_backend.workbench.domain.saved_assets import (
    saved_object_key,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.minio_object_zone import MinioObjectZone
from speechnexus_backend.workbench.infrastructure.postgres_deletions import (
    PostgresDeletionUnitOfWork,
)
from speechnexus_backend.workbench.infrastructure.postgres_owned_deletions import (
    PostgresOwnedDeletionCatalog,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
)
from speechnexus_backend.workbench.infrastructure.runtime_schema import (
    bootstrap_sql,
    cleanup_grants_sql,
    runtime_grants_sql,
)
from speechnexus_backend.workbench.runtime import RuntimeStartupError, create_runtime_app
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

pytestmark = pytest.mark.integration

WORKSPACE = "workspace-runtime"


@dataclass
class Probe:
    owner_dsn: str = ""
    schema: str = ""
    role: str = ""
    cleanup_role: str = ""
    cleanup_dsn: str = ""

    def runtime_settings(self, *, cleanup_url: str | None = None) -> WorkbenchSettings:
        return WorkbenchSettings(
            database_url=make_conninfo(self.owner_dsn, user=self.role),
            database_schema=self.schema,
            workspace_id=WORKSPACE,
            deployment_id="synthetic-runtime",
            cursor_key="synthetic-32-byte-cursor-key-test-only",
            cleanup_database_url=self.cleanup_dsn if cleanup_url is None else cleanup_url,
        )


@pytest.fixture
async def probe() -> AsyncIterator[Probe]:
    dsn = os.environ.get("SPEECHNEXUS_WORKBENCH_TEST_DSN")
    if not dsn:
        pytest.skip("disposable workbench PG probe not configured")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as owner:
        guard = await (
            await owner.execute(
                "SELECT current_database(),current_user,current_setting('listen_addresses'),"
                "current_setting('speechnexus.disposable_workbench_probe',true)"
            )
        ).fetchone()
        assert guard == ("speechnexus_workbench_test", "workbench_test_owner", "", "on"), (
            "refusing a database outside the disposable Unix-socket-only probe"
        )
        value = Probe(
            owner_dsn=dsn,
            schema="wb_recovery_" + uuid4().hex,
            role="wb_recovery_app_" + uuid4().hex,
            cleanup_role="wb_recovery_clean_" + uuid4().hex,
        )
        value.cleanup_dsn = make_conninfo(dsn, user=value.cleanup_role)
        try:
            await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(value.role)))
            await owner.execute(
                sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(value.cleanup_role))
            )
            async with owner.transaction():
                await owner.execute(bootstrap_sql(value.schema))
                await owner.execute(runtime_grants_sql(value.schema, value.role))
                await owner.execute(cleanup_grants_sql(value.schema, value.cleanup_role))
            yield value
        finally:
            await owner.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(value.schema))
            )
            for role in (value.role, value.cleanup_role):
                await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


async def pending(probe: Probe) -> tuple[str, ...]:
    async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
        rows = await (
            await owner.execute(
                sql.SQL(
                    "SELECT deletion_id FROM {}.deletion_outbox WHERE completed_at IS NULL"
                    " ORDER BY deletion_id"
                ).format(sql.Identifier(probe.schema))
            )
        ).fetchall()
    return tuple(row[0] for row in rows)


async def projects_left(probe: Probe, project_id: str) -> int:
    async with await psycopg.AsyncConnection.connect(probe.owner_dsn) as owner:
        counted = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.projects WHERE id = %s").format(
                    sql.Identifier(probe.schema)
                ),
                (project_id,),
            )
        ).fetchone()
    assert counted is not None
    return int(counted[0])


async def owed_deletion(probe: Probe, suffix: str = "recovery") -> tuple[str, str]:
    """Accept one project deletion through the public application path."""
    app_dsn = make_conninfo(probe.owner_dsn, user=probe.role)
    versions = WorkbenchVersions(PostgresVersionUnitOfWork(app_dsn, schema=probe.schema))
    project = await versions.create_project(
        workspace_id=WORKSPACE, name="合成稿件", idempotency_key=f"{suffix}-project"
    )
    deletions = WorkbenchDeletions(
        PostgresDeletionUnitOfWork(
            PostgresVersionUnitOfWork(app_dsn, schema=probe.schema),
            meetings=True,
            owned=PostgresOwnedDeletionCatalog(),
        ),
        digest_plan=plan_digest,
    )
    plan = await deletions.preview(
        workspace_id=WORKSPACE,
        target=DeletionTarget("workbench", "project", project.id, project.metadata_revision),
        mode="cascade",
        key=f"{suffix}-plan",
    )
    record = await deletions.accept(
        workspace_id=WORKSPACE,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key=f"{suffix}-delete",
    )
    return project.id, record.id


async def test_startup_replays_pending_deletions_before_serving(probe: Probe) -> None:
    project_id, deletion_id = await owed_deletion(probe)
    assert await pending(probe) == (deletion_id,)
    app = create_runtime_app(probe.runtime_settings())

    async with app.router.lifespan_context(app):
        # The accepted deletion is already kept when the runtime opens for requests.
        assert await pending(probe) == ()
        assert await projects_left(probe, project_id) == 0


async def test_a_second_start_over_a_settled_ledger_is_a_noop(probe: Probe) -> None:
    project_id, deletion_id = await owed_deletion(probe)
    app = create_runtime_app(probe.runtime_settings())
    async with app.router.lifespan_context(app):
        assert await pending(probe) == ()

    # Completion lives in the ledger, not in the process: a restart neither redoes work
    # nor re-opens content.
    async with app.router.lifespan_context(app):
        assert await pending(probe) == ()
        assert await projects_left(probe, project_id) == 0
    assert deletion_id not in await pending(probe)


async def test_a_privileged_cleanup_identity_refuses_startup(probe: Probe) -> None:
    project_id, deletion_id = await owed_deletion(probe)
    settings = probe.runtime_settings(cleanup_url=probe.owner_dsn)

    # Erasing confirmed content as the schema owner would bypass the narrow grants a
    # deletion worker must hold, so a privileged cleanup DSN must not start at all.
    with pytest.raises(RuntimeStartupError):
        app = create_runtime_app(settings)
        async with app.router.lifespan_context(app):
            pass
    assert await pending(probe) == (deletion_id,)
    assert await projects_left(probe, project_id) == 1


async def test_an_unusable_cleanup_identity_refuses_startup(probe: Probe) -> None:
    project_id, deletion_id = await owed_deletion(probe)
    settings = probe.runtime_settings(
        cleanup_url=make_conninfo(probe.owner_dsn, user="wb_absent_" + uuid4().hex)
    )

    with pytest.raises(RuntimeStartupError):
        app = create_runtime_app(settings)
        async with app.router.lifespan_context(app):
            pass
    assert await pending(probe) == (deletion_id,)
    assert await projects_left(probe, project_id) == 1


async def test_startup_without_pending_deletions_serves(probe: Probe) -> None:
    assert await pending(probe) == ()
    app = create_runtime_app(probe.runtime_settings())

    async with app.router.lifespan_context(app):
        assert await pending(probe) == ()


@pytest.mark.parametrize("kind", ["project", "transcript"])
async def test_runtime_recovery_includes_confirmed_meetings(probe: Probe, kind: str) -> None:
    settings = probe.runtime_settings().model_copy(update={"cleanup_interval_seconds": 60})
    async with serve(create_runtime_app(settings)) as client:
        created = await client.post("/v1/workbench/projects", json={"name": "合成会议删除项目"})
        assert created.status_code == 201, created.text
        project = created.json()
        source = await client.post(
            f"/v1/workbench/projects/{project['id']}/transcripts",
            json={"display_name": "合成会议原稿", "source": {"kind": "manual", "text": "合成正文"}},
        )
        assert source.status_code == 201, source.text
        transcript = source.json()["transcript"]
        meeting = await client.post(
            f"/v1/workbench/projects/{project['id']}/meetings",
            json={"display_name": "合成会议", "transcript_id": transcript["id"]},
        )
        assert meeting.status_code == 201, meeting.text
        plan = await client.post(
            "/v1/workbench/deletion-plans",
            json={
                "mode": "cascade",
                "target": {
                    "owner": "workbench",
                    "kind": kind,
                    "resource": {
                        "id": project["id"] if kind == "project" else transcript["id"],
                        "revision": "1",
                    },
                },
            },
            headers={"Idempotency-Key": "meeting-plan"},
        )
        assert plan.status_code == 200, plan.text
        assert any(d["target"]["kind"] == "meeting" for d in plan.json()["dependencies"])
        accepted = await client.post(
            "/v1/workbench/deletions",
            json=acceptance(plan.json()),
            headers={"Idempotency-Key": "meeting-delete"},
        )
        assert accepted.status_code == 202, accepted.text
        deletion_id = accepted.json()["id"]
    assert await pending(probe) == (deletion_id,)

    # Only the production composition and its real lifespan are allowed to replay
    # this deletion: directly constructing a correctly configured worker hides the bug.
    for _ in range(2):
        app = create_runtime_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client,
        ):
            current = await client.get(f"/v1/workbench/deletions/{deletion_id}")
            assert current.status_code == 200, current.text
            assert current.json()["status"] == "succeeded"
            assert current.json()["cleanup_state"] == "complete"
            assert any(t["kind"] == "meeting" for t in current.json()["result"]["deleted"])
            assert (
                await client.get(f"/v1/workbench/meetings/{meeting.json()['id']}")
            ).status_code == 404
            assert (await client.get(f"/v1/workbench/projects/{project['id']}")).status_code == (
                404 if kind == "project" else 200
            )
        assert await pending(probe) == ()


DEAD_ZONE = "http://127.0.0.1:1"


@pytest.fixture(scope="module")
def minio() -> Iterator[Objects]:
    """Real object zone plus its client, for the object half of the promise."""
    endpoint = os.environ.get("SPEECHNEXUS_TEST_MINIO_ENDPOINT")
    access = os.environ.get("SPEECHNEXUS_TEST_MINIO_ACCESS_KEY")
    secret = os.environ.get("SPEECHNEXUS_TEST_MINIO_SECRET_KEY")
    assert endpoint and access and secret, "isolated MinIO service is required for object evidence"
    zone = MinioObjectZone(endpoint, BUCKET, access, secret, prefix="saved")
    host = endpoint.split("://", 1)[-1]
    client = Minio(host, access_key=access, secret_key=secret, secure=False)
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
    try:
        yield Objects(zone, client)
    finally:
        zone.close()


def zone_settings(probe: Probe, endpoint: str) -> WorkbenchSettings:
    """Runtime settings that own a real object zone at the given endpoint."""
    return WorkbenchSettings(
        database_url=make_conninfo(probe.owner_dsn, user=probe.role),
        database_schema=probe.schema,
        workspace_id=WORKSPACE,
        deployment_id="synthetic-runtime",
        cursor_key="synthetic-32-byte-cursor-key-test-only",
        cleanup_database_url=probe.cleanup_dsn,
        # The saved-asset configuration is all-or-none, so an owned object zone also
        # names the public speech origin; recovery only uses the zone.
        speech_base_url="http://127.0.0.1:59999",
        object_endpoint=endpoint,
        object_bucket=BUCKET,
        object_access_key=os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
        object_secret_key=os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
    )


async def test_an_object_zone_failure_keeps_the_deletion_owed(probe: Probe, minio: Objects) -> None:
    project_id, deletion_id = await owed_deletion(probe)
    key = saved_object_key(project_id, DIGEST)
    await minio.zone.put(key=key, body=io.BytesIO(CONTENT), length=len(CONTENT))
    assert minio.names(project_id) == [f"saved/{key}"]

    # Start 1 commits the row half, then the object sweep fails. The deletion is not
    # finished, so it must stay owed.
    with pytest.raises(ObjectZoneUnavailableError):
        app = create_runtime_app(zone_settings(probe, DEAD_ZONE))
        async with app.router.lifespan_context(app):
            pass
    assert await projects_left(probe, project_id) == 0
    assert minio.names(project_id) == [f"saved/{key}"]

    # Start 2 sees the row half already published. Serving now would reopen bytes the
    # deployment still owns, so recovery must still refuse.
    with pytest.raises(ObjectZoneUnavailableError):
        app = create_runtime_app(zone_settings(probe, DEAD_ZONE))
        async with app.router.lifespan_context(app):
            pass
    assert minio.names(project_id) == [f"saved/{key}"]

    # Start 3 with a working zone settles the object half before serving.
    app = create_runtime_app(zone_settings(probe, os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"]))
    async with app.router.lifespan_context(app):
        assert await pending(probe) == ()
    assert minio.names(project_id) == []
    assert deletion_id not in await pending(probe)


async def test_a_broad_predefined_role_membership_refuses_startup(probe: Probe) -> None:
    _, deletion_id = await owed_deletion(probe)
    role = "wb_broad_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        await owner.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(probe.schema), sql.Identifier(role)
            )
        )
        await owner.execute(sql.SQL("GRANT pg_write_all_data TO {}").format(sql.Identifier(role)))
    try:
        with pytest.raises(RuntimeStartupError):
            app = create_runtime_app(
                probe.runtime_settings(cleanup_url=make_conninfo(probe.owner_dsn, user=role))
            )
            async with app.router.lifespan_context(app):
                pass
        assert await pending(probe) == (deletion_id,)
    finally:
        async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
            await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


async def test_a_cleanup_identity_without_schema_usage_refuses_startup(probe: Probe) -> None:
    project_id, deletion_id = await owed_deletion(probe)
    role = "wb_nousage_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    try:
        with pytest.raises(RuntimeStartupError):
            app = create_runtime_app(
                probe.runtime_settings(cleanup_url=make_conninfo(probe.owner_dsn, user=role))
            )
            async with app.router.lifespan_context(app):
                pass
        assert await pending(probe) == (deletion_id,)
        assert await projects_left(probe, project_id) == 1
    finally:
        async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
            await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


async def test_a_benign_role_switch_in_the_cleanup_dsn_is_settled_by_the_login_identity(
    probe: Probe,
) -> None:
    project_id, deletion_id = await owed_deletion(probe)
    plain = "wb_plain_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
        # A member of an unprivileged role: the DSN selects it, and the check plus every
        # cleanup connection must fall back to the login identity that holds the grants.
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(plain)))
        await owner.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(plain), sql.Identifier(probe.cleanup_role)
            )
        )
    try:
        switched = make_conninfo(
            probe.owner_dsn, user=probe.cleanup_role, options=f"-c role={plain}"
        )
        app = create_runtime_app(probe.runtime_settings(cleanup_url=switched))
        async with app.router.lifespan_context(app):
            assert await pending(probe) == ()
        assert await projects_left(probe, project_id) == 0
        assert deletion_id not in await pending(probe)
    finally:
        async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
            await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(plain)))
            await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(plain)))


async def test_a_deployment_without_a_cleanup_identity_cannot_accept_deletions(
    probe: Probe,
) -> None:
    settings = probe.runtime_settings().model_copy(update={"cleanup_database_url": None})
    body = {
        "mode": "cascade",
        "target": {
            "owner": "workbench",
            "kind": "project",
            "resource": {"id": "project-absent", "revision": "1"},
        },
    }

    async with serve(create_runtime_app(settings)) as client:
        # Unrelated content stays available; only the deletion promise is withheld.
        assert (await client.get("/v1/workbench/status")).status_code == 200
        refused = await client.post(
            "/v1/workbench/deletion-plans",
            json=body,
            headers={"Idempotency-Key": "no-cleanup-plan"},
        )
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "deletions_unavailable"
        assert (await client.get("/v1/workbench/deletions/absent-deletion")).status_code == 503

    # Nothing was accepted, so no deployment can owe an erasure it cannot perform.
    assert await pending(probe) == ()


async def test_a_deletion_accepted_while_serving_settles_without_a_restart(probe: Probe) -> None:
    settings = probe.runtime_settings().model_copy(update={"cleanup_interval_seconds": 0.05})

    async with serve(create_runtime_app(settings)) as client:
        assert (await client.get("/v1/workbench/status")).status_code == 200
        # Accepted while the process is already serving: the ledger, not a restart, is
        # what makes the sweep pick it up.
        project_id, deletion_id = await owed_deletion(probe)
        assert await pending(probe) == (deletion_id,)

        for _ in range(200):
            if not await pending(probe):
                break
            await asyncio.sleep(0.05)

        assert await pending(probe) == ()
        assert await projects_left(probe, project_id) == 0
        # The runtime keeps serving after the sweep, so cancellation stays bounded.
        assert (await client.get("/v1/workbench/status")).status_code == 200

    # Shutdown must stop the loop: a deletion accepted afterwards stays owed until the
    # next start replays the ledger.
    _, later = await owed_deletion(probe, "later")
    await asyncio.sleep(0.2)
    assert await pending(probe) == (later,)


async def test_a_shadowing_search_path_cannot_forge_a_safe_cleanup_identity(
    probe: Probe,
) -> None:
    """The identity check must resolve catalogs inside pg_catalog, never the DSN path."""
    project_id, deletion_id = await owed_deletion(probe)
    evil = "wb_shadow_" + uuid4().hex
    subject = "wb_shadowed_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as owner:
        # The subject is narrow by attribute but holds the owner's membership and the
        # worker's grants: the real catalogs must refuse it, forged ones must not decide.
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(subject)))
        await owner.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier("workbench_test_owner"), sql.Identifier(subject)
            )
        )
        await owner.execute(cleanup_grants_sql(probe.schema, subject))
        await owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(evil)))
        await owner.execute(
            sql.SQL(
                "CREATE TABLE {}.pg_roles (rolname text, oid oid, rolsuper bool,"
                " rolcreatedb bool, rolcreaterole bool, rolreplication bool, rolbypassrls bool)"
            ).format(sql.Identifier(evil))
        )
        await owner.execute(
            sql.SQL("CREATE TABLE {}.pg_namespace (nspname text, oid oid, nspowner oid)").format(
                sql.Identifier(evil)
            )
        )
        await owner.execute(
            sql.SQL("CREATE TABLE {}.pg_database (datname text, datdba oid)").format(
                sql.Identifier(evil)
            )
        )
        await owner.execute(
            sql.SQL(
                "INSERT INTO {}.pg_roles SELECT r.rolname, r.oid, false, false, false,"
                " false, false FROM pg_roles r WHERE r.rolname = %s"
            ).format(sql.Identifier(evil)),
            (subject,),
        )
        await owner.execute(
            sql.SQL(
                "INSERT INTO {}.pg_namespace SELECT n.nspname, n.oid, c.oid"
                " FROM pg_namespace n, pg_roles c WHERE n.nspname = %s AND c.rolname = %s"
            ).format(sql.Identifier(evil)),
            (probe.schema, probe.cleanup_role),
        )
        await owner.execute(
            sql.SQL(
                "INSERT INTO {}.pg_database SELECT current_database(), c.oid"
                " FROM pg_roles c WHERE c.rolname = %s"
            ).format(sql.Identifier(evil)),
            (probe.cleanup_role,),
        )
    settings = probe.runtime_settings(
        cleanup_url=make_conninfo(
            probe.owner_dsn, user=subject, options=f"-c search_path={evil},pg_catalog"
        )
    )

    try:
        with pytest.raises(RuntimeStartupError):
            app = create_runtime_app(settings)
            async with app.router.lifespan_context(app):
                pass
        assert await pending(probe) == (deletion_id,)
        assert await projects_left(probe, project_id) == 1
    finally:
        async with await psycopg.AsyncConnection.connect(probe.owner_dsn, autocommit=True) as own:
            await own.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(evil))
            )
            await own.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(subject)))
            await own.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(subject)))
