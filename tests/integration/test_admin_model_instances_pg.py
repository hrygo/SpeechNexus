"""Real isolated PostgreSQL proof for the administrative model instance reads.

The projection reads ``engine_instances`` and the immutable binding revision it
names through the dedicated read-only identity. These cases exercise the
assembled HTTP routes against a real migrated schema, so the response is the
contract-validated shape rather than a hand-built dictionary: the empty set,
deterministic key order across tied timestamps and multiple generations,
cross-page keyset boundaries, one recorded generation, both 404 layers, the
fail-closed join and the read-only grant set.

Instances and binding revisions are written by the migration administrator
because this slice only reads them; the writers' own transaction coverage lives
in the coordination and binding integration suites.
"""

from __future__ import annotations

import hashlib
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
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.model_instance_runtime import ModelInstanceRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.model_instances import ModelInstanceService
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.model_instances import InstancePosition
from speechnexus_backend.speech.infrastructure.model_instances_postgres import (
    PostgresModelInstanceReads,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.interfaces.http.model_instances import cursor_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_instance_reader"
URL = "/v1/nexus/admin/model-instances"
READABLE = ("engine_instances", "speech_binding_revisions")
DIGEST = "sha256:" + "a" * 64


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, Any]]
    schema: str
    urls: dict[str, str]
    pools: dict[str, SpeechPool]
    service: ModelInstanceService


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not projection evidence"
        )
    suffix = uuid.uuid4().hex[:10]
    schema = "sn_instances_" + suffix
    role = "sn_instances_reader_" + suffix
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
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    # The reader identity is granted exactly the two relations it reads and
    # nothing else; the runtime verifies that surface at startup.
    for table in READABLE:
        admin.execute(f'GRANT SELECT ON "{schema}"."{table}" TO "{role}"')
    url = make_conninfo(base, user=role, password=PASSWORD)
    pool = speech_pool(url, schema=schema, max_size=2, name="instances-" + suffix)
    pool.open()
    try:
        yield Sandbox(
            admin=admin,
            schema=schema,
            urls={"reader": url},
            pools={"reader": pool},
            service=ModelInstanceService(PostgresModelInstanceReads(pool)),
        )
    finally:
        pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def settings_for(sandbox: Sandbox, *, reader_url: str | None = None) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "process_role": "api",
            "database_schema": sandbox.schema,
            "model_instance_reader_database_url": (
                sandbox.urls["reader"] if reader_url is None else reader_url
            ),
            "asset_allowed_hosts": ("test",),
            "model_instance_read_concurrency": 1,
        }
    )


@pytest.fixture
async def http(sandbox: Sandbox) -> Any:
    app = create_app(
        settings=settings_for(sandbox),
        model_instances=sandbox.service,
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield sandbox, client


def bind_revision(sandbox: Sandbox, binding_id: str, revision: str = "7") -> str:
    """Insert the immutable revision the instance names; return its revision id."""
    revision_id = f"binding-revision-{binding_id}-{revision}"
    request_digest = digest(f"request-{binding_id}-{revision}")
    key_digest = digest(f"key-{binding_id}-{revision}")
    sandbox.admin.execute(
        "INSERT INTO speech_binding_families (binding_id, latest_revision) VALUES (%s, %s)",
        (binding_id, revision),
    )
    sandbox.admin.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, %s, 1, '{}', %s)",
        (key_digest, binding_id, request_digest),
    )
    sandbox.admin.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, definition_json, "
        " definition_digest, request_digest, created_at_us) "
        "VALUES (%s, %s, %s, %s, 1, '{}', %s, %s, %s)",
        (
            binding_id,
            revision,
            revision_id,
            key_digest,
            digest("definition-" + binding_id),
            request_digest,
            1_700_000_000_000_000,
        ),
    )
    return revision_id


def pool_row(sandbox: Sandbox, pool_id: str) -> None:
    sandbox.admin.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES (%s, 'realtime', 'active')",
        (pool_id,),
    )


def node(sandbox: Sandbox, node_id: str) -> None:
    sandbox.admin.execute(
        "INSERT INTO host_nodes "
        "(host_node_id, memory_architecture, node_state, topology_digest) "
        "VALUES (%s, 'unified_memory', 'active', %s)",
        (node_id, DIGEST),
    )


def record(
    sandbox: Sandbox,
    *,
    instance_id: str,
    generation: int = 1,
    host_node_id: str = "node-a",
    pool_id: str = "asr-realtime",
    binding_id: str = "asr-quality",
    binding_revision_id: str | None = None,
    state: str = "ready",
    ready_at_us: int | None = 1_700_000_000_000_001,
    stopped_at_us: int | None = None,
    quarantine_reason: str | None = None,
) -> None:
    sandbox.admin.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots, ready_at_us, stopped_at_us, "
        "quarantine_reason) VALUES "
        "(%s, %s, %s, %s, 'gateway-1', %s, 'manifest-1', 'template-1', 'managed', 'on_demand', "
        "%s, %s, 4, %s, %s, %s)",
        (
            instance_id,
            generation,
            host_node_id,
            pool_id,
            binding_revision_id or f"binding-revision-{binding_id}-7",
            DIGEST,
            state,
            ready_at_us,
            stopped_at_us,
            quarantine_reason,
        ),
    )


