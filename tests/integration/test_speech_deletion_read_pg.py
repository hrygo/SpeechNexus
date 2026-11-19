"""Real PostgreSQL deletion read: the ledger is the answer, and only the ledger.

The write side is slice B; this module pins the read surface:

* a 200 body is exactly the 202 body the execution committed, byte for byte, and
  a second app built over a fresh pool and ledger service returns the same bytes
  (the read depends on PostgreSQL alone; a real second OS process was checked by
  hand and is not reproduced here);
* an unknown id, an id that is not a deletion id and another workspace's real
  deletion all answer the same declared 404, so existence cannot be inferred;
* a queued row keeps ``cleanup_state=pending`` whether or not its object purge is
  claimed, and the read never adds or changes a claim: the frozen ``Deletion``
  shape has no field for it;
* a GET writes nothing: the ledger row, the content-deletion row and the object
  purge count are unchanged afterwards;
* the provisioned writer role can read, and a reader granted only
  ``SELECT speech_deletion_ledger`` can read too. A reader without that SELECT
  gets the configured-gap 503, not an empty 200.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from tests.contract.test_architecture_repairs import _deletion_validator
from tests.contract.test_speech_http_statuses import assert_error_contract
from tests.integration.test_speech_deletion_execute_pg import (
    ASSET,
    ROLE_PASSWORD,
    WORKSPACE,
    Sandbox,
    _admin,
    _app,
    _confirm,
    _execute,
    _ledger,
    _plan,
    _preview,
    _provision_writer,
    _read,
)
from tests.integration.test_speech_deletion_execute_pg import (
    sandbox as sandbox,
)
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.job_runtime import _verify_privileges
from speechnexus_backend.speech.application.deletions import SpeechDeletions
from speechnexus_backend.speech.domain.deletion import (
    DeletionPlan,
    DeletionRequest,
    confirmed_targets,
    deletion_intent_digest,
    idempotency_key_digest,
    plan_digest,
)
from speechnexus_backend.speech.infrastructure.deletion_executions_postgres import (
    PostgresDeletionExecutions,
)
from speechnexus_backend.speech.infrastructure.deletion_plans_postgres import (
    PostgresDeletionPlans,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

pytestmark = pytest.mark.integration

OTHER_WORKSPACE = "workspace-other-read"
OTHER_DELETION = "dl-" + "b" * 32
OTHER_PLAN = "dp-" + "b" * 32
UNKNOWN = "dl-" + "e" * 32
READ_REQUEST_ID = "deletion-read-pg"


async def _get(
    application: Any, deletion_id: str, *, request_id: str | None = None
) -> httpx.Response:
    headers = {} if request_id is None else {"X-Request-ID": request_id}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as http:
        return await http.get(f"/v1/nexus/deletions/{deletion_id}", headers=headers)


def _row_snapshot(sandbox: Sandbox, deletion_id: str) -> list[dict[str, Any]]:
    """The columns a read must not touch."""
    return _read(
        sandbox,
        "SELECT id, status, cleanup_state, purge_claimed_at, created_at, updated_at, "
        "completed_at FROM speech_deletion_ledger WHERE id = %s",
        (deletion_id,),
    )


def _seed_other_workspace(sandbox: Sandbox) -> None:
    """A real succeeded deletion in a workspace this deployment does not serve."""
    admin = _admin(sandbox.url)
    try:
        admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        admin.execute(
            "INSERT INTO speech_workspaces "
            "(workspace_id, display_name, workspace_state, created_at_us) "
            "VALUES (%s, 'Other workspace', 'active', 0)",
            (OTHER_WORKSPACE,),
        )
        admin.execute(
            """INSERT INTO speech_deletion_plans
                (id, workspace_id, target_owner, target_kind, target_id, target_revision,
                 target_digest, mode, plan_revision, plan_digest, idempotency_key_digest,
                 intent_digest, created_at, expires_at, dependencies, content_snapshot)
            VALUES (%s, %s, 'speech', 'operation_content', 'job-other-read', '2',
                    %s, 'ordinary', '1', %s, %s, %s,
                    NOW(), NOW() + interval '300 seconds', '[]'::jsonb, '{}'::jsonb)""",
            (
                OTHER_PLAN,
                OTHER_WORKSPACE,
                "sha256:" + "c" * 64,
                "sha256:" + "c" * 64,
                "c" * 64,
                "d" * 64,
            ),
        )
        admin.execute(
            """INSERT INTO speech_deletion_ledger
                (id, workspace_id, plan_id, plan_revision, plan_digest, target_owner,
                 target_kind, target_id, target_revision, idempotency_key_digest,
                 intent_digest, deleted, retained, status, cleanup_state,
                 content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at)
            VALUES (%s, %s, %s, '1', %s, 'speech', 'operation_content',
                    'job-other-read', '2', %s, %s,
                    '[["speech","operation_content","job-other-read","2"]]'::jsonb,
                    '[]'::jsonb, 'succeeded', 'complete', NULL, NULL,
                    NOW(), NOW(), NOW())""",
            (
                OTHER_DELETION,
                OTHER_WORKSPACE,
                OTHER_PLAN,
                "sha256:" + "c" * 64,
                "c" * 64,
                "d" * 64,
            ),
        )
    finally:
        admin.close()


def _provision_reader(sandbox: Sandbox, *, ledger_select: bool) -> tuple[str, Any]:
    """A least-privilege reader: schema USAGE plus, optionally, the ledger SELECT."""
    role = "sn_delread_" + uuid.uuid4().hex[:10]
    admin = _admin(sandbox.url)
    admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(ROLE_PASSWORD)
        )
    )
    admin.execute(f'GRANT USAGE ON SCHEMA "{sandbox.schema}" TO "{role}"')
    if ledger_select:
        admin.execute(f'GRANT SELECT ON "{sandbox.schema}".speech_deletion_ledger TO "{role}"')
    return role, admin


def _drop_role(admin: Any, role: str) -> None:
    admin.execute(f'DROP OWNED BY "{role}"')
    admin.execute(f'DROP ROLE IF EXISTS "{role}"')
    admin.close()


def _reader_app(sandbox: Sandbox, role: str) -> tuple[Any, Any]:
    pool = speech_pool(
        make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD),
        schema=sandbox.schema,
    )
    pool.open()
    service = SpeechDeletions(
        PostgresDeletionPlans(pool),
        workspace_id=WORKSPACE,
        executions=PostgresDeletionExecutions(pool),
    )
    return create_app(settings=_settings(), deletions=service), pool


def _confirmation(plan: DeletionPlan) -> DeletionRequest:
    return DeletionRequest(plan.id, plan.revision, plan_digest(plan), confirmed_targets(plan))


async def test_get_projects_the_ledger_after_a_real_execution(sandbox: Sandbox) -> None:
    """The read answers what the execution committed, and a GET writes nothing."""
    application, pool, objects, _ = _app(sandbox)
    try:
        plan = await _preview(application, "asset", ASSET, 3, "key-read-plan-exec")
        executed = await _execute(application, _confirm(plan), "key-read-exec")
        assert executed.status_code == 202, executed.text
        payload = executed.json()
        _deletion_validator("Deletion").validate(payload)
        deletion_id = payload["id"]
        before = _row_snapshot(sandbox, deletion_id)

        got = await _get(application, deletion_id)
        assert got.status_code == 200, got.text
        _deletion_validator("Deletion").validate(got.json())
        # Exactly what the 202 said, down to the bytes: both are projections of
        # the same committed row and the serializer is deterministic.
        assert got.json() == payload
        assert got.content == executed.content
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["x-request-id"]

        # No side effects: the row, the content ledger and the object purges are
        # untouched by the read.
        assert _row_snapshot(sandbox, deletion_id) == before
        assert len(_ledger(sandbox, "key-read-exec")) == 1
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_content_deletions") == [{"n": 1}]
        assert objects.versions == ["minio-asset-exec-1"]
    finally:
        pool.close()


async def test_a_fresh_pool_and_service_return_the_same_bytes(sandbox: Sandbox) -> None:
    """A fresh pool and ledger service answer the same id from PostgreSQL alone.

    Same interpreter, new pool, new service, new app: nothing but PostgreSQL
    carries the row. A genuine second OS process was verified manually.
    """
    first_app, first_pool, _, _ = _app(sandbox)
    plan = await _preview(first_app, "asset", ASSET, 3, "key-read-restart-plan")
    executed = await _execute(first_app, _confirm(plan), "key-read-restart-exec")
    assert executed.status_code == 202, executed.text
    deletion_id = executed.json()["id"]
    first = await _get(first_app, deletion_id)
    assert first.status_code == 200, first.text
    first_pool.close()

    second_app, second_pool, objects, _ = _app(sandbox)
    try:
        second = await _get(second_app, deletion_id)
        assert second.status_code == 200, second.text
        assert second.content == first.content
        # A restart does not replay the purge or add a ledger row.
        assert objects.versions == []
        assert len(_ledger(sandbox, "key-read-restart-exec")) == 1
    finally:
        second_pool.close()


async def test_an_unknown_deletion_is_the_declared_404(sandbox: Sandbox) -> None:
    application, pool, _, _ = _app(sandbox)
    try:
        response = await _get(application, UNKNOWN, request_id=READ_REQUEST_ID)
        assert response.status_code == 404, response.text
        error = response.json()["error"]
        assert error["code"] == "resource_not_found"
        assert error["type"] == "not_found_error"
        assert error["retryable"] is False
        assert error["param"] is None
        assert error["request_id"] == READ_REQUEST_ID
        assert response.headers["cache-control"] == "no-store"
        assert_error_contract("speechGetDeletion", response)

        # A real speech resource id is not a deletion id, and is answered the same.
        other = await _get(application, ASSET)
        assert other.status_code == 404
        assert other.json()["error"]["code"] == "resource_not_found"
    finally:
        pool.close()


async def test_another_workspaces_deletion_is_not_found_without_leaking(
    sandbox: Sandbox,
) -> None:
    """A real row outside this workspace is answered exactly like an absent one."""
    _seed_other_workspace(sandbox)
    assert _read(
        sandbox,
        "SELECT workspace_id, status FROM speech_deletion_ledger WHERE id = %s",
        (OTHER_DELETION,),
    ) == [{"workspace_id": OTHER_WORKSPACE, "status": "succeeded"}]

    application, pool, _, _ = _app(sandbox)
    try:
        cross_workspace = await _get(application, OTHER_DELETION, request_id=READ_REQUEST_ID)
        absent = await _get(application, UNKNOWN, request_id=READ_REQUEST_ID)
        assert cross_workspace.status_code == absent.status_code == 404
        assert cross_workspace.json()["error"]["code"] == "resource_not_found"
        # Same request id, same bytes: the answer cannot distinguish "exists in
        # another workspace" from "does not exist" (a 403 or a different message
        # would leak existence).
        assert cross_workspace.content == absent.content
    finally:
        pool.close()


async def test_a_claimed_but_unfinished_row_reports_the_ledger_cleanup_state(
    sandbox: Sandbox,
) -> None:
    """The claim is invisible by contract, and a read does not take or drop it.

    Slice B's live claim is exactly a queued row with ``purge_claimed_at`` set. The
    frozen ``Deletion`` has no field for it, so both a claimed and a released row
    project ``queued``/``pending``; only ``cleanup_state`` reports progress, and it
    must equal the ledger. Adding a field is a contract change, not this slice.
    """
    application, pool, _, _ = _app(sandbox)
    try:
        plan = _plan(pool, "asset", ASSET, 3, "key-read-claim-plan")
        executions = PostgresDeletionExecutions(pool)
        key = "key-read-claim-exec"
        request = _confirmation(plan)
        accepted = executions.accept(
            request,
            key_digest=idempotency_key_digest(key),
            intent_digest=deletion_intent_digest(request),
            purge_available=True,
            artifact_purge_available=True,
            request_id="read-claim",
            workspace_id=WORKSPACE,
        )
        # The claim is committed and the object is deliberately left unpurged.
        assert accepted.pending_object is not None
        deletion_id = accepted.record.id
        claimed_row = _ledger(sandbox, key)
        assert len(claimed_row) == 1
        assert claimed_row[0]["status"] == "queued"
        assert claimed_row[0]["cleanup_state"] == "pending"
        assert claimed_row[0]["purge_claimed_at"] is not None
        before = _row_snapshot(sandbox, deletion_id)

        claimed = await _get(application, deletion_id)
        assert claimed.status_code == 200, claimed.text
        body = claimed.json()
        _deletion_validator("Deletion").validate(body)
        assert body["status"] == claimed_row[0]["status"]
        assert body["cleanup_state"] == claimed_row[0]["cleanup_state"] == "pending"
        assert body["result"] is None
        assert body["error"] is None
        assert not any("claim" in field or "purge" in field for field in body)
        # The read did not claim, release, complete or purge anything.
        assert _row_snapshot(sandbox, deletion_id) == before

        executions.release_claim(
            deletion_id, key_digest=idempotency_key_digest(key), workspace_id=WORKSPACE
        )
        released_row = _ledger(sandbox, key)
        assert released_row[0]["purge_claimed_at"] is None
        released_before = _row_snapshot(sandbox, deletion_id)

        released = await _get(application, deletion_id)
        assert released.status_code == 200, released.text
        released_body = released.json()
        assert set(released_body) == set(body)
        assert released_body["status"] == released_row[0]["status"] == "queued"
        assert released_body["cleanup_state"] == released_row[0]["cleanup_state"] == "pending"
        # Dropping the claim only moves the row's update timestamp; the read
        # surface presents no claim difference because the contract has none.
        changed = {field for field in body if body[field] != released_body[field]}
        assert changed <= {"updated_at"}
        assert _row_snapshot(sandbox, deletion_id) == released_before
    finally:
        pool.close()


async def test_the_provisioned_writer_role_can_read_the_deletion(sandbox: Sandbox) -> None:
    """The identity the deployment uses reads its own committed deletion."""
    role, admin = _provision_writer(sandbox, include_execution=True)
    pool: Any = None
    try:
        with psycopg.connect(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD),
            row_factory=dict_row,
        ) as connection:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            _verify_privileges(connection, sandbox.schema)
        pool = speech_pool(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD),
            schema=sandbox.schema,
        )
        pool.open()
        application, _, _, _ = _app(sandbox, pool=pool)
        plan = await _preview(application, "asset", ASSET, 3, "key-read-role-plan")
        executed = await _execute(application, _confirm(plan), "key-read-role-exec")
        assert executed.status_code == 202, executed.text
        got = await _get(application, executed.json()["id"])
        assert got.status_code == 200, got.text
        assert got.json() == executed.json()
    finally:
        if pool is not None:
            pool.close()
        _drop_role(admin, role)


async def test_a_reader_with_only_the_ledger_select_serves_the_read(
    sandbox: Sandbox,
) -> None:
    """The read does not need the executor's write columns or any other table."""
    writer_app, writer_pool, _, _ = _app(sandbox)
    try:
        plan = await _preview(writer_app, "asset", ASSET, 3, "key-read-reader-plan")
        executed = await _execute(writer_app, _confirm(plan), "key-read-reader-exec")
        assert executed.status_code == 202, executed.text
        expected = executed.json()
    finally:
        writer_pool.close()

    role, admin = _provision_reader(sandbox, ledger_select=True)
    reader_pool = None
    try:
        application, reader_pool = _reader_app(sandbox, role)
        got = await _get(application, expected["id"])
        assert got.status_code == 200, got.text
        assert got.json() == expected
        # The grant probe passed; a missing row is still the public 404.
        missing = await _get(application, UNKNOWN)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "resource_not_found"
    finally:
        if reader_pool is not None:
            reader_pool.close()
        _drop_role(admin, role)


async def test_a_reader_without_the_ledger_select_is_not_configured(
    sandbox: Sandbox,
) -> None:
    """A missing SELECT is a named deployment gap, never an empty successful read."""
    role, admin = _provision_reader(sandbox, ledger_select=False)
    reader_pool = None
    try:
        application, reader_pool = _reader_app(sandbox, role)
        response = await _get(application, UNKNOWN)
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "deletion_not_configured"
        assert error["retryable"] is False
        # The provisioning unit is the execution grant group; the ledger SELECT
        # is part of it, so the public message names that group.
        assert "deletion_execution_grants" in error["message"]
    finally:
        if reader_pool is not None:
            reader_pool.close()
        _drop_role(admin, role)
