"""Real PG workspace writes: one row per identity, one ledger row per key.

These tests prove what an in-memory double cannot: the unique constraints, the
transaction boundary, the compare-and-set on the metadata revision and the
same-key replay all survive a real server. ``SPEECHNEXUS_TEST_DATABASE_URL`` is
required; a missing DSN fails instead of skipping, because an unexecuted write
is not write evidence.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from speechnexus_contracts.admin_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.domain.assets import hash_bytes
from speechnexus_backend.speech.domain.workspaces import (
    WorkspaceConflictError,
    WorkspaceCreateCommand,
    WorkspaceNotFoundError,
    WorkspaceStatus,
    WorkspaceUpdateCommand,
    WorkspaceWriteReceipt,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.workspace_schema import workspace_writer_grants
from speechnexus_backend.speech.infrastructure.workspaces_postgres import (
    PostgresWorkspaceReads,
    PostgresWorkspaceWrites,
)
from speechnexus_backend.workspace_writer_runtime import WorkspaceWriterRuntime

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/v1/nexus/workspaces"
PASSWORD = "synthetic_workspace_test"


@dataclass
class Database:
    url: str = field(repr=False)
    schema: str
    role: str
    admin: Any = field(repr=False)
    writes: PostgresWorkspaceWrites = field(repr=False)

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return cast(list[dict[str, Any]], list(cursor.fetchall())) if cursor.description else []

    def count(self, table: str, where: str = "true") -> int:
        return int(
            self.query(f'SELECT count(*) AS n FROM "{self.schema}".{table} WHERE {where}')[0]["n"]
        )

    def workspaces(self) -> int:
        """Rows this suite created; the migration also seeds ``workspace-default``."""
        return self.count("speech_workspaces", "workspace_id <> 'workspace-default'")

    def writer_url(self) -> str:
        return make_conninfo(self.url, user=self.role, password=PASSWORD)


def create(
    display_name: str, *, key: str | None, request_id: str = "synthetic-request"
) -> WorkspaceCreateCommand:
    return WorkspaceCreateCommand(
        display_name=display_name,
        request_id=request_id,
        key_digest=hash_bytes(key.encode("utf-8")) if key is not None else None,
        intent_digest=hash_bytes(('{"display_name":"' + display_name + '"').encode("utf-8")),
    )


def update(
    workspace_id: str,
    expected: int,
    *,
    key: str | None,
    display_name: str | None = None,
    status: WorkspaceStatus | None = None,
) -> WorkspaceUpdateCommand:
    return WorkspaceUpdateCommand(
        workspace_id=workspace_id,
        expected_metadata_revision=expected,
        request_id="synthetic-request",
        key_digest=hash_bytes(key.encode("utf-8")) if key is not None else None,
        intent_digest=hash_bytes(f"{workspace_id}:{expected}:{display_name}:{status}".encode()),
        display_name=display_name,
        status=status,
    )


def upgrade(url: str, schema: str, revision: str) -> None:
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            if revision == "head":
                command.upgrade(config, revision)
            else:
                command.downgrade(config, revision)
    finally:
        engine.dispose()


@pytest.fixture()
def database() -> Iterator[Database]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not write evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_workspace_" + suffix, "sn_workspace_role_" + suffix
    upgrade(url, schema, "head")
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in workspace_writer_grants(schema, role):
        admin.execute(grant)
    pool = speech_pool(make_conninfo(url, user=role, password=PASSWORD), schema=schema, max_size=4)
    pool.open()
    try:
        yield Database(url, schema, role, admin, PostgresWorkspaceWrites(pool))
    finally:
        pool.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.execute(f'DROP ROLE "{role}"')
        admin.close()


def test_create_then_same_key_replays_one_identity_and_one_ledger_row(
    database: Database,
) -> None:
    command = create("共享分组", key="workspace-create-key-1")
    first = database.writes.create(command, workspace_id="workspace-created-1")
    assert first.replayed is False
    assert first.workspace.workspace_id == "workspace-created-1"
    assert first.workspace.metadata_revision == 1
    second = database.writes.create(command, workspace_id="workspace-created-2")
    assert second.replayed is True
    assert second.workspace == first.workspace
    assert database.workspaces() == 1
    assert database.count("speech_workspace_commands") == 1
    assert database.query("SELECT operation, workspace_id FROM speech_workspace_commands") == [
        {"operation": "create", "workspace_id": "workspace-created-1"}
    ]


def test_same_key_with_another_body_is_a_conflict_and_writes_nothing(
    database: Database,
) -> None:
    database.writes.create(
        create("First", key="workspace-create-key-2"), workspace_id="workspace-a"
    )
    with pytest.raises(WorkspaceConflictError) as error:
        database.writes.create(
            create("Second", key="workspace-create-key-2"), workspace_id="workspace-b"
        )
    assert error.value.code == "idempotency_conflict"
    assert database.workspaces() == 1
    assert database.count("speech_workspace_commands") == 1


def test_two_concurrent_same_key_requests_serialise_to_one_identity(
    database: Database,
) -> None:
    command = create("Concurrent", key="workspace-create-key-3")

    def submit(index: int) -> WorkspaceWriteReceipt:
        return database.writes.create(command, workspace_id=f"workspace-race-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = [future.result() for future in [pool.submit(submit, 0), pool.submit(submit, 1)]]
    assert len({receipt.workspace.workspace_id for receipt in receipts}) == 1
    assert sorted(receipt.replayed for receipt in receipts) == [False, True]
    assert database.workspaces() == 1
    assert database.count("speech_workspace_commands") == 1


def test_identity_collision_is_reported_without_a_second_row(database: Database) -> None:
    database.writes.create(create("Original", key=None), workspace_id="workspace-taken")
    with pytest.raises(WorkspaceConflictError) as error:
        database.writes.create(create("Other", key=None), workspace_id="workspace-taken")
    assert error.value.code == "workspace_identity_conflict"
    with pytest.raises(WorkspaceConflictError) as keyed:
        database.writes.create(
            create("Other", key="workspace-create-key-4"), workspace_id="workspace-taken"
        )
    assert keyed.value.code == "workspace_identity_conflict"
    assert database.workspaces() == 1
    assert database.count("speech_workspace_commands") == 0


def test_update_compare_and_set_advances_once_even_when_replayed(database: Database) -> None:
    database.writes.create(create("Original", key=None), workspace_id="workspace-edit")
    command = update("workspace-edit", 1, key="workspace-update-key-1", display_name="Renamed")
    first = database.writes.update(command)
    assert first.replayed is False
    assert (first.workspace.display_name, first.workspace.metadata_revision) == ("Renamed", 2)
    second = database.writes.update(command)
    assert second.replayed is True and second.workspace == first.workspace
    stored = database.query(
        f'SELECT metadata_revision FROM "{database.schema}".speech_workspaces '
        "WHERE workspace_id = 'workspace-edit'"
    )[0]["metadata_revision"]
    assert stored == 2
    assert database.count("speech_workspace_commands") == 1
    with pytest.raises(WorkspaceConflictError) as stale:
        database.writes.update(update("workspace-edit", 1, key=None, status="disabled"))
    assert stale.value.code == "configuration_changed"
    disabled = database.writes.update(update("workspace-edit", 2, key=None, status="disabled"))
    assert disabled.workspace.status == "disabled" and disabled.workspace.metadata_revision == 3


def test_update_of_a_missing_target_or_with_another_keys_receipt_is_refused(
    database: Database,
) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        database.writes.update(update("workspace-none", 1, key=None, status="disabled"))
    database.writes.create(
        create("Original", key="workspace-create-key-5"), workspace_id="workspace-x"
    )
    database.writes.create(create("Second", key=None), workspace_id="workspace-y")
    with pytest.raises(WorkspaceConflictError) as error:
        database.writes.update(
            update("workspace-y", 1, key="workspace-create-key-5", status="disabled")
        )
    assert error.value.code == "idempotency_conflict"
    assert database.count("speech_workspace_commands") == 1


async def test_http_returns_201_twice_for_one_key_and_persists_one_row(
    database: Database,
) -> None:
    settings = Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint="http://127.0.0.1:9000",
        object_storage_healthcheck_url="http://127.0.0.1:9000/health",
        object_storage_bucket="test",
        database_schema=database.schema,
        asset_allowed_hosts=("test",),
    )
    pool = speech_pool(database.writer_url(), schema=database.schema)
    pool.open()
    try:
        service = WorkspaceService(PostgresWorkspaceReads(pool), PostgresWorkspaceWrites(pool))
        app = create_app(settings=settings, workspaces=service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Idempotency-Key": "workspace-http-key-1"}
            first = await client.post(PREFIX, json={"display_name": "共享分组"}, headers=headers)
            second = await client.post(PREFIX, json={"display_name": "共享分组"}, headers=headers)
            assert first.status_code == 201 and second.status_code == 201
            validate_response("createWorkspace", 201, {"body": first.json()})
            assert first.json() == second.json()
            identifier = first.json()["id"]
            assert first.json()["metadata_revision"] == 1
            assert database.workspaces() == 1
            assert database.count("speech_workspace_commands") == 1
            patched = await client.patch(
                f"{PREFIX}/{identifier}",
                json={"expected_metadata_revision": 1, "status": "disabled"},
                headers={"Idempotency-Key": "workspace-http-key-2"},
            )
            assert patched.status_code == 200
            validate_response("updateWorkspace", 200, {"body": patched.json()})
            assert patched.json()["status"] == "disabled"
            assert patched.json()["metadata_revision"] == 2
            read = await client.get(f"{PREFIX}/{identifier}")
            assert read.status_code == 200 and read.json() == patched.json()
    finally:
        pool.close()


def test_writer_runtime_accepts_only_the_declared_column_surface(database: Database) -> None:
    settings = Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint="http://127.0.0.1:9000",
        object_storage_healthcheck_url="http://127.0.0.1:9000/health",
        object_storage_bucket="test",
        database_schema=database.schema,
        workspace_writer_database_url=SecretStr(database.writer_url()),
    )
    runtime = WorkspaceWriterRuntime(settings)
    runtime.open()
    runtime.close()
    database.admin.execute(
        f'GRANT UPDATE (policy_revision) ON "{database.schema}".speech_workspaces '
        f'TO "{database.role}"'
    )
    try:
        widened = WorkspaceWriterRuntime(settings)
        with pytest.raises(ValueError):
            widened.open()
    finally:
        database.admin.execute(
            f'REVOKE UPDATE (policy_revision) ON "{database.schema}".speech_workspaces '
            f'FROM "{database.role}"'
        )


def test_migration_rolls_back_and_replays_the_ledger(database: Database) -> None:
    upgrade(database.url, database.schema, "0048_voice_evidence_scan_index")
    tables = database.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (database.schema,),
    )
    assert "speech_workspace_commands" not in {row["table_name"] for row in tables}
    upgrade(database.url, database.schema, "head")
    assert database.count("speech_workspace_commands") == 0
    assert database.count("speech_workspaces", "workspace_id = 'workspace-default'") == 1