async def get(client: AsyncClient, path: str = URL, **parameters: Any) -> httpx.Response:
    return await client.get(path, params=parameters, headers={"X-Request-ID": "request-instances"})


async def test_empty_store_returns_an_empty_page(http: Any) -> None:
    _, client = http
    response = await get(client)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "request-instances"
    assert response.json() == {
        "object": "nexus.list",
        "data": [],
        "has_more": False,
        "next_cursor": None,
    }


async def test_one_generation_is_projected_field_by_field(http: Any) -> None:
    sandbox, client = http
    node(sandbox, "node-a")
    pool_row(sandbox, "asr-realtime")
    revision_id = bind_revision(sandbox, "asr-quality")
    record(sandbox, instance_id="inst-asr-1", binding_revision_id=revision_id)

    response = await get(client)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "object": "nexus.list",
        "data": [
            {
                "schema_version": "1.0.0",
                "instance_id": "inst-asr-1",
                "host_node_id": "node-a",
                "gateway_id": "gateway-1",
                "binding": {"id": "asr-quality", "revision": "7"},
                "binding_revision_id": revision_id,
                "pool_id": "asr-realtime",
                "generation": 1,
                "config_digest": DIGEST,
                "state": "ready",
                "readiness": {
                    "accepting_new_work": True,
                    "ready_at": "2023-11-14T22:13:20.000001Z",
                },
                "observed": {"stopped_at": None},
                "quarantine_reason": None,
            }
        ],
        "has_more": False,
        "next_cursor": None,
    }


