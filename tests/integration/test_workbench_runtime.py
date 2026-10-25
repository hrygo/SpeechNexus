"""Owner bootstrap and real independent workbench lifespan on disposable PG."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_http_management import acceptance

from speechnexus_backend.workbench.infrastructure.runtime_schema import (
    SCHEMA_REVISION,
    bootstrap_sql,
    cleanup_grants_sql,
    runtime_grants_sql,
)
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

pytestmark = pytest.mark.integration


@dataclass
class Database:
    owner_dsn: str = field(repr=False)
    schema: str
    role: str
    cleanup_role: str = ""

    @property
    def cleanup_dsn(self) -> str:
        return make_conninfo(self.owner_dsn, user=self.cleanup_role)

    def settings(self, *, owner: bool = False) -> WorkbenchSettings:
        return WorkbenchSettings(
            database_url=self.owner_dsn if owner else make_conninfo(self.owner_dsn, user=self.role),
            database_schema=self.schema,
            workspace_id="workspace-runtime",
            deployment_id="synthetic-runtime",
            cursor_key="synthetic-32-byte-cursor-key-test-only",
            # Deletion routes stay unavailable until a cleanup identity exists, so every
            # fixture that exercises them configures one.
            cleanup_database_url=self.cleanup_dsn,
        )


@pytest.fixture
async def database() -> AsyncIterator[Database]:
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
        value = Database(
            dsn,
            "wb_root_" + uuid4().hex,
            "wb_app_" + uuid4().hex,
            "wb_cleanup_" + uuid4().hex,
        )
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


async def test_bootstrap_is_atomic_fresh_only_and_runtime_cannot_stamp(database: Database) -> None:
    schema = "wb_rollback_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(database.owner_dsn, autocommit=True) as owner:
        with pytest.raises(psycopg.errors.DivisionByZero):
            async with owner.transaction():
                await owner.execute(bootstrap_sql(schema))
                await owner.execute("SELECT 1/0")
        assert (
            await (
                await owner.execute("SELECT oid FROM pg_namespace WHERE nspname=%s", (schema,))
            ).fetchone()
            is None
        )
        with pytest.raises(psycopg.errors.DuplicateSchema):
            async with owner.transaction():
                await owner.execute(bootstrap_sql(database.schema))
        row = await (
            await owner.execute(
                sql.SQL("SELECT revision FROM {}.runtime_schema_revision").format(
                    sql.Identifier(database.schema)
                )
            )
        ).fetchone()
        assert row == (SCHEMA_REVISION,)
    async with await psycopg.AsyncConnection.connect(
        database.settings().database_url.get_secret_value(), autocommit=True
    ) as runtime:
        for query in (
            sql.SQL("UPDATE {}.runtime_schema_revision SET revision={}").format(
                sql.Identifier(database.schema), sql.Literal(SCHEMA_REVISION + 1)
            ),
            sql.SQL("CREATE TABLE {}.forbidden (id int)").format(sql.Identifier(database.schema)),
            sql.SQL("UPDATE {}.project_versions SET name='forbidden'").format(
                sql.Identifier(database.schema)
            ),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await runtime.execute(query)


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("runtime server did not start")
                await asyncio.sleep(0.005)
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            yield client
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()


async def test_env_factory_real_http_persistence_replay_meeting_and_deletion(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from speechnexus_backend.workbench.runtime import create_app_from_env

    for key, value in database.settings().model_dump().items():
        if value is None:
            # An unset optional service stays unconfigured instead of becoming "None".
            continue
        plain = (
            value.get_secret_value()
            if hasattr(value, "get_secret_value")
            else json.dumps(value)
            if isinstance(value, tuple)
            else str(value)
        )
        monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_" + key.upper(), plain)
    async with serve(create_app_from_env()) as client:
        status = await client.get("/v1/workbench/status")
        assert status.status_code == 200 and status.json()["llm_enabled"] is False
        assert status.json()["workbench_enabled"] is True
        created = await client.post(
            "/v1/workbench/projects",
            json={"name": "合成独立入口项目"},
            headers={"Idempotency-Key": "runtime-project-original"},
        )
        assert created.status_code == 201, created.text
        project = created.json()
        source = await client.post(
            f"/v1/workbench/projects/{project['id']}/transcripts",
            json={
                "display_name": "合成原稿",
                "source": {"kind": "manual", "text": " e\u0301😀\n "},
            },
            headers={"Idempotency-Key": "runtime-transcript"},
        )
        assert source.status_code == 201, source.text
        transcript = source.json()["transcript"]
        meeting = await client.post(
            f"/v1/workbench/projects/{project['id']}/meetings",
            json={"display_name": "合成会议", "transcript_id": transcript["id"]},
            headers={"Idempotency-Key": "runtime-meeting"},
        )
        assert meeting.status_code == 201, meeting.text
        path = f"/v1/workbench/meetings/{meeting.json()['id']}"
        sealed = await client.post(
            path + "/seal",
            json={"expected_transcript_revision": "1", "text": " 精确封存😀\n "},
            headers={"Idempotency-Key": "runtime-seal"},
        )
        assert sealed.status_code == 200 and sealed.json()["sealed_revision"] == "2"
        disabled = await client.post(
            path + "/minutes",
            json={"template_revision": "1"},
            headers={"Idempotency-Key": "runtime-minutes-disabled"},
        )
        assert disabled.status_code == 503 and disabled.json()["error"]["code"] == "llm_disabled"
    # A new factory and lifespan must recover authoritative rows and original receipts.
    async with serve(create_app_from_env()) as client:
        read = await client.get(f"/v1/workbench/projects/{project['id']}")
        replay = await client.post(
            "/v1/workbench/projects",
            json={"name": "合成独立入口项目"},
            headers={"Idempotency-Key": "runtime-project-original"},
        )
        assert read.status_code == 200 and read.json() == project
        assert replay.status_code == 201 and replay.json() == project
        assert (await client.get(path)).json()["sealed_revision"] == "2"
        plan = await client.post(
            "/v1/workbench/deletion-plans",
            json={
                "mode": "cascade",
                "target": {
                    "owner": "workbench",
                    "kind": "project",
                    "resource": {"id": project["id"], "revision": "1"},
                },
            },
            headers={"Idempotency-Key": "runtime-plan"},
        )
        assert plan.status_code == 200, plan.text
        deleted = await client.post(
            "/v1/workbench/deletions",
            json=acceptance(plan.json()),
            headers={"Idempotency-Key": "runtime-delete"},
        )
        assert deleted.status_code == 202, deleted.text
        assert (await client.get(f"/v1/workbench/projects/{project['id']}")).status_code == 404
        assert (await client.get(path)).status_code == 404


def runtime_env(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in database.settings().model_dump().items():
        if value is None:
            # An unset optional service stays unconfigured instead of becoming "None".
            continue
        plain = (
            value.get_secret_value()
            if hasattr(value, "get_secret_value")
            else json.dumps(value)
            if isinstance(value, tuple)
            else str(value)
        )
        monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_" + key.upper(), plain)


async def create_project(client: httpx.AsyncClient, key: str, name: str = "合成提词项目") -> str:
    created = await client.post(
        "/v1/workbench/projects", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    assert isinstance(identifier, str)
    return identifier


async def test_real_teleprompter_state_persists_replays_and_rejects_stale_scripts(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from speechnexus_backend.workbench.runtime import create_app_from_env

    runtime_env(database, monkeypatch)
    async with serve(create_app_from_env()) as client:
        project = await create_project(client, "runtime-prompter-project")
        path = f"/v1/workbench/projects/{project}/teleprompter"
        # A project without a saved position is not an empty position.
        assert (await client.get(path)).status_code == 404
        accepted = await client.put(
            path,
            json={"script_revision": "3", "visible_position": 120, "manual_hold": True},
            headers={"Idempotency-Key": "runtime-prompter-first"},
        )
        assert accepted.status_code == 200, accepted.text
        saved = accepted.json()
        assert saved["object"] == "nexus.teleprompter_state" and saved["project_id"] == project
        assert (saved["script_revision"], saved["visible_position"], saved["manual_hold"]) == (
            "3",
            120,
            True,
        )
        replay = await client.put(
            path,
            json={"script_revision": "3", "visible_position": 120, "manual_hold": True},
            headers={"Idempotency-Key": "runtime-prompter-first"},
        )
        assert replay.status_code == 200 and replay.json() == saved
        # A later unkeyed write moves the authoritative state, so a replay that returned the
        # current row instead of the accepted snapshot would no longer match `saved`.
        advanced = await client.put(
            path,
            json={"script_revision": "5", "visible_position": 7, "manual_hold": False},
        )
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["script_revision"] == "5"
        replayed_after_advance = await client.put(
            path,
            json={"script_revision": "3", "visible_position": 120, "manual_hold": True},
            headers={"Idempotency-Key": "runtime-prompter-first"},
        )
        assert replayed_after_advance.status_code == 200
        assert replayed_after_advance.json() == saved
        assert replayed_after_advance.json() != advanced.json()
        assert (await client.get(path)).json() == advanced.json()
        conflict = await client.put(
            path,
            json={"script_revision": "3", "visible_position": 5, "manual_hold": True},
            headers={"Idempotency-Key": "runtime-prompter-first"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        stale = await client.put(
            path,
            json={"script_revision": "2", "visible_position": 0, "manual_hold": False},
            headers={"Idempotency-Key": "runtime-prompter-stale"},
        )
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
        # An equal revision may move backwards because the operator seeks by hand.
        moved = await client.put(
            path,
            json={"script_revision": "5", "visible_position": 4, "manual_hold": False},
            headers={"Idempotency-Key": "runtime-prompter-move"},
        )
        assert moved.status_code == 200
        assert (moved.json()["visible_position"], moved.json()["manual_hold"]) == (4, False)
        newer = await client.put(
            path,
            json={"script_revision": "9", "visible_position": 0, "manual_hold": True},
            headers={"Idempotency-Key": "runtime-prompter-new"},
        )
        assert newer.status_code == 200 and newer.json()["script_revision"] == "9"
        assert (await client.get(path)).json() == newer.json()
    # A new factory and lifespan read the same authoritative state.
    async with serve(create_app_from_env()) as client:
        assert (await client.get(path)).json() == newer.json()
    # The runtime role may advance declared columns only; receipts and identity stay fixed.
    async with await psycopg.AsyncConnection.connect(
        database.settings().database_url.get_secret_value(), autocommit=True
    ) as runtime:
        for query in (
            sql.SQL("UPDATE {}.teleprompter_receipts SET intent_digest='forbidden'").format(
                sql.Identifier(database.schema)
            ),
            sql.SQL("UPDATE {}.teleprompter_states SET project_id='forbidden'").format(
                sql.Identifier(database.schema)
            ),
            sql.SQL("DELETE FROM {}.teleprompter_states").format(sql.Identifier(database.schema)),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await runtime.execute(query)


async def test_concurrent_teleprompter_updates_serialize_on_the_project_row(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from speechnexus_backend.workbench.runtime import create_app_from_env

    runtime_env(database, monkeypatch)
    async with serve(create_app_from_env()) as client:
        project = await create_project(client, "runtime-prompter-concurrent")
        path = f"/v1/workbench/projects/{project}/teleprompter"
        revisions = (11, 13, 12, 15, 14)
        responses = await asyncio.gather(
            *(
                client.put(
                    path,
                    json={
                        "script_revision": str(revision),
                        "visible_position": revision,
                        "manual_hold": False,
                    },
                    headers={"Idempotency-Key": f"runtime-prompter-concurrent-{revision}"},
                )
                for revision in revisions
            )
        )
        # Concurrent writes are either accepted or refused as stale, never interleaved: the
        # newest revision cannot be overwritten by a slower older one.
        assert {response.status_code for response in responses} <= {200, 409}
        assert [response.status_code for response in responses].count(200) >= 1
        for response in responses:
            if response.status_code == 409:
                assert response.json()["error"]["code"] == "revision_conflict"
        current = (await client.get(path)).json()
        assert (current["script_revision"], current["visible_position"]) == ("15", 15)


async def test_real_teleprompter_state_is_tombstoned_then_erased_with_its_project(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
        PostgresDeletionCleanup,
    )
    from speechnexus_backend.workbench.infrastructure.postgres_versions import (
        PostgresVersionUnitOfWork,
    )
    from speechnexus_backend.workbench.runtime import create_app_from_env

    runtime_env(database, monkeypatch)
    async with serve(create_app_from_env()) as client:
        project = await create_project(client, "runtime-prompter-delete")
        path = f"/v1/workbench/projects/{project}/teleprompter"
        saved = await client.put(
            path,
            json={"script_revision": "4", "visible_position": 40, "manual_hold": False},
            headers={"Idempotency-Key": "runtime-prompter-delete-state"},
        )
        assert saved.status_code == 200, saved.text
        plan = await client.post(
            "/v1/workbench/deletion-plans",
            json={
                "mode": "cascade",
                "target": {
                    "owner": "workbench",
                    "kind": "project",
                    "resource": {"id": project, "revision": "1"},
                },
            },
            headers={"Idempotency-Key": "runtime-prompter-plan"},
        )
        assert plan.status_code == 200, plan.text
        deleted = await client.post(
            "/v1/workbench/deletions",
            json=acceptance(plan.json()),
            headers={"Idempotency-Key": "runtime-prompter-delete"},
        )
        assert deleted.status_code == 202, deleted.text
        # Tombstoned immediately: neither read nor a late match can reach the position.
        assert (await client.get(path)).status_code == 404
        assert (
            await client.put(
                path,
                json={"script_revision": "4", "visible_position": 1, "manual_hold": False},
                headers={"Idempotency-Key": "runtime-prompter-late"},
            )
        ).status_code == 404
        deletion_id = deleted.json()["id"]
    # The cleanup worker erases content; the position and its receipts cascade with the
    # project row. This proves the fragment's FK action, not the worker's grant boundary.
    cleanup = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(database.owner_dsn, schema=database.schema), meetings=True
    )
    assert await cleanup.run(deletion_id) is not None
    async with await psycopg.AsyncConnection.connect(database.owner_dsn, autocommit=True) as owner:
        for table in ("teleprompter_states", "teleprompter_receipts"):
            counted = await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.{} WHERE project_id = %s").format(
                        sql.Identifier(database.schema), sql.Identifier(table)
                    ),
                    (project,),
                )
            ).fetchone()
            assert counted == (0,), table


async def test_refused_startup_releases_the_owned_http_and_object_handles(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from speechnexus_backend.workbench import runtime
    from speechnexus_backend.workbench.infrastructure.minio_object_zone import MinioObjectZone

    closed: list[str] = []
    real_aclose, real_close = httpx.AsyncClient.aclose, MinioObjectZone.close

    async def spy_aclose(client: httpx.AsyncClient) -> None:
        closed.append("httpx")
        await real_aclose(client)

    def spy_close(zone: MinioObjectZone) -> None:
        closed.append("minio")
        real_close(zone)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", spy_aclose)
    monkeypatch.setattr(MinioObjectZone, "close", spy_close)
    settings = WorkbenchSettings(
        database_url=database.settings().database_url,
        # A schema the startup check must refuse, so the lifespan raises before serving.
        database_schema="wb_missing_" + uuid4().hex,
        workspace_id="workspace-runtime",
        deployment_id="synthetic-runtime",
        cursor_key="synthetic-32-byte-cursor-key-test-only",
        speech_base_url="http://127.0.0.1:59999",
        object_endpoint="http://127.0.0.1:59000",
        object_bucket="synthetic-bucket",
        object_access_key="synthetic-access",
        object_secret_key="synthetic-object-secret",
    )
    app = runtime.create_runtime_app(settings)

    with pytest.raises(runtime.RuntimeStartupError):
        async with app.router.lifespan_context(app):
            pass
    # The refused start must not leak the two long-lived handles it already built.
    assert closed == ["httpx", "minio"]


async def test_dsn_role_switch_cannot_change_checked_or_business_identity(
    database: Database,
) -> None:
    from speechnexus_backend.workbench.runtime import create_runtime_app

    elevated = "wb_elevated_" + uuid4().hex
    role, owner_dsn = sql.Identifier(elevated), database.owner_dsn
    async with await psycopg.AsyncConnection.connect(owner_dsn, autocommit=True) as owner:
        await owner.execute(sql.SQL("CREATE ROLE {} BYPASSRLS").format(role))
        await owner.execute(sql.SQL("GRANT {} TO {}").format(role, sql.Identifier(database.role)))
    try:
        switched = WorkbenchSettings(
            database_url=make_conninfo(
                owner_dsn, user=database.role, options=f"-c role={elevated}"
            ),
            database_schema=database.schema,
            workspace_id="workspace-runtime",
            deployment_id="synthetic-runtime",
            cursor_key="synthetic-32-byte-cursor-key-test-only",
        )
        # The startup check must judge the login identity, and every business
        # connection must run as that same login identity instead of a privileged
        # role a DSN or PGOPTIONS switch could otherwise select.
        async with serve(create_runtime_app(switched)) as client:
            assert (await client.get("/v1/workbench/status")).status_code == 200
            created = await client.post(
                "/v1/workbench/projects",
                json={"name": "合成身份一致项目"},
                headers={"Idempotency-Key": "runtime-identity-parity"},
            )
            assert created.status_code == 201, created.text
    finally:
        async with await psycopg.AsyncConnection.connect(owner_dsn, autocommit=True) as owner:
            await owner.execute(
                sql.SQL("REVOKE {} FROM {}").format(role, sql.Identifier(database.role))
            )
            await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(role))


@pytest.mark.parametrize(
    "failure",
    [
        "owner",
        "schema_create",
        "owner_member",
        "bypassrls",
        "history_update",
        "delete",
        "stamp_write",
        "wrong_stamp",
        "missing_table",
        "missing_select",
        "missing_insert",
        "head_full_update",
        "missing_head_update",
        "missing_prompter_table",
        "prompter_full_update",
        "prompter_receipt_update",
        "missing_prompter_insert",
    ],
)
async def test_real_lifespan_refuses_dangerous_or_incomplete_database(
    database: Database, failure: str
) -> None:
    from speechnexus_backend.workbench.runtime import RuntimeStartupError, create_runtime_app

    s, r = sql.Identifier(database.schema), sql.Identifier(database.role)
    changes = {
        "schema_create": sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(s, r),
        "owner_member": sql.SQL("GRANT workbench_test_owner TO {}").format(r),
        "bypassrls": sql.SQL("ALTER ROLE {} BYPASSRLS").format(r),
        "history_update": sql.SQL("GRANT UPDATE ON {}.transcript_revisions TO {}").format(s, r),
        "delete": sql.SQL("GRANT DELETE ON {}.projects TO {}").format(s, r),
        "stamp_write": sql.SQL("GRANT UPDATE ON {}.runtime_schema_revision TO {}").format(s, r),
        "wrong_stamp": sql.SQL("UPDATE {}.runtime_schema_revision SET revision={}").format(
            s, sql.Literal(SCHEMA_REVISION + 1)
        ),
        "missing_table": sql.SQL("DROP TABLE {}.meetings CASCADE").format(s),
        "missing_select": sql.SQL("REVOKE SELECT ON {}.transcripts FROM {}").format(s, r),
        "missing_insert": sql.SQL("REVOKE INSERT ON {}.projects FROM {}").format(s, r),
        "head_full_update": sql.SQL("GRANT UPDATE ON {}.projects TO {}").format(s, r),
        "missing_head_update": sql.SQL(
            "REVOKE UPDATE(metadata_revision) ON {}.projects FROM {}"
        ).format(s, r),
        "missing_prompter_table": sql.SQL("DROP TABLE {}.teleprompter_states CASCADE").format(s),
        "prompter_full_update": sql.SQL("GRANT UPDATE ON {}.teleprompter_states TO {}").format(
            s, r
        ),
        "prompter_receipt_update": sql.SQL("GRANT UPDATE ON {}.teleprompter_receipts TO {}").format(
            s, r
        ),
        "missing_prompter_insert": sql.SQL(
            "REVOKE INSERT ON {}.teleprompter_receipts FROM {}"
        ).format(s, r),
    }
    if failure in changes:
        async with await psycopg.AsyncConnection.connect(
            database.owner_dsn, autocommit=True
        ) as owner:
            await owner.execute(changes[failure])
    app = create_runtime_app(database.settings(owner=failure == "owner"))
    with pytest.raises(RuntimeStartupError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("invalid runtime database accepted")
    assert str(caught.value) == "Workbench runtime database is unavailable or unsafe."
    assert database.schema not in str(caught.value)
