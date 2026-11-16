"""Published bindings must name themselves; the catalogue proves it on real rows.

The reported defect was a production path that accepted and published a binding
definition without the ``binding_id`` its own contract requires, so ``GET
/v1/models`` answered 503 after a documented create → publish → activate run.
These checks drive the real HTTP providers, the real migrations and a real
PostgreSQL role: the stored document, the publish refusal and the catalogue read
are one chain, not a unit fixture.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator, FormatChecker
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from referencing import Registry
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.binding_examples import binding_request
from tests.support.contracts import ROOT as CONTRACTS
from tests.support.contracts import document, retrieve
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.binding_documents import (
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import PostgresBindingDrafts
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
)
from speechnexus_backend.speech.infrastructure.model_catalog_postgres import PostgresModelCatalog
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    binding_lifecycle_grants,
    binding_publication_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_catalogue_identity_test"
CREATE_URL = "/v1/nexus/admin/model-bindings"
MODEL = "asr-quality"
PUBLISH_URL = CREATE_URL + f"/{MODEL}/publications"
ACTIVATE_URL = CREATE_URL + f"/{MODEL}/revisions/1/activations"

_SPEECH_OPENAPI = document(CONTRACTS / "speech-openapi.yaml")


@dataclass
class Sandbox:
    pool: SpeechPool
    admin: psycopg.Connection[tuple[object, ...]]
    schema: str
    role: str
    url: str
    drafts: PostgresBindingDrafts
    lifecycle: PostgresBindingLifecycle
    catalogue: PostgresModelCatalog


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_catid_" + suffix, "sn_catid_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    admin = psycopg.connect(url, autocommit=True)
    pool: SpeechPool | None = None
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in (
            *binding_draft_grants(schema, role),
            *binding_lifecycle_grants(schema, role),
            *binding_publication_grants(schema, role),
            *model_catalog_reader_grants(schema, role),
        ):
            admin.execute(grant)
        pool = speech_pool(
            make_conninfo(url, user=role, password=PASSWORD),
            schema=schema,
            max_size=2,
            name="catalogue-identity-" + suffix,
        )
        pool.open()
        yield Sandbox(
            pool=pool,
            admin=admin,
            schema=schema,
            role=role,
            url=url,
            drafts=PostgresBindingDrafts(pool, timeout_seconds=3),
            lifecycle=PostgresBindingLifecycle(pool, timeout_seconds=3),
            catalogue=PostgresModelCatalog(pool, timeout_seconds=3),
        )
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()
        engine.dispose()


def _http(sandbox: Sandbox) -> AsyncClient:
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",)}),
        bindings=sandbox.drafts,
        binding_lifecycle=sandbox.lifecycle,
        catalogue=sandbox.catalogue,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _create_body() -> dict[str, Any]:
    body = binding_request()
    return {**body, "binding_id": MODEL, "logical_model": MODEL}


PUBLISH_BODY = {
    "expected_revision": "1",
    "expected_current_revision": None,
    "capacity_manifest_id": "cap-asr-quality-7",
    "qualification_report_id": "qrep-asr-quality-1",
}


def _stored_definition(sandbox: Sandbox, revision: str) -> dict[str, Any]:
    row = sandbox.admin.execute(
        "SELECT definition_json FROM speech_binding_revisions "
        "WHERE binding_id = %s AND revision = %s",
        (MODEL, revision),
    ).fetchone()
    assert row is not None
    return decode_document(str(row[0]).encode("utf-8"))


def _insert_legacy_revision(sandbox: Sandbox, revision: str) -> dict[str, Any]:
    """A row exactly as the pre-fix writer stored it: identity only in columns."""
    legacy = _create_body()
    legacy.pop("binding_id")
    key = "sha256:" + ("7" + revision).ljust(64, "7")
    request = "sha256:" + ("8" + revision).ljust(64, "8")
    definition = encode_document(legacy).decode("utf-8")
    sandbox.admin.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, %s, 1, %s, %s)",
        (key, MODEL, "{}", request),
    )
    sandbox.admin.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, "
        " definition_json, definition_digest, request_digest, created_at_us) "
        "VALUES (%s, %s, %s, %s, 1, %s, %s, %s, 1700000000000000)",
        (MODEL, revision, "binding-rev-" + revision, key, definition, key, request),
    )
    sandbox.admin.execute(
        "UPDATE speech_binding_families SET latest_revision = %s WHERE binding_id = %s",
        (revision, MODEL),
    )
    return legacy


def _models(sandbox: Sandbox, body: dict[str, Any]) -> dict[str, Any]:
    schema = {"$ref": "#/components/schemas/ModelList", **_SPEECH_OPENAPI}
    Draft202012Validator(
        schema,
        registry=Registry(retrieve=retrieve),  # type: ignore[call-arg]
        format_checker=FormatChecker(),
    ).validate(body)
    return body


async def test_create_publish_activate_then_the_catalogue_lists_the_binding(
    sandbox: Sandbox,
) -> None:
    async with _http(sandbox) as http:
        created = await http.post(
            CREATE_URL, json=_create_body(), headers={"Idempotency-Key": "catalogue-identity-1"}
        )
        assert created.status_code == 201, created.text
        revision_id = created.json()["revision_id"]

        # The write path stores the identity the published revision exposes; the
        # pre-fix writer stored the request minus binding_id and the catalogue
        # then refused the whole list.
        stored = _stored_definition(sandbox, "1")
        assert stored["binding_id"] == MODEL
        from speechnexus_contracts.admin import parse as parse_admin

        parse_admin("ModelBindingCreateRequest", stored)

        published = await http.post(
            PUBLISH_URL, json=PUBLISH_BODY, headers={"Idempotency-Key": "catalogue-identity-2"}
        )
        assert published.status_code == 201, published.text
        assert published.json()["binding_id"] == MODEL

        activated = await http.post(
            ACTIVATE_URL,
            json={"expected_current_revision": "1"},
            headers={"Idempotency-Key": "catalogue-identity-3"},
        )
        assert activated.status_code == 200, activated.text

        listed = await http.get("/v1/models")
        assert listed.status_code == 200, listed.text
        body = _models(sandbox, listed.json())

    assert [entry["id"] for entry in body["data"]] == [MODEL]
    entry = body["data"][0]
    assert entry["object"] == "model"
    assert entry["nexus"]["binding"] == {"id": MODEL, "revision": "1"}
    assert entry["nexus"]["logical_model"] == MODEL
    assert entry["nexus"]["readiness"]["admission_state"] == "unavailable"
    # The public entry names the exact immutable revision the activation selected.
    head = sandbox.admin.execute(
        "SELECT revision_id FROM speech_publication_heads "
        "WHERE kind = 'binding' AND object_id = %s",
        (MODEL,),
    ).fetchone()
    assert head == (revision_id,)


async def test_a_stored_document_without_identity_is_refused_at_publish(
    sandbox: Sandbox,
) -> None:
    async with _http(sandbox) as http:
        created = await http.post(
            CREATE_URL, json=_create_body(), headers={"Idempotency-Key": "catalogue-legacy-1"}
        )
        assert created.status_code == 201, created.text
        published = await http.post(
            PUBLISH_URL, json=PUBLISH_BODY, headers={"Idempotency-Key": "catalogue-legacy-2"}
        )
        assert published.status_code == 201, published.text
    # A row written before the fix retains identity only in columns.
    legacy = _insert_legacy_revision(sandbox, "2")

    async with _http(sandbox) as http:
        refused = await http.post(
            PUBLISH_URL,
            json={
                **PUBLISH_BODY,
                "expected_revision": "2",
                "expected_current_revision": "1",
            },
            headers={"Idempotency-Key": "catalogue-legacy-3"},
        )
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "invalid_binding_definition"
        assert error["param"] == "binding_id"
        assert error["retryable"] is False
        # Nothing was promoted: the legacy document is not published, and the
        # catalogue still lists the one readable binding.
        heads = sandbox.admin.execute(
            "SELECT revision FROM speech_publication_heads "
            "WHERE kind = 'binding' AND object_id = %s",
            (MODEL,),
        ).fetchall()
        assert heads == [("1",)]
        publications = sandbox.admin.execute(
            "SELECT revision FROM speech_publications WHERE kind = 'binding' AND object_id = %s",
            (MODEL,),
        ).fetchall()
        assert publications == [("1",)]
        listed = await http.get("/v1/models")
        assert listed.status_code == 200, listed.text
    assert "binding_id" not in legacy, "the legacy fixture must not carry identity"


async def test_a_published_revision_that_names_no_binding_fails_the_catalogue(
    sandbox: Sandbox,
) -> None:
    async with _http(sandbox) as http:
        created = await http.post(
            CREATE_URL, json=_create_body(), headers={"Idempotency-Key": "catalogue-forced-1"}
        )
        assert created.status_code == 201, created.text
        published = await http.post(
            PUBLISH_URL, json=PUBLISH_BODY, headers={"Idempotency-Key": "catalogue-forced-2"}
        )
        assert published.status_code == 201, published.text
        healthy = await http.get("/v1/models")
        assert healthy.status_code == 200, healthy.text

    _insert_legacy_revision(sandbox, "2")
    # Force the legacy document into the published surface, exactly what the
    # pre-fix publish path did; the catalogue must fail closed rather than list a
    # model whose identity is only an assumption.
    sandbox.admin.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding', %s, '2', 'binding-rev-2', 1700000000000002)",
        (MODEL,),
    )
    sandbox.admin.execute(
        "INSERT INTO speech_binding_publication_evidence "
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        " qualification_report_id, evidence_source, recorded_at_us) "
        "VALUES ('binding', %s, '2', 'binding-rev-2', 'cap-asr-quality-7', "
        "'qrep-asr-quality-1', 'publish_http', 1700000000000002)",
        (MODEL,),
    )
    sandbox.admin.execute(
        "INSERT INTO speech_publication_heads "
        "(kind, object_id, revision, revision_id, updated_at_us) "
        "VALUES ('binding', %s, '2', 'binding-rev-2', 1700000000000002) "
        "ON CONFLICT (kind, object_id) DO UPDATE SET revision = EXCLUDED.revision, "
        "revision_id = EXCLUDED.revision_id, updated_at_us = EXCLUDED.updated_at_us",
        (MODEL,),
    )
    sandbox.admin.execute(
        "UPDATE speech_catalog_state SET revision = revision + 1, "
        "generated_at_us = GREATEST(generated_at_us, 1700000000000002)"
    )

    async with _http(sandbox) as http:
        unavailable = await http.get("/v1/models")
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["error"]["code"] == "model_catalogue_unavailable"


async def test_the_deployed_api_footprint_serves_the_catalogue(sandbox: Sandbox) -> None:
    """The production assembly: three least-privilege roles, verified at startup."""
    writer, life, reader = (f"sn_catid_{part}_{uuid.uuid4().hex[:8]}" for part in ("w", "l", "r"))
    for role, grants in (
        (writer, binding_draft_grants(sandbox.schema, writer)),
        (
            life,
            (
                *binding_lifecycle_grants(sandbox.schema, life),
                *binding_publication_grants(sandbox.schema, life),
            ),
        ),
        (reader, model_catalog_reader_grants(sandbox.schema, reader)),
    ):
        sandbox.admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in grants:
            sandbox.admin.execute(grant)
    try:
        settings = _settings().model_copy(
            update={
                "database_schema": sandbox.schema,
                "asset_allowed_hosts": ("test",),
                "binding_writer_database_url": SecretStr(
                    make_conninfo(sandbox.url, user=writer, password=PASSWORD)
                ),
                "binding_lifecycle_database_url": SecretStr(
                    make_conninfo(sandbox.url, user=life, password=PASSWORD)
                ),
                "model_catalog_reader_database_url": SecretStr(
                    make_conninfo(sandbox.url, user=reader, password=PASSWORD)
                ),
            }
        )
        app = create_app(settings=settings)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
        ):
            created = await http.post(
                CREATE_URL,
                json=_create_body(),
                headers={"Idempotency-Key": "deployed-footprint-1"},
            )
            assert created.status_code == 201, created.text
            published = await http.post(
                PUBLISH_URL,
                json=PUBLISH_BODY,
                headers={"Idempotency-Key": "deployed-footprint-2"},
            )
            assert published.status_code == 201, published.text
            activated = await http.post(
                ACTIVATE_URL,
                json={"expected_current_revision": "1"},
                headers={"Idempotency-Key": "deployed-footprint-3"},
            )
            assert activated.status_code == 200, activated.text
            listed = await http.get("/v1/models")
            assert listed.status_code == 200, listed.text
            body = _models(sandbox, listed.json())
        assert [entry["id"] for entry in body["data"]] == [MODEL]
    finally:
        for role in (writer, life, reader):
            sandbox.admin.execute(f'DROP OWNED BY "{role}"')
            sandbox.admin.execute(f'DROP ROLE IF EXISTS "{role}"')
