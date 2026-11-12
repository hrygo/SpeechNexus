"""Real isolated PostgreSQL proof for the administrative audit event listing.

The projection reads the append-only ``speech_publication_lifecycle_events``
table through the dedicated read-only identity. These cases exercise the
assembled HTTP route against a real migrated schema, so the response is the
contract-validated shape rather than a hand-built dictionary, and they cover the
empty history, deterministic composite keyset boundaries across equal
timestamps, the exact action filter, cursor scope, an untrusted stored summary
and the read-only grant set.

Events are written by the migration administrator because this slice only reads
them; the lifecycle writer's own transaction coverage lives in
``test_binding_lifecycle_pg.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.audit_reader_runtime import AuditReaderRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.audit_events import AuditEventService
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.audit_events import AuditEventPosition
from speechnexus_backend.speech.infrastructure.audit_events_postgres import (
    PostgresAuditEventReads,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    audit_reader_grants,
    binding_lifecycle_grants,
)
from speechnexus_backend.speech.interfaces.http.audit_events import cursor_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_audit_reader"
URL = "/v1/nexus/audit-events"
EVENT_TABLE = "speech_publication_lifecycle_events"
ROLES = ("lifecycle", "audit")


def event_id(value: int) -> str:
    return "lifecycle-" + f"{value:032x}"


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, Any]]
    schema: str
    urls: dict[str, str]
    pools: dict[str, SpeechPool]
    service: AuditEventService

    def grant(self, role: str, statements: tuple[str, ...]) -> None:
        for statement in statements:
            self.admin.execute(statement)


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not projection evidence"
        )
    suffix = uuid.uuid4().hex[:10]
    schema = "sn_audit_" + suffix
    names = {role: f"sn_audit_{role}_{suffix}" for role in ROLES}
    engine = create_engine(
        base.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        base, row_factory=dict_row, autocommit=True
    )
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    for role in ROLES:
        admin.execute(f"CREATE ROLE \"{names[role]}\" LOGIN PASSWORD '{PASSWORD}'")
    for statement in binding_lifecycle_grants(schema, names["lifecycle"]):
        admin.execute(statement)
    # The declared read-only provisioning function is the subject under test:
    # the audit reader identity comes from it, not from a hand-written grant.
    for statement in audit_reader_grants(schema, names["audit"]):
        admin.execute(statement)
    urls = {role: make_conninfo(base, user=names[role], password=PASSWORD) for role in ROLES}
    pools = {
        role: speech_pool(urls[role], schema=schema, max_size=2, name=f"audit-{role}-" + suffix)
        for role in ROLES
    }
    for pool in pools.values():
        pool.open()
    try:
        yield Sandbox(
            admin=admin,
            schema=schema,
            urls=urls,
            pools=pools,
            service=AuditEventService(PostgresAuditEventReads(pools["audit"])),
        )
    finally:
        for pool in pools.values():
            pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in ROLES:
            admin.execute(f'DROP OWNED BY "{names[role]}"')
            admin.execute(f'DROP ROLE IF EXISTS "{names[role]}"')
        admin.close()


def settings_for(sandbox: Sandbox, *, reader_url: str | None = None) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "process_role": "api",
            "database_schema": sandbox.schema,
            "audit_reader_database_url": (
                sandbox.urls["audit"] if reader_url is None else reader_url
            ),
            "asset_allowed_hosts": ("test",),
            "audit_read_concurrency": 1,
        }
    )


@pytest.fixture
async def http(sandbox: Sandbox) -> Any:
    app = create_app(
        settings=settings_for(sandbox),
        audit_events=sandbox.service,
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield sandbox, client


def publish(sandbox: Sandbox, binding_id: str, published_at_us: int) -> None:
    """Insert the publication row the event foreign key requires."""
    sandbox.admin.execute(
        f'INSERT INTO "{sandbox.schema}".speech_publications '
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding', %s, '1', %s, %s)",
        (binding_id, f"binding-{binding_id}-1", published_at_us),
    )


def record(
    sandbox: Sandbox,
    *,
    number: int,
    binding_id: str,
    occurred_at_us: int,
    action: str = "model_binding.retire",
    actor_type: str = "anonymous",
    credential_label: str | None = None,
    request_id: str = "request-audit-1",
    policy_revision: int = 7,
    change_summary: dict[str, Any] | None = None,
) -> str:
    identity = event_id(number)
    sandbox.admin.execute(
        f'INSERT INTO "{sandbox.schema}".{EVENT_TABLE} '
        "(event_id, action, binding_id, revision, revision_id, result_status, retired_at_us, "
        " request_id, actor_type, credential_label, occurred_at_us, policy_revision, "
        " intent_digest, change_summary_json) "
        "VALUES (%s, %s, %s, '1', %s, 'retired', %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            identity,
            action,
            binding_id,
            f"binding-{binding_id}-1",
            occurred_at_us,
            request_id,
            actor_type,
            credential_label,
            occurred_at_us,
            policy_revision,
            "sha256:" + "1" * 64,
            Jsonb(
                change_summary
                if change_summary is not None
                else {"changed_fields": ["/publication/status"], "reason": None}
            ),
        ),
    )
    return identity


async def get(client: AsyncClient, **parameters: Any) -> httpx.Response:
    return await client.get(URL, params=parameters, headers={"X-Request-ID": "request-audit"})


async def test_empty_history_returns_an_empty_page(http: Any) -> None:
    _, client = http
    response = await get(client)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "request-audit"
    assert response.json() == {
        "object": "nexus.list",
        "data": [],
        "has_more": False,
        "next_cursor": None,
    }


async def test_one_event_is_projected_field_by_field(http: Any) -> None:
    sandbox, client = http
    publish(sandbox, "audit-alpha", 1_700_000_000_000_000)
    identity = record(
        sandbox,
        number=1,
        binding_id="audit-alpha",
        occurred_at_us=1_700_000_000_000_001,
        action="model_binding.retire",
        credential_label="shared-reader",
        request_id="request-projection",
        policy_revision=9,
        change_summary={
            "changed_fields": ["/publication/status", "/publication/retired_at"],
            "reason": "replace the experimental revision",
        },
    )
    response = await get(client)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "object": "nexus.list",
        "data": [
            {
                "id": identity,
                "object": "nexus.audit_event",
                "actor_type": "anonymous",
                "credential_label": "shared-reader",
                "request_id": "request-projection",
                "action": "model_binding.retire",
                "resource_type": "model_binding",
                "resource_id": "audit-alpha",
                "occurred_at": "2023-11-14T22:13:20.000001Z",
                "policy_revision": "9",
                "change_summary": {
                    "changed_fields": ["/publication/status", "/publication/retired_at"],
                    "reason": "replace the experimental revision",
                },
            }
        ],
        "has_more": False,
        "next_cursor": None,
    }


async def test_equal_timestamps_page_by_the_unique_composite_key(http: Any) -> None:
    sandbox, client = http
    # Insertion order is b, a, c and every row shares the exact timestamp, so a
    # page ordered by time alone could only inherit the scan order.
    for number in (2, 1, 3):
        binding = f"audit-tie-{number}"
        publish(sandbox, binding, 1_700_000_000_000_000)
        record(
            sandbox,
            number=number,
            binding_id=binding,
            occurred_at_us=1_700_000_000_000_001,
        )

    first = await get(client, limit=2)
    assert first.status_code == 200, first.text
    assert [row["id"] for row in first.json()["data"]] == [event_id(3), event_id(2)]
    assert first.json()["has_more"] is True
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    # Repeating the same page is stable and repeats no row.
    assert (await get(client, limit=2)).json() == first.json()

    last = await get(client, limit=2, cursor=cursor)
    assert last.status_code == 200, last.text
    assert [row["id"] for row in last.json()["data"]] == [event_id(1)]
    assert last.json()["has_more"] is False
    assert last.json()["next_cursor"] is None

    # One row per page walks the frontier without duplicating or skipping.
    seen: list[str] = []
    cursor = None
    for _ in range(4):
        response = await get(client, limit=1, **({"cursor": cursor} if cursor else {}))
        assert response.status_code == 200, response.text
        seen.extend(row["id"] for row in response.json()["data"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == [event_id(3), event_id(2), event_id(1)]

    oldest = AuditEventPosition(1_700_000_000_000_001, event_id(1))
    past = await get(client, cursor=cursor_for(oldest, None))
    assert past.status_code == 200
    assert past.json()["data"] == []
    assert past.json()["has_more"] is False


async def test_the_action_filter_is_an_exact_match(http: Any) -> None:
    sandbox, client = http
    publish(sandbox, "audit-actions", 1_700_000_000_000_000)
    record(
        sandbox,
        number=1,
        binding_id="audit-actions",
        occurred_at_us=1_700_000_000_000_001,
        action="model_binding.retire",
    )
    record(
        sandbox,
        number=2,
        binding_id="audit-actions",
        occurred_at_us=1_700_000_000_000_002,
        action="model_binding.disable",
    )
    record(
        sandbox,
        number=3,
        binding_id="audit-actions",
        occurred_at_us=1_700_000_000_000_003,
        action="model_binding.activate",
    )
    for action, identity in (
        ("model_binding.retire", event_id(1)),
        ("model_binding.disable", event_id(2)),
        ("model_binding.activate", event_id(3)),
    ):
        response = await get(client, action=action)
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["data"]] == [identity]
        assert {row["action"] for row in response.json()["data"]} == {action}

    # A value no recorded event carries selects nothing; it is not an error.
    unknown = await get(client, action="profile.update")
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["data"] == []
    assert unknown.json()["has_more"] is False


async def test_a_cursor_cannot_cross_its_action_filter(http: Any) -> None:
    sandbox, client = http
    publish(sandbox, "audit-scope", 1_700_000_000_000_000)
    record(
        sandbox,
        number=3,
        binding_id="audit-scope",
        occurred_at_us=1_700_000_000_000_003,
        action="model_binding.disable",
    )
    record(
        sandbox,
        number=2,
        binding_id="audit-scope",
        occurred_at_us=1_700_000_000_000_002,
        action="model_binding.disable",
    )
    record(
        sandbox,
        number=1,
        binding_id="audit-scope",
        occurred_at_us=1_700_000_000_000_001,
        action="model_binding.retire",
    )
    page = await get(client, action="model_binding.disable", limit=1)
    assert page.status_code == 200, page.text
    cursor = page.json()["next_cursor"]
    assert cursor is not None
    crossed = await get(client, action="model_binding.retire", cursor=cursor)
    assert crossed.status_code == 400
    assert crossed.json()["error"]["code"] == "invalid_audit_cursor"

    for parameters in (
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not-a-cursor"},
        {"cursor": "x" * 513},
        {"unknown": "value"},
    ):
        invalid = await get(client, **parameters)
        assert invalid.status_code == 400, (parameters, invalid.text)
        assert invalid.json()["error"]["type"] == "invalid_request_error"


async def test_an_untrusted_stored_summary_fails_closed(http: Any) -> None:
    sandbox, client = http
    publish(sandbox, "audit-corrupt", 1_700_000_000_000_000)
    identity = record(
        sandbox,
        number=1,
        binding_id="audit-corrupt",
        occurred_at_us=1_700_000_000_000_001,
    )
    healthy = await get(client)
    assert healthy.status_code == 200

    # A stored summary that smuggles before/after payload keys must not reach
    # the wire; the page is refused instead of projecting a partial row.
    sandbox.admin.execute(
        f'UPDATE "{sandbox.schema}".{EVENT_TABLE} SET change_summary_json = %s::jsonb '
        "WHERE event_id = %s",
        ('{"changed_fields": ["/publication/status"], "before": "private body"}', identity),
    )
    broken = await get(client)
    assert broken.status_code == 503
    assert broken.json()["error"]["code"] == "audit_service_unavailable"


def test_the_audit_reader_identity_can_only_select(sandbox: Sandbox) -> None:
    publish(sandbox, "audit-reader", 1_700_000_000_000_000)
    record(
        sandbox,
        number=1,
        binding_id="audit-reader",
        occurred_at_us=1_700_000_000_000_001,
    )
    with sandbox.pools["audit"].connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        row = connection.execute(
            f'SELECT count(*) AS count FROM "{sandbox.schema}".{EVENT_TABLE}'
        ).fetchone()
        assert row is not None and row["count"] == 1
    connection = psycopg.connect(sandbox.urls["audit"], autocommit=True)
    connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    # Every statement below is otherwise complete: with a correct SELECT-only
    # grant it fails on privilege, not on a constraint or a missing reference.
    publish(sandbox, "audit-intruder", 1_700_000_000_000_000)
    intruder = (
        f"INSERT INTO {EVENT_TABLE} (event_id, kind, action, binding_id, revision, "
        "revision_id, result_status, request_id, actor_type, occurred_at_us, "
        "policy_revision, intent_digest, change_summary_json) VALUES "
        "('lifecycle-intruder', 'binding', 'model_binding.retire', 'audit-intruder', "
        "'1', 'binding-audit-intruder-1', 'retired', 'request-intruder', 'anonymous', "
        "1700000000000001, 1, 'sha256:" + "2" * 64 + "', '{}'::jsonb)"
    )
    try:
        for statement in (
            intruder,
            f"UPDATE {EVENT_TABLE} SET result_status = 'disabled'",
            f"DELETE FROM {EVENT_TABLE}",
            "SELECT count(*) FROM speech_publications",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
    finally:
        connection.close()


def test_runtime_rejects_a_writer_or_a_narrow_reader_before_serving(
    sandbox: Sandbox,
) -> None:
    runtime = AuditReaderRuntime(settings_for(sandbox))
    runtime.open()
    try:
        opened = runtime.service
        assert opened is not None
    finally:
        runtime.close()
    assert runtime.service is None

    # The lifecycle writer can SELECT and INSERT the event table; a read path
    # must not run with that identity.
    with pytest.raises(ValueError, match="Audit reader startup rejected"):
        AuditReaderRuntime(settings_for(sandbox, reader_url=sandbox.urls["lifecycle"])).open()

    narrow = NamedRole(sandbox, "narrow", readable=False)
    try:
        with pytest.raises(ValueError, match="Audit reader startup rejected"):
            AuditReaderRuntime(settings_for(sandbox, reader_url=narrow.url)).open()
    finally:
        narrow.drop()


@dataclass
class NamedRole:
    sandbox: Sandbox
    name: str
    readable: bool = True
    role: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.role = f"sn_audit_{self.name}_{suffix}"
        self.sandbox.admin.execute(f"CREATE ROLE \"{self.role}\" LOGIN PASSWORD '{PASSWORD}'")
        self.sandbox.admin.execute(
            f'GRANT USAGE ON SCHEMA "{self.sandbox.schema}" TO "{self.role}"'
        )
        if self.readable:
            self.sandbox.admin.execute(
                f'GRANT SELECT ON "{self.sandbox.schema}".{EVENT_TABLE} TO "{self.role}"'
            )
        self.url = make_conninfo(self.sandbox.urls["audit"], user=self.role, password=PASSWORD)

    def drop(self) -> None:
        self.sandbox.admin.execute(f'DROP OWNED BY "{self.role}"')
        self.sandbox.admin.execute(f'DROP ROLE IF EXISTS "{self.role}"')


async def test_route_answers_503_when_no_reader_identity_is_configured(sandbox: Sandbox) -> None:
    # With no configured reader identity the route must report an unavailable
    # service instead of advertising an empty history.
    unconfigured = settings_for(sandbox).model_copy(update={"audit_reader_database_url": None})
    app = create_app(
        settings=unconfigured,
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await get(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_service_unavailable"
