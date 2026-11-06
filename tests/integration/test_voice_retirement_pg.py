"""A voice retirement hides the revision while its exact publication stays readable."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime
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
from speechnexus_contracts.voice_retirement_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import (
    RETIREMENT_TABLE,
)
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    EVENT_TABLE,
    HEAD_TABLE,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
)
from speechnexus_backend.speech.infrastructure.voice_catalogue_schema import (
    voice_catalogue_reader_grants,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "synthetic_voice_retirement_test"
DOCUMENT = next(
    example["value"]["body"]
    for example in json.loads(
        (ROOT / "tests/fixtures/contracts/voice-catalogue-http.json").read_text()
    )["examples"]
    if example["id"] == "published"
)
PATH = "/v1/nexus/voices/voice-demo-1/revisions/{revision}/retirements"


class RetirementDB:
    def __init__(
        self,
        owner: psycopg.Connection[dict[str, Any]],
        schema: str,
        reader_dsn: str,
        lifecycle_dsn: str,
    ) -> None:
        self.owner, self.schema = owner, schema
        self.reader_dsn, self.lifecycle_dsn = reader_dsn, lifecycle_dsn

    def settings(self) -> Settings:
        return _settings().model_copy(
            update={
                "database_schema": self.schema,
                "model_catalog_reader_database_url": SecretStr(self.reader_dsn),
                "binding_lifecycle_database_url": SecretStr(self.lifecycle_dsn),
            }
        )

    def publish(self, voice: str = "voice-demo-1", revision: str = "2") -> dict[str, Any]:
        doc: dict[str, Any] = deepcopy(DOCUMENT)
        doc.update(voice_id=voice, revision=revision)
        doc["source"] = {
            "type": "builtin",
            "draft_id": None,
            "draft_revision": None,
            "source_asset_digest": None,
        }
        identity = voice + "-" + revision
        self.owner.execute(
            "INSERT INTO speech_builtin_voice_source_approvals "
            "(source_approval_id,voice_id,source_revision,source_digest,"
            "native_speaker_id,expires_at) "
            "VALUES (%s,%s,'synthetic-source','synthetic-digest','Vivian',CURRENT_TIMESTAMP)",
            (identity, voice),
        )
        self.owner.execute(
            "INSERT INTO speech_builtin_voice_approvals "
            "(approval_id,voice_id,revision,revision_id,binding_revision_id,source_approval_id,"
            "facts,expires_at) VALUES (%s,%s,%s,%s,'binding-rev-1',%s,"
            '\'{"private_fact":"synthetic-never-public"}\',CURRENT_TIMESTAMP)',
            (identity, voice, revision, identity, identity),
        )
        self.owner.execute(
            "INSERT INTO speech_builtin_voice_qualifications VALUES "
            "(%s,%s,%s,'binding-rev-1',%s,%s::jsonb,'Vivian')",
            (voice, revision, identity, identity, json.dumps(doc)),
        )
        timestamp = (
            int(datetime.fromisoformat(doc["published_at"].replace("Z", "+00:00")).timestamp())
            * 1_000_000
        )
        self.owner.execute(
            "INSERT INTO speech_publications VALUES ('voice',%s,%s,%s,%s)",
            (voice, revision, identity, timestamp),
        )
        self.owner.execute(
            "INSERT INTO speech_publication_heads VALUES ('voice',%s,%s,%s,%s) "
            "ON CONFLICT (kind,object_id) DO UPDATE SET revision=EXCLUDED.revision, "
            "revision_id=EXCLUDED.revision_id,updated_at_us=EXCLUDED.updated_at_us",
            (voice, revision, identity, timestamp),
        )
        return doc

    def retirements(self) -> list[dict[str, Any]]:
        return self.owner.execute(
            f"SELECT mode, reason FROM {RETIREMENT_TABLE} "
            "WHERE kind='voice' AND object_id='voice-demo-1' ORDER BY revision"
        ).fetchall()


@pytest.fixture
def retirement_db() -> Iterator[RetirementDB]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not retirement evidence")
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_retire_" + suffix
    reader, lifecycle = "sn_retire_reader_" + suffix, "sn_retire_writer_" + suffix
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
        for role in (reader, lifecycle):
            owner.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in (
            *model_catalog_reader_grants(schema, reader),
            *voice_catalogue_reader_grants(schema, reader),
        ):
            owner.execute(grant)
        from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
            binding_lifecycle_grants,
        )

        for grant in binding_lifecycle_grants(schema, lifecycle):
            owner.execute(grant)
        yield RetirementDB(
            owner,
            schema,
            make_conninfo(url, user=reader, password=PASSWORD),
            make_conninfo(url, user=lifecycle, password=PASSWORD),
        )
    finally:
        owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in (reader, lifecycle):
            owner.execute(f'DROP ROLE IF EXISTS "{role}"')
        owner.close()
        engine.dispose()


async def _client(db: RetirementDB) -> httpx.AsyncClient:
    app = create_app(settings=db.settings())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://speech.test")


async def test_disable_is_rejected_without_writing_a_tombstone(
    retirement_db: RetirementDB,
) -> None:
    retirement_db.publish()
    app = create_app(settings=retirement_db.settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://speech.test"
        ) as client,
    ):
        rejected = await client.post(
            PATH.format(revision="2"),
            json={"mode": "disable", "reason": "emergency"},
            headers={"X-Request-ID": "req-disable-490"},
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "voice_disable_unsupported"
        validate_response("retireVoiceRevision", 422, {"body": rejected.json()})
        still = await client.get("/v1/nexus/voices/voice-demo-1/revisions/2")
        assert still.status_code == 200
        assert still.json()["status"] == "published"
    assert retirement_db.retirements() == []


async def test_retire_hides_new_references_and_replays(
    retirement_db: RetirementDB,
) -> None:
    retirement_db.publish()
    app = create_app(settings=retirement_db.settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://speech.test"
        ) as client,
    ):
        first = await client.post(
            PATH.format(revision="2"),
            json={"mode": "retire"},
            headers={"X-Request-ID": "req-retire-490"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "retired"
        assert first.json()["disabled_reason"] is None
        validate_response("retireVoiceRevision", 200, {"body": first.json()})

        page = await client.get("/v1/nexus/voices")
        assert page.status_code == 200 and page.json()["data"] == []
        fixed = await client.get("/v1/nexus/voices/voice-demo-1/revisions/2")
        assert fixed.status_code == 200 and fixed.json() == first.json()

        replay = await client.post(
            PATH.format(revision="2"),
            json={"mode": "retire"},
            headers={"X-Request-ID": "req-retire-490"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()

        conflict = await client.post(
            PATH.format(revision="2"),
            json={"mode": "retire", "reason": "changed intent"},
            headers={"X-Request-ID": "req-retire-490"},
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "voice_retirement_conflict"

        for path in ("voice-unknown/revisions/2", "voice-demo-1/revisions/99"):
            missing = await client.post(
                "/v1/nexus/voices/" + path + "/retirements",
                json={"mode": "retire"},
                headers={"X-Request-ID": "req-retire-490"},
            )
            assert missing.status_code == 404, missing.text
            assert missing.json()["error"]["code"] == "voice_not_found"
    assert retirement_db.retirements() == [{"mode": "retire", "reason": None}]


async def test_a_superseded_revision_cannot_be_retired(
    retirement_db: RetirementDB,
) -> None:
    retirement_db.publish(revision="2")
    retirement_db.publish(revision="3")
    app = create_app(settings=retirement_db.settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://speech.test"
        ) as client,
    ):
        rejected = await client.post(
            PATH.format(revision="2"),
            json={"mode": "retire"},
            headers={"X-Request-ID": "req-retire-490"},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "voice_retirement_conflict"
        current = await client.post(
            PATH.format(revision="3"),
            json={"mode": "retire"},
            headers={"X-Request-ID": "req-retire-490"},
        )
        assert current.status_code == 200, current.text
    assert retirement_db.retirements() == [{"mode": "retire", "reason": None}]


def test_lifecycle_writer_grants_keep_the_binding_lifecycle_surface(
    retirement_db: RetirementDB,
) -> None:
    # The reused writer identity must still pass the existing lifecycle startup
    # verification; a new grant would silently widen its authority.
    with psycopg.connect(retirement_db.lifecycle_dsn, autocommit=True) as writer:
        for statement in (
            f'SELECT count(*) FROM "{retirement_db.schema}".' + RETIREMENT_TABLE,
            f'SELECT count(*) FROM "{retirement_db.schema}".' + HEAD_TABLE,
            f'SELECT count(*) FROM "{retirement_db.schema}".' + EVENT_TABLE,
        ):
            writer.execute(statement)