async def test_tied_timestamps_and_generations_page_by_the_composite_key(http: Any) -> None:
    sandbox, client = http
    node(sandbox, "node-a")
    pool_row(sandbox, "asr-realtime")
    bind_revision(sandbox, "asr-quality")
    # Insertion order is c, b2, b1, a; every ready instance shares one timestamp,
    # so only the (instance_id, generation) key can order the page.
    for instance_id, generation in (
        ("inst-c", 1),
        ("inst-b", 2),
        ("inst-b", 1),
        ("inst-a", 1),
    ):
        record(sandbox, instance_id=instance_id, generation=generation)

    first = await get(client, limit=2)
    assert first.status_code == 200, first.text
    assert [(row["instance_id"], row["generation"]) for row in first.json()["data"]] == [
        ("inst-a", 1),
        ("inst-b", 1),
    ]
    assert first.json()["has_more"] is True
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    # Repeating the same page is stable and repeats no row.
    assert (await get(client, limit=2)).json() == first.json()

    last = await get(client, limit=2, cursor=cursor)
    assert last.status_code == 200, last.text
    assert [(row["instance_id"], row["generation"]) for row in last.json()["data"]] == [
        ("inst-b", 2),
        ("inst-c", 1),
    ]
    assert last.json()["has_more"] is False
    assert last.json()["next_cursor"] is None

    seen: list[tuple[str, int]] = []
    cursor = None
    for _ in range(5):
        response = await get(client, limit=1, **({"cursor": cursor} if cursor else {}))
        assert response.status_code == 200, response.text
        seen.extend((row["instance_id"], row["generation"]) for row in response.json()["data"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == [("inst-a", 1), ("inst-b", 1), ("inst-b", 2), ("inst-c", 1)]

    past = await get(client, cursor=cursor_for(InstancePosition("inst-c", 1), None, None))
    assert past.status_code == 200
    assert past.json()["data"] == []
    assert past.json()["has_more"] is False


async def test_the_state_and_host_filters_are_exact_matches(http: Any) -> None:
    sandbox, client = http
    node(sandbox, "node-a")
    node(sandbox, "node-b")
    pool_row(sandbox, "asr-realtime")
    bind_revision(sandbox, "asr-quality")
    record(sandbox, instance_id="inst-a", host_node_id="node-a")
    record(
        sandbox,
        instance_id="inst-b",
        host_node_id="node-b",
        state="stopped",
        ready_at_us=None,
        stopped_at_us=1_700_000_000_000_009,
    )

    stopped = await get(client, state="stopped")
    assert stopped.status_code == 200, stopped.text
    assert [row["instance_id"] for row in stopped.json()["data"]] == ["inst-b"]
    assert stopped.json()["data"][0]["observed"] == {"stopped_at": "2023-11-14T22:13:20.000009Z"}
    assert stopped.json()["data"][0]["readiness"]["accepting_new_work"] is False

    node_b = await get(client, host_node_id="node-b")
    assert [row["instance_id"] for row in node_b.json()["data"]] == ["inst-b"]

    both = await get(client, host_node_id="node-a", state="stopped")
    assert both.json()["data"] == []

    # A cursor issued under one filter cannot page another filter.
    cursor = stopped.json()["next_cursor"] or cursor_for(
        InstancePosition("inst-b", 1), "node-b", "stopped"
    )
    mismatched = await get(client, cursor=cursor, state="ready")
    assert mismatched.status_code == 400
    assert mismatched.json()["error"]["code"] == "invalid_model_instance_cursor"


async def test_a_quarantine_reason_is_reported_only_while_quarantined(http: Any) -> None:
    sandbox, client = http
    node(sandbox, "node-a")
    pool_row(sandbox, "asr-realtime")
    bind_revision(sandbox, "asr-quality")
    record(
        sandbox,
        instance_id="inst-a",
        state="quarantined",
        ready_at_us=None,
        quarantine_reason="repeated load failure",
    )
    # The store keeps the reason after the instance stops; the contract confines
    # the field to the quarantined state.
    record(
        sandbox,
        instance_id="inst-b",
        state="stopped",
        ready_at_us=None,
        stopped_at_us=1_700_000_000_000_009,
        quarantine_reason="repeated load failure",
    )

    response = await get(client)
    assert response.status_code == 200, response.text
    rows = {row["instance_id"]: row for row in response.json()["data"]}
    assert rows["inst-a"]["quarantine_reason"] == "repeated load failure"
    assert rows["inst-b"]["quarantine_reason"] is None


async def test_one_recorded_generation_is_returned_by_its_identity(http: Any) -> None:
    sandbox, client = http
    node(sandbox, "node-a")
    pool_row(sandbox, "asr-realtime")
    revision_id = bind_revision(sandbox, "asr-quality")
    record(sandbox, instance_id="inst-asr-1", generation=1, binding_revision_id=revision_id)
    record(
        sandbox,
        instance_id="inst-asr-1",
        generation=2,
        binding_revision_id=revision_id,
        state="draining",
        ready_at_us=None,
    )

    response = await get(client, URL + "/inst-asr-1/generations/2")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["instance_id"], body["generation"]) == ("inst-asr-1", 2)
    assert body["state"] == "draining"
    assert body["readiness"] == {"accepting_new_work": False, "ready_at": None}
    assert body["binding"] == {"id": "asr-quality", "revision": "7"}


async def test_a_missing_instance_and_a_missing_generation_differ(http: Any) -> None:
    sandbox, client = http
    node(sandbox, "node-a")
    pool_row(sandbox, "asr-realtime")
    bind_revision(sandbox, "asr-quality")
    record(sandbox, instance_id="inst-asr-1", generation=1)

    absent_instance = await get(client, URL + "/inst-absent/generations/1")
    assert absent_instance.status_code == 404
    assert absent_instance.json()["error"]["code"] == "model_instance_unknown"

    absent_generation = await get(client, URL + "/inst-asr-1/generations/2")
    assert absent_generation.status_code == 404
    assert absent_generation.json()["error"]["code"] == "model_instance_generation_unknown"
    assert absent_instance.json()["error"]["code"] != absent_generation.json()["error"]["code"]
    for response in (absent_instance, absent_generation):
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["error"]["type"] == "not_found_error"
        assert response.json()["error"]["request_id"] == "request-instances"


async def test_an_instance_whose_revision_row_is_missing_fails_closed(http: Any) -> None:
    sandbox, client = http
    node(sandbox, "node-a")
    pool_row(sandbox, "asr-realtime")
    # No speech_binding_revisions row for this identity: the binding cannot be
    # projected truthfully, so the read refuses instead of omitting the row.
    record(sandbox, instance_id="inst-asr-1", binding_revision_id="binding-revision-missing")

    listing = await get(client)
    assert listing.status_code == 503
    assert listing.json()["error"]["code"] == "model_instance_service_unavailable"
    single = await get(client, URL + "/inst-asr-1/generations/1")
    assert single.status_code == 503
    assert single.json()["error"]["code"] == "model_instance_service_unavailable"


def test_the_reader_identity_cannot_write(sandbox: Sandbox) -> None:
    with sandbox.pools["reader"].connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        for table in READABLE:
            connection.execute(f'SELECT count(*) FROM "{sandbox.schema}"."{table}"').fetchone()
    connection = psycopg.connect(sandbox.urls["reader"], autocommit=True)
    connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    try:
        for statement in (
            "INSERT INTO engine_instances (instance_id) VALUES ('inst-write')",
            "UPDATE engine_instances SET instance_state = 'ready'",
            "DELETE FROM engine_instances",
            "INSERT INTO speech_binding_revisions (binding_id) VALUES ('asr-write')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
    finally:
        connection.close()


def test_runtime_verifies_the_read_only_identity_before_serving(sandbox: Sandbox) -> None:
    runtime = ModelInstanceRuntime(settings_for(sandbox))
    runtime.open()
    try:
        opened = runtime.service
        assert opened is not None
    finally:
        runtime.close()
    assert runtime.service is None
