"""Real PostgreSQL: keyset listing, immutable draft creation and minimum roles.

The reader may only SELECT the two fact tables and the writer may only INSERT
revisions and advance the two A-owned pointers; both are granted by the
production helpers so this exercise cannot pass with a broader identity.
"""

from __future__ import annotations

import asyncio
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
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from speechnexus_contracts.pronunciation_sets_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.pronunciation_examples import pronunciation_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.pronunciation_draft_schema import (
    pronunciation_draft_grants,
)
from speechnexus_backend.speech.interfaces.http.pronunciation_sets import (
    CREATE_OPERATION,
    LIST_OPERATION,
    PATH,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
KEY = "synthetic-sets-key"
MAX_BYTES = 4096


@dataclass
class Sandbox:
    url: str
    schema: str
    reader: str
    writer: str
    admin: psycopg.Connection[dict[str, Any]]
    settings: Settings

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return cursor.fetchall() if cursor.description else []

    def app(self, **changes: Any) -> Any:
        return create_app(settings=self.settings.model_copy(update=changes))

    def counts(self) -> tuple[int, int]:
        family = self.query("SELECT count(*) AS n FROM speech_pronunciation_families")[0]["n"]
        revision = self.query("SELECT count(*) AS n FROM speech_pronunciation_revisions")[0]["n"]
        return family, revision

    def heads(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT pronunciation_id, latest_revision FROM speech_pronunciation_families "
            "ORDER BY pronunciation_id"
        )


def dsn(url: str, role: str, password: str) -> str:
    return make_conninfo(url, user=role, password=password)


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set SPEECHNEXUS_TEST_DATABASE_URL to an isolated PostgreSQL database")
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_sets_" + suffix
    reader, writer = "sn_sets_reader_" + suffix, "sn_sets_writer_" + suffix
    reader_password, writer_password = "synthetic-sets-reader", "synthetic-sets-writer"
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}",pg_catalog')
    try:
        admin.execute(f"CREATE ROLE \"{reader}\" LOGIN PASSWORD '{reader_password}'")
        admin.execute(f"CREATE ROLE \"{writer}\" LOGIN PASSWORD '{writer_password}'")
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{reader}"')
        admin.execute(f'GRANT SELECT ON "{schema}".speech_pronunciation_revisions TO "{reader}"')
        admin.execute(f'GRANT SELECT ON "{schema}".speech_pronunciation_families TO "{reader}"')
        for grant in pronunciation_draft_grants(schema, writer):
            admin.execute(grant)
        settings = Settings(
            **{
                **_settings().model_dump(),
                "database_schema": schema,
                "pronunciation_reader_database_url": dsn(url, reader, reader_password),
                "pronunciation_read_concurrency": 1,
                "pronunciation_read_timeout_seconds": 1,
                "pronunciation_writer_database_url": dsn(url, writer, writer_password),
                "pronunciation_write_concurrency": 4,
                "pronunciation_transaction_timeout_seconds": 2,
                "pronunciation_receive_max_bytes": MAX_BYTES,
                "asset_allowed_hosts": ("testserver", "test"),
            }
        )
        yield Sandbox(url, schema, reader, writer, admin, settings)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in (writer, reader):
            admin.execute(f'DROP OWNED BY "{role}"')
            admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def check(response: httpx.Response, status: int, operation: str) -> None:
    assert response.status_code == status, response.text
    validate_response(
        operation, status, {"body": response.json(), "headers": dict(response.headers)}
    )


def create_body(**changes: Any) -> dict[str, Any]:
    return pronunciation_request(**changes)


async def test_empty_list_create_append_replay_conflict_and_publication(
    sandbox: Sandbox,
) -> None:
    db = sandbox
    app = db.app()
    async with app.router.lifespan_context(app), client(app) as http:
        empty = await http.get(PATH)
        check(empty, 200, LIST_OPERATION)
        assert empty.json() == {
            "object": "nexus.list",
            "data": [],
            "has_more": False,
            "next_cursor": None,
        }

        created = await http.post(PATH, json=create_body(), headers={"Idempotency-Key": KEY})
        check(created, 201, CREATE_OPERATION)
        first = created.json()
        assert first["status"] == "draft" and first["revision"] == "1"
        family = first["id"]
        rows = db.query(
            "SELECT encoding, entry_count FROM speech_pronunciation_revisions "
            "WHERE pronunciation_id=%s AND revision='1'",
            (family,),
        )
        assert rows == [{"encoding": "pinyin", "entry_count": 2}]

        appended = await http.post(
            PATH,
            json=create_body(id=family, expected_revision="1", encoding="ipa"),
            headers={"Idempotency-Key": KEY + "-append"},
        )
        check(appended, 201, CREATE_OPERATION)
        assert appended.json()["revision"] == "2"
        assert db.heads() == [{"pronunciation_id": family, "latest_revision": "2"}]

        stale = await http.post(
            PATH,
            json=create_body(id=family, expected_revision="1", encoding="pinyin"),
            headers={"Idempotency-Key": KEY + "-stale"},
        )
        check(stale, 409, CREATE_OPERATION)
        assert stale.json()["error"]["code"] == "pronunciation_configuration_changed"

        before = db.counts()
        replay = await http.post(PATH, json=create_body(), headers={"Idempotency-Key": KEY})
        check(replay, 201, CREATE_OPERATION)
        assert replay.json() == first
        assert db.counts() == before

        conflict = await http.post(
            PATH, json=create_body(encoding="ipa"), headers={"Idempotency-Key": KEY}
        )
        check(conflict, 409, CREATE_OPERATION)
        assert conflict.json()["error"]["code"] == "pronunciation_idempotency_conflict"
        assert db.counts() == before

        published = await http.post(
            PATH,
            json=create_body(publish=True),
            headers={"Idempotency-Key": KEY + "-publish"},
        )
        check(published, 422, CREATE_OPERATION)
        assert published.json()["error"]["code"] == "pronunciation_publication_not_supported"
        assert db.counts() == before

        oversized = await http.post(
            PATH,
            json=create_body(entries=[{"grapheme": "synthetic", "phonemes": "x"}] * 200),
            headers={"Idempotency-Key": KEY + "-large"},
        )
        check(oversized, 400, CREATE_OPERATION)
        assert oversized.json()["error"]["code"] == "pronunciation_invalid_request"
        assert db.counts() == before

        listed = await http.get(PATH)
        check(listed, 200, LIST_OPERATION)
        assert [item["id"] for item in listed.json()["data"]] == [family]
        assert listed.json()["data"][0]["entry_count"] == 2
        assert listed.json()["data"][0]["head_revision"] == "2"


