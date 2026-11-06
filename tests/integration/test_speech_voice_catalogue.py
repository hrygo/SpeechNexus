"""Existing publication authority -> real API lifespan -> unchanged workbench consumer."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from speechnexus_contracts.voice_catalogue_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.model_catalog_runtime import ModelCatalogRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.publication_schema import model_catalog_reader_grants
from speechnexus_backend.speech.infrastructure.voice_catalogue_schema import (
    voice_catalogue_reader_grants,
)
from speechnexus_backend.workbench.application.voices import WorkbenchVoices
from speechnexus_backend.workbench.infrastructure.public_voice_catalogue import PublicVoiceCatalogue
from speechnexus_backend.workbench.interfaces.http_voices import create_voice_router

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "synthetic_voice_catalogue_test"
DOCUMENT = next(
    example["value"]["body"]
    for example in json.loads(
        (ROOT / "tests/fixtures/contracts/voice-catalogue-http.json").read_text()
    )["examples"]
    if example["id"] == "published"
)


@dataclass
class CatalogueDB:
    owner: psycopg.Connection[dict[str, Any]]
    schema: str
    role: str
    reader_dsn: str

    def settings(self) -> Settings:
        return _settings().model_copy(
            update={
                "database_schema": self.schema,
                "model_catalog_reader_database_url": SecretStr(self.reader_dsn),
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
        validate_response("getVoiceRevision", 200, {"body": doc})
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


@pytest.fixture
def catalogue_db() -> Iterator[CatalogueDB]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("project-isolated PostgreSQL required; skip is not evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_voices_" + suffix, "sn_voices_reader_" + suffix
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
        owner.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in (
            *model_catalog_reader_grants(schema, role),
            *voice_catalogue_reader_grants(schema, role),
        ):
            owner.execute(grant)
        yield CatalogueDB(owner, schema, role, make_conninfo(url, user=role, password=PASSWORD))
    finally:
        owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        owner.execute(f'DROP ROLE IF EXISTS "{role}"')
        owner.close()
        engine.dispose()


async def test_published_reads_and_unchanged_workbench_take_the_same_version(
    catalogue_db: CatalogueDB,
) -> None:
    old = catalogue_db.publish(revision="1")
    doc = catalogue_db.publish()
    catalogue_db.publish("voice-demo-2")
    api = create_app(settings=catalogue_db.settings())
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://speech.test"
        ) as client,
    ):
        page = await client.get("/v1/nexus/voices", params={"limit": 1})
        assert page.status_code == 200, page.text
        validate_response("listVoices", 200, {"body": page.json()})
        assert page.json()["data"][0]["revision"] == "2"
        assert page.json()["has_more"] is True
        tail = await client.get("/v1/nexus/voices", params={"cursor": page.json()["next_cursor"]})
        assert [item["id"] for item in tail.json()["data"]] == ["voice-demo-2"]
        assert tail.json()["has_more"] is False and tail.json()["next_cursor"] is None
        empty = await client.get("/v1/nexus/voices", params={"language": "fr-FR"})
        assert empty.status_code == 200 and empty.json()["data"] == []
        for revision, expected in (("1", old), ("2", doc)):
            fixed = await client.get(f"/v1/nexus/voices/voice-demo-1/revisions/{revision}")
            assert fixed.status_code == 200, fixed.text
            assert fixed.json() == expected
        for path in ("voice-unknown/revisions/2", "voice-demo-1/revisions/99"):
            missing = await client.get("/v1/nexus/voices/" + path)
            assert missing.status_code == 404
            assert missing.headers["x-should-retry"] == "false"
            validate_response("getVoiceRevision", 404, {"body": missing.json()})

        workbench = FastAPI()
        workbench.include_router(
            create_voice_router(
                WorkbenchVoices(PublicVoiceCatalogue(client)),
                default_workspace_id="workspace-test",
                max_json_bytes=65536,
                body_timeout_seconds=2,
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workbench), base_url="http://workbench.test"
        ) as consumer:
            listed = await consumer.get("/v1/workbench/voices")
            fixed = await consumer.get("/v2/workbench/voices/voice-demo-1/revisions/2")
            assert listed.status_code == fixed.status_code == 200, (listed.text, fixed.text)
            assert fixed.json()["revision"] == "2"
            assert fixed.json()["model_binding"] == doc["model_binding"]

        with psycopg.connect(catalogue_db.reader_dsn, autocommit=True) as reader:
            for table in ("speech_builtin_voice_qualifications", "speech_builtin_voice_approvals"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    reader.execute(f'SELECT * FROM "{catalogue_db.schema}".{table}')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute(f'DELETE FROM "{catalogue_db.schema}".speech_publications')


async def test_no_published_document_is_retryable(catalogue_db: CatalogueDB) -> None:
    api = create_app(settings=catalogue_db.settings())
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://speech.test"
        ) as client,
    ):
        for path in ("/v1/nexus/voices", "/v1/nexus/voices/voice-demo-1/revisions/2"):
            response = await client.get(path)
            assert response.status_code == 503, response.text
            assert response.json()["error"]["retryable"] is True


@pytest.mark.parametrize("mode", ["retire", "disable"])
async def test_retirement_hides_head_and_preserves_fixed_history(
    catalogue_db: CatalogueDB,
    mode: str,
) -> None:
    catalogue_db.publish()
    catalogue_db.owner.execute(
        "INSERT INTO speech_publication_retirements VALUES "
        "('voice','voice-demo-1','2','voice-demo-1-2',1793001600000000,%s,%s)",
        (mode, "synthetic-disabled" if mode == "disable" else None),
    )
    api = create_app(settings=catalogue_db.settings())
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://speech.test"
        ) as client,
    ):
        page = await client.get("/v1/nexus/voices")
        assert page.status_code == 200 and page.json()["data"] == []
        fixed = await client.get("/v1/nexus/voices/voice-demo-1/revisions/2")
        assert fixed.status_code == 200
        assert fixed.json()["status"] == ("disabled" if mode == "disable" else "retired")
        validate_response("getVoiceRevision", 200, {"body": fixed.json()})


def test_reader_login_has_password_even_with_trusted_local_auth(catalogue_db: CatalogueDB) -> None:
    # Local trust authentication must not hide an unusable CI reader identity.
    row = catalogue_db.owner.execute(
        "SELECT rolcanlogin AND rolpassword IS NOT NULL AS password_login "
        "FROM pg_catalog.pg_authid WHERE rolname=%s",
        (catalogue_db.role,),
    ).fetchone()
    assert row is not None and row["password_login"]


def test_private_qualification_grant_is_rejected_at_startup(catalogue_db: CatalogueDB) -> None:
    # Prove this identity can open before attributing rejection to its extra grant.
    runtime = ModelCatalogRuntime(catalogue_db.settings())
    try:
        runtime.open()
        assert runtime.voices is not None
    finally:
        runtime.close()
    catalogue_db.owner.execute(
        f'GRANT SELECT ON speech_builtin_voice_qualifications TO "{catalogue_db.role}"'
    )
    runtime = ModelCatalogRuntime(catalogue_db.settings())
    try:
        with pytest.raises(ValueError, match="startup rejected"):
            runtime.open()
        assert runtime.voices is None
    finally:
        runtime.close()


@pytest.mark.parametrize("fault", ["private-field", "wrong-identity", "wrong-time"])
async def test_corrupt_public_document_fails_closed(catalogue_db: CatalogueDB, fault: str) -> None:
    doc = catalogue_db.publish()
    if fault == "private-field":
        doc["private_endpoint"] = "synthetic-never-public"
    elif fault == "wrong-identity":
        doc["voice_id"] = "voice-other-1"
    else:
        doc["published_at"] = "2020-01-01T00:00:00Z"
    catalogue_db.owner.execute(
        "UPDATE speech_builtin_voice_qualifications SET document=%s::jsonb",
        (json.dumps(doc),),
    )
    api = create_app(settings=catalogue_db.settings())
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://speech.test"
        ) as client,
    ):
        for path in ("/v1/nexus/voices", "/v1/nexus/voices/voice-demo-1/revisions/2"):
            response = await client.get(path)
            assert response.status_code == 503, response.text
            assert "synthetic-never-public" not in response.text
            assert "voice-other-1" not in response.text


async def test_unpublished_qualification_is_invisible(catalogue_db: CatalogueDB) -> None:
    catalogue_db.publish()
    catalogue_db.publish("voice-unpublished")
    catalogue_db.owner.execute(
        "DELETE FROM speech_publication_heads WHERE object_id='voice-unpublished'"
    )
    catalogue_db.owner.execute(
        "DELETE FROM speech_publications WHERE object_id='voice-unpublished'"
    )
    api = create_app(settings=catalogue_db.settings())
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://speech.test"
        ) as client,
    ):
        page = await client.get("/v1/nexus/voices")
        assert [item["id"] for item in page.json()["data"]] == ["voice-demo-1"]
        missing = await client.get("/v1/nexus/voices/voice-unpublished/revisions/2")
        assert missing.status_code == 404


async def test_existing_model_only_reader_keeps_voices_unavailable(
    catalogue_db: CatalogueDB,
) -> None:
    catalogue_db.publish()
    catalogue_db.owner.execute(
        f'REVOKE SELECT ON speech_published_voice_revisions FROM "{catalogue_db.role}"'
    )
    api = create_app(settings=catalogue_db.settings())
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://speech.test"
        ) as client,
    ):
        response = await client.get("/v1/nexus/voices")
        assert response.status_code == 503 and response.json()["error"]["retryable"]
        models = await client.get("/v1/models")
        assert models.status_code == 200
