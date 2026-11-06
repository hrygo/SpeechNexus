"""Placeholders stay inert in the production assembly against a real database.

#473: every declared-but-unimplemented operation is answered by the central
placeholder before any authoritative read, while an implemented route on the
same application still reaches the migrated schema. The isolated schema is
created per run and dropped in ``finally``; a missing DSN fails the test because
a skip is not evidence.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.interfaces.http.unimplemented import UNIMPLEMENTED_OPERATIONS
from speechnexus_backend.speech.interfaces.http.unimplemented_errors import (
    OPERATION_NOT_IMPLEMENTED,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_unimplemented_test"
TABLES = (
    "speech_pronunciation_families",
    "speech_pronunciation_revisions",
    "speech_pronunciation_commands",
)


@dataclass
class Sandbox:
    owner: psycopg.Connection[dict[str, Any]] = field(repr=False)
    schema: str
    role: str
    reader_dsn: str = field(repr=False)

    def settings(self) -> Settings:
        return _settings().model_copy(
            update={
                "database_schema": self.schema,
                "pronunciation_reader_database_url": SecretStr(self.reader_dsn),
                # The pronunciation ingress rejects an unlisted Host before any
                # route; that boundary is unchanged by the placeholder.
                "asset_allowed_hosts": ("speech.test", "localhost"),
            }
        )

    def snapshot(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in TABLES:
            row = self.owner.execute(
                f'SELECT count(*) AS n FROM "{self.schema}".{table}'
            ).fetchone()
            assert row is not None
            counts[table] = int(row["n"])
        return counts


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not evidence"
        )
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_unimpl_" + suffix, "sn_unimpl_reader_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    owner = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        owner.execute(f'SET search_path TO "{schema}", pg_catalog')
        owner.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'")
        # The reader identity sees only the fixed revision table; the runtime
        # rejects any broader grant, so the sandbox must be the minimum role.
        owner.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        owner.execute(f'GRANT SELECT ON "{schema}".speech_pronunciation_revisions TO "{role}"')
        yield Sandbox(
            owner=owner,
            schema=schema,
            role=role,
            reader_dsn=make_conninfo(url, user=role, password=PASSWORD),
        )
    finally:
        owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        owner.execute(f'DROP ROLE IF EXISTS "{role}"')
        owner.close()
        engine.dispose()


def concrete(path: str) -> str:
    while "{" in path:
        start = path.index("{")
        end = path.index("}", start)
        path = path[:start] + "synthetic-id" + path[end + 1 :]
    return path


async def test_placeholders_answer_without_touching_the_authoritative_store(
    sandbox: Sandbox,
) -> None:
    before = sandbox.snapshot()
    api = create_app(settings=sandbox.settings())
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://speech.test"
        ) as client,
    ):
        for method, path in UNIMPLEMENTED_OPERATIONS.values():
            response = await client.request(
                method,
                concrete(path),
                headers={"X-Request-ID": "unimplemented-473"},
                content=b"{}" if method in {"POST", "PATCH", "PUT"} else None,
            )
            assert response.status_code == 503, (method, path, response.text)
            body = response.json()
            assert body["error"]["code"] == OPERATION_NOT_IMPLEMENTED, (method, path)
            assert body["error"]["retryable"] is False
            assert body["error"]["request_id"] == "unimplemented-473"
            assert response.headers["x-request-id"] == "unimplemented-473"

        # The same assembly still reaches the migrated store: an empty fixed
        # revision is a confirmed not-found, not "unimplemented".
        missing = await client.get("/v1/nexus/pronunciation-sets/synthetic-voice/revisions/1")
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "pronunciation_not_found"
    assert sandbox.snapshot() == before
    assert set(before) == set(TABLES)