async def test_keyset_pagination_has_no_duplicates_or_omissions(sandbox: Sandbox) -> None:
    db = sandbox
    app = db.app()
    async with app.router.lifespan_context(app), client(app) as http:
        for index in range(3):
            response = await http.post(
                PATH,
                json=create_body(encoding="ipa" if index else "pinyin"),
                headers={"Idempotency-Key": f"{KEY}-{index}"},
            )
            check(response, 201, CREATE_OPERATION)
        expected = [row["pronunciation_id"] for row in db.heads()]
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(4):
            query = {"limit": "1"} | ({"cursor": cursor} if cursor else {})
            page = await http.get(PATH, params=query)
            check(page, 200, LIST_OPERATION)
            body = page.json()
            seen.extend(item["id"] for item in body["data"])
            if not body["has_more"]:
                assert body["next_cursor"] is None
                break
            cursor = body["next_cursor"]
        assert seen == expected
        assert len(set(seen)) == 3
        tampered = cursor + "=" if cursor else "cHJvbi1hYWE="
        rejected = await http.get(PATH, params={"cursor": tampered})
        check(rejected, 400, LIST_OPERATION)


async def test_concurrent_append_has_exactly_one_winner(sandbox: Sandbox) -> None:
    db = sandbox
    app = db.app()
    async with app.router.lifespan_context(app), client(app) as http:
        created = await http.post(
            PATH, json=create_body(), headers={"Idempotency-Key": KEY + "-base"}
        )
        check(created, 201, CREATE_OPERATION)
        family = created.json()["id"]
        left, right = await asyncio.gather(
            http.post(
                PATH,
                json=create_body(id=family, expected_revision="1", encoding="ipa"),
                headers={"Idempotency-Key": KEY + "-left"},
            ),
            http.post(
                PATH,
                json=create_body(
                    id=family,
                    expected_revision="1",
                    entries=[{"grapheme": "other", "phonemes": "o th er"}],
                ),
                headers={"Idempotency-Key": KEY + "-right"},
            ),
        )
        assert sorted([left.status_code, right.status_code]) == [201, 409], (
            left.text,
            right.text,
        )
        loser = left if left.status_code == 409 else right
        assert loser.json()["error"]["code"] == "pronunciation_configuration_changed"
        assert db.heads() == [{"pronunciation_id": family, "latest_revision": "2"}]
        assert db.counts() == (1, 2)


def test_reader_missing_families_select_is_rejected_at_startup(sandbox: Sandbox) -> None:
    db = sandbox
    suffix = uuid.uuid4().hex[:8]
    narrow = "sn_sets_narrow_" + suffix
    db.admin.execute(f"CREATE ROLE \"{narrow}\" LOGIN PASSWORD 'synthetic-narrow'")
    try:
        db.admin.execute(f'GRANT USAGE ON SCHEMA "{db.schema}" TO "{narrow}"')
        db.admin.execute(
            f'GRANT SELECT ON "{db.schema}".speech_pronunciation_revisions TO "{narrow}"'
        )
        app = db.app(
            pronunciation_reader_database_url=SecretStr(dsn(db.url, narrow, "synthetic-narrow"))
        )
        with (
            pytest.raises(ValueError, match="Pronunciation reader startup rejected"),
            TestClient(app),
        ):
            pytest.fail("Reader without family SELECT was accepted")
        assert app.state.pronunciation_runtime.provider is None
    finally:
        db.admin.execute(f'DROP OWNED BY "{narrow}"')
        db.admin.execute(f'DROP ROLE IF EXISTS "{narrow}"')


def test_writer_extra_column_update_is_rejected_at_startup(sandbox: Sandbox) -> None:
    db = sandbox
    db.admin.execute(
        f'GRANT UPDATE (definition_json) ON "{db.schema}".speech_pronunciation_revisions '
        f'TO "{db.writer}"'
    )
    app = db.app()
    with (
        pytest.raises(ValueError, match="Pronunciation writer startup rejected"),
        TestClient(app),
    ):
        pytest.fail("Writer with a revision UPDATE was accepted")
    assert app.state.pronunciation_writer_runtime.provider is None
