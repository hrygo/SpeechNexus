"""Real PostgreSQL: keyset listing, immutable drafts and atomic publication.

The reader may only SELECT the two fact tables plus the publication ledger, the
draft writer may only INSERT revisions and advance the two A-owned pointers, and
the publisher may additionally append the ledger and advance the narrow head and
catalogue-version columns in one transaction. Every identity is granted by the
production helpers, so this exercise cannot pass with a broader role.
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
from speechnexus_contracts.pronunciation_read_http import (
    validate_response as validate_read_response,
)
from speechnexus_contracts.pronunciation_sets_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from tests.integration.test_coordination_foundation import (
    UpgradeSandbox,
)
from tests.integration.test_coordination_foundation import (
    upgrade_sandbox as upgrade_sandbox,
)
from tests.integration.test_production_jobs import database as database
from tests.support.pronunciation_examples import pronunciation_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.pronunciation_draft_schema import (
    pronunciation_draft_grants,
    pronunciation_publication_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    publication_grants,
)
from speechnexus_backend.speech.interfaces.http.pronunciation_sets import (
    CREATE_OPERATION,
    LIST_OPERATION,
    PATH,
)
from speechnexus_backend.speech.interfaces.http.pronunciations import (
    OPERATION as READ_OPERATION,
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
    publisher: str
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

    def published(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT kind, object_id, revision, revision_id FROM speech_publications "
            "WHERE kind='pronunciation' ORDER BY object_id, revision"
        )

    def publication_heads(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT object_id, revision FROM speech_publication_heads "
            "WHERE kind='pronunciation' ORDER BY object_id"
        )

    def catalog_revision(self) -> int:
        return int(
            self.query("SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE")[0][
                "revision"
            ]
        )

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
    publisher = "sn_sets_publisher_" + suffix
    reader_password, writer_password = "synthetic-sets-reader", "synthetic-sets-writer"
    publisher_password = "synthetic-sets-publisher"
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
        admin.execute(f"CREATE ROLE \"{publisher}\" LOGIN PASSWORD '{publisher_password}'")
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{reader}"')
        admin.execute(f'GRANT SELECT ON "{schema}".speech_pronunciation_revisions TO "{reader}"')
        admin.execute(f'GRANT SELECT ON "{schema}".speech_pronunciation_families TO "{reader}"')
        admin.execute(f'GRANT SELECT ON "{schema}".speech_publications TO "{reader}"')
        for grant in pronunciation_draft_grants(schema, writer):
            admin.execute(grant)
        for grant in pronunciation_publication_grants(schema, publisher):
            admin.execute(grant)
        settings = Settings(
            **{
                **_settings().model_dump(),
                "database_schema": schema,
                "pronunciation_reader_database_url": dsn(url, reader, reader_password),
                "pronunciation_read_concurrency": 1,
                "pronunciation_read_timeout_seconds": 1,
                "pronunciation_writer_database_url": dsn(url, writer, writer_password),
                "pronunciation_publisher_database_url": dsn(url, publisher, publisher_password),
                "pronunciation_write_concurrency": 4,
                "pronunciation_transaction_timeout_seconds": 2,
                "pronunciation_receive_max_bytes": MAX_BYTES,
                "asset_allowed_hosts": ("testserver", "test"),
            }
        )
        yield Sandbox(url, schema, reader, writer, publisher, admin, settings)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in (publisher, writer, reader):
            admin.execute(f'DROP OWNED BY "{role}"')
            admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def check_read(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text
    validate_read_response(
        READ_OPERATION, status, {"body": response.json(), "headers": dict(response.headers)}
    )


def check(response: httpx.Response, status: int, operation: str) -> None:
    assert response.status_code == status, response.text
    validate_response(
        operation, status, {"body": response.json(), "headers": dict(response.headers)}
    )


def create_body(**changes: Any) -> dict[str, Any]:
    return pronunciation_request(**changes)


async def test_empty_list_draft_publish_replay_and_status_semantics(sandbox: Sandbox) -> None:
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
        check(replay, 200, CREATE_OPERATION)
        assert replay.json() == first
        assert db.counts() == before

        conflict = await http.post(
            PATH, json=create_body(encoding="ipa"), headers={"Idempotency-Key": KEY}
        )
        check(conflict, 409, CREATE_OPERATION)
        assert conflict.json()["error"]["code"] == "pronunciation_idempotency_conflict"
        assert db.counts() == before

        # Omission selects the declared publication default and writes the
        # revision, the ledger row and the selected head together.
        body = create_body()
        body.pop("publish")
        published = await http.post(PATH, json=body, headers={"Idempotency-Key": KEY + "-publish"})
        check(published, 201, CREATE_OPERATION)
        published_body = published.json()
        assert published_body["status"] == "published"
        published_family = published_body["id"]
        ledger = db.published()
        assert [(row["object_id"], row["revision"]) for row in ledger] == [(published_family, "1")]
        assert ledger[0]["revision_id"].startswith("prv-")
        assert db.publication_heads() == [{"object_id": published_family, "revision": "1"}]
        assert db.catalog_revision() >= 1

        fixed = await http.get(f"{PATH}/{published_family}/revisions/1")
        check_read(fixed, 200)
        assert fixed.json()["status"] == "published"

        # A draft above a published revision lists as draft; the older revision
        # keeps its own published fact and stays readable.
        above = await http.post(
            PATH,
            json=create_body(id=published_family, expected_revision="1", encoding="ipa"),
            headers={"Idempotency-Key": KEY + "-above"},
        )
        check(above, 201, CREATE_OPERATION)
        assert above.json()["status"] == "draft" and above.json()["revision"] == "2"
        assert [(row["object_id"], row["revision"]) for row in db.published()] == [
            (published_family, "1")
        ]
        assert db.publication_heads() == [{"object_id": published_family, "revision": "1"}]
        pure = await http.post(
            PATH, json=create_body(publish=True), headers={"Idempotency-Key": KEY + "-pure"}
        )
        check(pure, 201, CREATE_OPERATION)
        pure_family = pure.json()["id"]
        assert pure.json()["status"] == "published"

        older = await http.get(f"{PATH}/{published_family}/revisions/1")
        check_read(older, 200)
        assert older.json()["status"] == "published"
        newer = await http.get(f"{PATH}/{published_family}/revisions/2")
        check_read(newer, 200)
        assert newer.json()["status"] == "draft"

        listed = await http.get(PATH)
        check(listed, 200, LIST_OPERATION)
        by_id = {item["id"]: item for item in listed.json()["data"]}
        assert by_id[family]["status"] == "draft" and by_id[family]["head_revision"] == "2"
        assert by_id[published_family]["status"] == "draft"
        assert by_id[published_family]["head_revision"] == "2"
        assert by_id[pure_family]["status"] == "published"
        assert by_id[pure_family]["head_revision"] == "1"

        oversized = await http.post(
            PATH,
            json=create_body(entries=[{"grapheme": "synthetic", "phonemes": "x"}] * 200),
            headers={"Idempotency-Key": KEY + "-large"},
        )
        check(oversized, 400, CREATE_OPERATION)
        assert oversized.json()["error"]["code"] == "pronunciation_invalid_request"


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


async def test_repeated_publish_of_one_key_is_a_200_replay_with_one_ledger_row(
    sandbox: Sandbox,
) -> None:
    db = sandbox
    app = db.app()
    async with app.router.lifespan_context(app), client(app) as http:
        first = await http.post(
            PATH, json=create_body(publish=True), headers={"Idempotency-Key": KEY}
        )
        check(first, 201, CREATE_OPERATION)
        family = first.json()["id"]
        ledger = db.published()
        assert [(row["object_id"], row["revision"]) for row in ledger] == [(family, "1")]

        replay = await http.post(
            PATH, json=create_body(publish=True), headers={"Idempotency-Key": KEY}
        )
        check(replay, 200, CREATE_OPERATION)
        assert replay.json() == first.json()
        assert db.published() == ledger
        assert db.publication_heads() == [{"object_id": family, "revision": "1"}]
        assert db.counts() == (1, 1)


async def test_concurrent_double_publish_has_exactly_one_winner(sandbox: Sandbox) -> None:
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
                json=create_body(id=family, expected_revision="1", encoding="ipa", publish=True),
                headers={"Idempotency-Key": KEY + "-left"},
            ),
            http.post(
                PATH,
                json=create_body(
                    id=family,
                    expected_revision="1",
                    entries=[{"grapheme": "other", "phonemes": "o th er"}],
                    publish=True,
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
        ledger = db.published()
        assert len(ledger) == 1
        assert (ledger[0]["object_id"], ledger[0]["revision"]) == (family, "2")
        assert db.publication_heads() == [{"object_id": family, "revision": "2"}]
        assert db.counts() == (1, 2)


async def test_publication_failure_rolls_back_revision_ledger_and_head(sandbox: Sandbox) -> None:
    db = sandbox
    db.admin.execute(
        f'CREATE FUNCTION "{db.schema}".reject_publication_head() RETURNS trigger '
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic publication head failure'; END $$"
    )
    db.admin.execute(
        f"CREATE TRIGGER reject_publication_head BEFORE INSERT ON "
        f'"{db.schema}".speech_publication_heads FOR EACH ROW '
        f'EXECUTE FUNCTION "{db.schema}".reject_publication_head()'
    )
    try:
        app = db.app()
        async with app.router.lifespan_context(app), client(app) as http:
            failed = await http.post(
                PATH,
                json=create_body(
                    publish=True, entries=[{"grapheme": "synthetic", "phonemes": "x"}]
                ),
                headers={"Idempotency-Key": KEY + "-atomic"},
            )
            check(failed, 503, CREATE_OPERATION)
            assert failed.json()["error"]["code"] == "pronunciation_unavailable"
            assert db.counts() == (0, 0)
            assert db.published() == []
            assert db.publication_heads() == []
            assert db.query("SELECT count(*) AS n FROM speech_pronunciation_commands")[0]["n"] == 0
    finally:
        db.admin.execute(
            f"DROP TRIGGER IF EXISTS reject_publication_head ON "
            f'"{db.schema}".speech_publication_heads'
        )
        db.admin.execute(f'DROP FUNCTION IF EXISTS "{db.schema}".reject_publication_head()')

    # The failed command left no pending row, so the same key is a fresh attempt
    # and now commits all three facts together.
    app = db.app()
    async with app.router.lifespan_context(app), client(app) as http:
        recovered = await http.post(
            PATH,
            json=create_body(publish=True, entries=[{"grapheme": "synthetic", "phonemes": "x"}]),
            headers={"Idempotency-Key": KEY + "-atomic"},
        )
        check(recovered, 201, CREATE_OPERATION)
        assert recovered.json()["status"] == "published"
        assert len(db.published()) == 1
        assert db.publication_heads() == [{"object_id": recovered.json()["id"], "revision": "1"}]


def test_publisher_role_is_append_only_and_cannot_delete_or_write_catalogue(
    sandbox: Sandbox,
) -> None:
    db = sandbox
    with psycopg.connect(
        dsn(db.url, db.publisher, "synthetic-sets-publisher"), autocommit=True
    ) as connection:
        connection.execute(f'SET search_path TO "{db.schema}",pg_catalog')
        for statement in (
            "DELETE FROM speech_publications",
            "DELETE FROM speech_publication_heads",
            "UPDATE speech_publications SET revision_id='prv-synthetic'",
            "INSERT INTO speech_catalog_state (singleton) VALUES (TRUE)",
            "UPDATE speech_pronunciation_revisions SET definition_json='{}'",
            "CREATE TRIGGER synthetic BEFORE INSERT ON speech_publications "
            "FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)


def test_reader_missing_publications_select_is_rejected_at_startup(sandbox: Sandbox) -> None:
    db = sandbox
    suffix = uuid.uuid4().hex[:8]
    narrow = "sn_sets_narrow_" + suffix
    db.admin.execute(f"CREATE ROLE \"{narrow}\" LOGIN PASSWORD 'synthetic-narrow'")
    try:
        db.admin.execute(f'GRANT USAGE ON SCHEMA "{db.schema}" TO "{narrow}"')
        db.admin.execute(
            f'GRANT SELECT ON "{db.schema}".speech_pronunciation_revisions TO "{narrow}"'
        )
        db.admin.execute(
            f'GRANT SELECT ON "{db.schema}".speech_pronunciation_families TO "{narrow}"'
        )
        app = db.app(
            pronunciation_reader_database_url=SecretStr(dsn(db.url, narrow, "synthetic-narrow"))
        )
        with (
            pytest.raises(ValueError, match="Pronunciation reader startup rejected"),
            TestClient(app),
        ):
            pytest.fail("Reader without publication SELECT was accepted")
        assert app.state.pronunciation_runtime.provider is None
    finally:
        db.admin.execute(f'DROP OWNED BY "{narrow}"')
        db.admin.execute(f'DROP ROLE IF EXISTS "{narrow}"')


def test_reader_column_insert_on_the_read_only_ledger_is_rejected_at_startup(
    sandbox: Sandbox,
) -> None:
    db = sandbox
    db.admin.execute(
        f'GRANT INSERT (published_at_us) ON "{db.schema}".speech_publications TO "{db.reader}"'
    )
    app = db.app()
    with (
        pytest.raises(ValueError, match="Pronunciation reader startup rejected"),
        TestClient(app),
    ):
        pytest.fail("Reader with a column INSERT on the ledger was accepted")
    assert app.state.pronunciation_runtime.provider is None


def test_publisher_with_delete_bearing_shared_grants_is_rejected_at_startup(
    sandbox: Sandbox,
) -> None:
    db = sandbox
    suffix = uuid.uuid4().hex[:8]
    broad = "sn_sets_broad_" + suffix
    db.admin.execute(f"CREATE ROLE \"{broad}\" LOGIN PASSWORD 'synthetic-broad'")
    try:
        for grant in publication_grants(db.schema, broad):
            db.admin.execute(grant)
        app = db.app(
            pronunciation_publisher_database_url=SecretStr(dsn(db.url, broad, "synthetic-broad"))
        )
        with (
            pytest.raises(ValueError, match="Pronunciation publisher startup rejected"),
            TestClient(app),
        ):
            pytest.fail("Publisher with the shared DELETE-bearing grants was accepted")
        assert app.state.pronunciation_publisher_runtime.provider is None
    finally:
        db.admin.execute(f'DROP OWNED BY "{broad}"')
        db.admin.execute(f'DROP ROLE IF EXISTS "{broad}"')


def test_publisher_with_delete_only_on_the_selected_head_is_rejected_at_startup(
    sandbox: Sandbox,
) -> None:
    db = sandbox
    db.admin.execute(f'GRANT DELETE ON "{db.schema}".speech_publication_heads TO "{db.publisher}"')
    app = db.app()
    with (
        pytest.raises(ValueError, match="Pronunciation publisher startup rejected"),
        TestClient(app),
    ):
        pytest.fail("Publisher with DELETE on the selected head was accepted")
    assert app.state.pronunciation_publisher_runtime.provider is None


def test_publisher_extra_column_update_is_rejected_at_startup(sandbox: Sandbox) -> None:
    db = sandbox
    db.admin.execute(
        f'GRANT UPDATE (definition_json) ON "{db.schema}".speech_pronunciation_revisions '
        f'TO "{db.publisher}"'
    )
    app = db.app()
    with (
        pytest.raises(ValueError, match="Pronunciation publisher startup rejected"),
        TestClient(app),
    ):
        pytest.fail("Publisher with a revision UPDATE was accepted")
    assert app.state.pronunciation_publisher_runtime.provider is None


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


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_0045_names_a_bad_historical_row_and_rolls_back_its_columns(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0044_voice_preview_jobs")
    sandbox.admin.execute(
        "INSERT INTO speech_pronunciation_families (pronunciation_id) VALUES ('pron-bad')"
    )
    sandbox.admin.execute(
        "INSERT INTO speech_pronunciation_revisions "
        "(pronunciation_id, revision, definition_json, definition_digest, key_digest, "
        "request_digest, created_at_us) VALUES ('pron-bad', '1', 'not json', %s, %s, %s, 1)",
        ("sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64),
    )
    with pytest.raises(ProgrammingError) as failure:
        sandbox.upgrade("0045_pronunciation_set_summary")
    message = str(failure.value)
    assert "refusing to constrain speech_pronunciation_revisions" in message
    assert "cannot derive encoding/entry_count" in message
    assert sandbox.version() == "0044_voice_preview_jobs"
    columns = sandbox.admin.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'speech_pronunciation_revisions'",
        (sandbox.schema,),
    ).fetchall()
    names = {row["column_name"] for row in columns}
    assert "encoding" not in names
    assert "entry_count" not in names
