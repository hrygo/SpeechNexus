"""Real isolated PostgreSQL aggregates behind the administrative pool projection.

The pool row is never stored as one record: the counts are summed from the
instances that still carry budget. These cases therefore assert both the stored
state mapping and the aggregate arithmetic, and they run the assembled HTTP
route so the response is the validated contract shape rather than a hand-built
dictionary.
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
from pydantic import AnyHttpUrl, SecretStr
from speechnexus_contracts.admin_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe

from speechnexus_backend.app import create_app
from speechnexus_backend.resource_pool_runtime import ResourcePoolRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.application.resource_pools import ResourcePoolService
from speechnexus_backend.speech.domain.resource_pools import ResourcePoolsUnavailableError
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.resource_pools_postgres import (
    PostgresResourcePoolReads,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_pool_test"
DIGEST = "sha256:" + "b" * 64
URL = "/v1/nexus/admin/resource-pools"


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, Any]]
    schema: str
    role: str
    url: str
    service: ResourcePoolService

    def query(self, statement: str, parameters: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        cursor = self.admin.execute(statement, parameters or None)
        return list(cursor.fetchall()) if cursor.description else []

    def pool_row(
        self,
        pool_id: str,
        service_class: str,
        *,
        state: str = "active",
    ) -> None:
        self.admin.execute(
            "INSERT INTO resource_pools (pool_id, service_class, pool_state) VALUES (%s, %s, %s)",
            (pool_id, service_class, state),
        )

    def node(self, node_id: str) -> None:
        self.admin.execute(
            "INSERT INTO host_nodes "
            "(host_node_id, memory_architecture, node_state, topology_digest) "
            "VALUES (%s, 'unified_memory', 'active', %s)",
            (node_id, DIGEST),
        )

    def instance(
        self,
        *,
        instance_id: str,
        host_node_id: str,
        pool_id: str,
        state: str,
        max_slots: int,
        realtime_reserved_slots: int = 0,
        held_slots: int = 0,
        quarantined_slots: int = 0,
    ) -> None:
        self.admin.execute(
            "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
            "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
            "management_mode, loading_policy, config_digest, instance_state, max_slots, "
            "realtime_reserved_slots, held_slots, quarantined_slots) VALUES "
            "(%s, 1, %s, %s, 'gateway-test', 'binding-test', 'manifest-test', 'template-test', "
            "'managed', 'on_demand', %s, %s, %s, %s, %s, %s)",
            (
                instance_id,
                host_node_id,
                pool_id,
                DIGEST,
                state,
                max_slots,
                realtime_reserved_slots,
                held_slots,
                quarantined_slots,
            ),
        )


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not projection evidence"
        )
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_pools_" + suffix, "sn_pools_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    admin: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        url, row_factory=dict_row, autocommit=True
    )
    pool = speech_pool(
        make_conninfo(url, user=role, password=PASSWORD),
        schema=schema,
        name="speech-pools-" + suffix,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        # The reader identity is granted exactly the two aggregate relations and
        # nothing else; the runtime verifies that surface at startup.
        for table in ("resource_pools", "engine_instances"):
            admin.execute(f'GRANT SELECT ON "{schema}"."{table}" TO "{role}"')
        pool.open()
        yield Sandbox(
            admin=admin,
            schema=schema,
            role=role,
            url=url,
            service=ResourcePoolService(PostgresResourcePoolReads(pool)),
        )
    finally:
        pool.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.execute(f'DROP ROLE "{role}"')
        admin.close()
        engine.dispose()


def settings_for(sandbox: Sandbox, *, role: str | None = None) -> Settings:
    dsn = sandbox.url if role is None else make_conninfo(sandbox.url, user=role, password=PASSWORD)
    return Settings(
        environment="test",
        process_role="api",
        node_id="node-pools-test",
        instance_id="api-pools-test",
        database_url=SecretStr(dsn),
        database_schema=sandbox.schema,
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health/live"),
        object_storage_bucket="synthetic-pool-bucket",
        resource_pool_reader_database_url=SecretStr(
            make_conninfo(sandbox.url, user=role or sandbox.role, password=PASSWORD)
        ),
        asset_allowed_hosts=("test",),
    )


async def get(client: AsyncClient, path: str = URL) -> httpx.Response:
    return await client.get(path, headers={"X-Request-ID": "request-pools"})


@pytest.fixture
async def http(sandbox: Sandbox) -> Any:
    app = create_app(
        settings=settings_for(sandbox),
        resource_pools=sandbox.service,
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield sandbox, client


async def test_two_pools_are_projected_field_by_field_from_instance_aggregates(
    http: Any,
) -> None:
    sandbox, client = http
    sandbox.pool_row("asr-realtime", "realtime")
    # Opposite insertion order: a stable node list cannot be an accident of scan
    # order, and the aggregate has to span more than one host node.
    sandbox.node("node-b")
    sandbox.node("node-a")
    sandbox.instance(
        instance_id="instance-b",
        host_node_id="node-b",
        pool_id="asr-realtime",
        state="ready",
        max_slots=2,
        realtime_reserved_slots=1,
        held_slots=1,
        quarantined_slots=1,
    )
    sandbox.instance(
        instance_id="instance-a",
        host_node_id="node-a",
        pool_id="asr-realtime",
        state="ready",
        max_slots=4,
        realtime_reserved_slots=3,
        held_slots=1,
    )
    sandbox.pool_row("tts-batch", "batch")
    response = await get(client)
    assert response.status_code == 200
    body = response.json()
    validate_response("listResourcePools", 200, {"body": body})
    assert body == {
        "object": "nexus.list",
        "data": [
            {
                "id": "asr-realtime",
                "object": "nexus.resource_pool",
                "node_ids": ["node-a", "node-b"],
                "service_class": "realtime",
                "max_active_slots": 6,
                "realtime_reserved_slots": 4,
                "active_slots": 2,
                "quarantine_slots": 1,
                "status": "ready",
            },
            {
                "id": "tts-batch",
                "object": "nexus.resource_pool",
                "node_ids": [],
                "service_class": "batch",
                "max_active_slots": 0,
                "realtime_reserved_slots": 0,
                "active_slots": 0,
                "quarantine_slots": 0,
                "status": "unavailable",
            },
        ],
    }


async def test_empty_registry_is_an_empty_data_array_not_null(http: Any) -> None:
    sandbox, client = http
    assert sandbox.query("SELECT * FROM resource_pools") == []
    response = await get(client)
    assert response.status_code == 200
    body = response.json()
    assert body == {"object": "nexus.list", "data": []}
    assert isinstance(body["data"], list)


async def test_stopped_instances_do_not_contribute_nodes_or_slots(http: Any) -> None:
    sandbox, client = http
    sandbox.pool_row("tts-realtime", "realtime")
    sandbox.node("node-live")
    sandbox.node("node-retired")
    sandbox.instance(
        instance_id="instance-live",
        host_node_id="node-live",
        pool_id="tts-realtime",
        state="ready",
        max_slots=2,
        held_slots=1,
    )
    sandbox.instance(
        instance_id="instance-retired",
        host_node_id="node-retired",
        pool_id="tts-realtime",
        state="stopped",
        max_slots=8,
    )
    response = await get(client)
    assert response.status_code == 200
    pool = response.json()["data"][0]
    assert pool["node_ids"] == ["node-live"]
    assert pool["max_active_slots"] == 2
    assert pool["active_slots"] == 1
    assert pool["status"] == "ready"


async def test_capacity_without_a_serving_instance_is_degraded_and_a_disabled_pool_is_not(
    http: Any,
) -> None:
    sandbox, client = http
    sandbox.pool_row("voice-creation", "interactive")
    sandbox.node("node-preparing")
    sandbox.instance(
        instance_id="instance-preparing",
        host_node_id="node-preparing",
        pool_id="voice-creation",
        state="preparing",
        max_slots=4,
        realtime_reserved_slots=1,
        quarantined_slots=1,
    )
    sandbox.pool_row("auxiliary", "batch", state="disabled")
    sandbox.node("node-disabled")
    sandbox.instance(
        instance_id="instance-disabled",
        host_node_id="node-disabled",
        pool_id="auxiliary",
        state="ready",
        max_slots=1,
    )
    response = await get(client)
    assert response.status_code == 200
    pools = {item["id"]: item for item in response.json()["data"]}
    assert pools["voice-creation"]["status"] == "degraded"
    assert pools["voice-creation"]["node_ids"] == ["node-preparing"]
    assert pools["auxiliary"]["status"] == "unavailable"


async def test_undeclared_query_parameter_is_refused_instead_of_ignored(http: Any) -> None:
    _, client = http
    response = await client.get(URL + "?limit=1", headers={"X-Request-ID": "request-pools"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_resource_pool_request"
    assert error["retryable"] is False


def test_reader_identity_cannot_write_the_aggregate_inputs(sandbox: Sandbox) -> None:
    with psycopg.connect(
        make_conninfo(sandbox.url, user=sandbox.role, password=PASSWORD)
    ) as reader:
        reader.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("UPDATE engine_instances SET held_slots = held_slots + 1 WHERE false")


def test_runtime_verifies_its_read_only_surface_before_serving(sandbox: Sandbox) -> None:
    runtime = ResourcePoolRuntime(settings_for(sandbox))
    runtime.open()
    try:
        service = runtime.service
        assert service is not None
        assert isinstance(service.list(), tuple)
    finally:
        runtime.close()
    assert runtime.service is None
    writer = "sn_pools_writer_" + uuid.uuid4().hex[:12]
    sandbox.admin.execute(f"CREATE ROLE \"{writer}\" LOGIN PASSWORD '{PASSWORD}'")
    try:
        sandbox.admin.execute(f'GRANT USAGE ON SCHEMA "{sandbox.schema}" TO "{writer}"')
        for table in ("resource_pools", "engine_instances"):
            sandbox.admin.execute(f'GRANT SELECT ON "{sandbox.schema}"."{table}" TO "{writer}"')
        sandbox.admin.execute(
            f'GRANT INSERT ON "{sandbox.schema}"."engine_instances" TO "{writer}"'
        )
        rejected = ResourcePoolRuntime(settings_for(sandbox, role=writer))
        with pytest.raises(ValueError, match="Resource pool reader startup rejected"):
            rejected.open()
        assert rejected.service is None
    finally:
        sandbox.admin.execute(f'DROP OWNED BY "{writer}"')
        sandbox.admin.execute(f'DROP ROLE "{writer}"')


def test_unreachable_aggregates_are_reported_instead_of_empty(sandbox: Sandbox) -> None:
    sandbox.pool_row("asr-batch", "batch")
    sandbox.pool_row("asr-realtime", "realtime")
    # A pool identity outside the closed domain cannot be projected; the read
    # fails closed rather than dropping the row or inventing a status.
    sandbox.admin.execute(
        "UPDATE resource_pools SET pool_id = 'synthetic-unknown' WHERE pool_id = 'asr-batch'"
    )
    with pytest.raises(ResourcePoolsUnavailableError):
        sandbox.service.list()
