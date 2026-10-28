"""Real PostgreSQL publication registry; a skip is never publication evidence."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.app import create_app
from speechnexus_backend.model_catalog_runtime import ModelCatalogRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.binding_documents import encode_document
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.model_catalog_postgres import PostgresModelCatalog
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
    publication_grants,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_publication_test"


def scalar_int(
    connection: psycopg.Connection[tuple[object, ...]],
    query: str,
    parameters: tuple[object, ...] = (),
) -> int:
    row = connection.execute(query, parameters).fetchone()
    assert row is not None
    value = row[0]
    assert isinstance(value, int)
    return value


@dataclass
class Sandbox:
    store: PostgresPublications
    catalog: PostgresModelCatalog
    pool: SpeechPool
    admin: psycopg.Connection[tuple[object, ...]]
    schema: str
    role: str
    url: str


@pytest.fixture
def sandbox(request: pytest.FixtureRequest) -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.skip("Configure an isolated PostgreSQL DSN; skip is not evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_pub_" + suffix, "sn_pub_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    legacy_migration_test = request.node.name in {
        "test_pronunciation_downgrade_refuses_existing_publications",
        "test_pronunciation_downgrade_requires_read_committed",
        "test_empty_pronunciation_downgrade_reverts_the_kind_constraint",
    }
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(
                config, "0023_publication_lifecycle" if legacy_migration_test else "head"
            )
    finally:
        engine.dispose()
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in (
        *publication_grants(schema, role),
        *binding_draft_grants(schema, role),
        *model_catalog_reader_grants(schema, role),
    ):
        if legacy_migration_test and (
            "speech_publication_installation" in grant or "speech_binding_publish_intents" in grant
        ):
            continue
        admin.execute(grant)
    pool = speech_pool(
        make_conninfo(url, user=role, password=PASSWORD), schema=schema, max_size=2, name="pub-test"
    )
    pool.open()
    try:
        yield Sandbox(
            PostgresPublications(pool), PostgresModelCatalog(pool), pool, admin, schema, role, url
        )
    finally:
        pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def _http_settings() -> Settings:
    """A deterministic app config; the catalogue is injected explicitly below."""
    return Settings(
        service_name="speechnexus-models-http-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )


def publication(revision: int, revision_id: str, object_id: str = "binding-tts") -> Publication:
    return Publication(
        "binding", object_id, revision, revision_id, 1_700_000_000_000_000 + revision
    )


def _append(
    sandbox: Sandbox, item: Publication, *, expected_current_revision: object = None
) -> bool:
    state = sandbox.admin.execute(
        "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE"
    ).fetchone()
    assert state is not None
    with sandbox.admin.transaction(), sandbox.admin.cursor(row_factory=dict_row) as cursor:
        return append_publication(
            cursor,
            item,
            expected_current_revision=expected_current_revision,
            expected_catalog_revision=state[0],
        )


def test_append_is_insert_only_and_idempotent(sandbox: Sandbox) -> None:
    first = publication(1, "binding-rev-1")

    assert _append(sandbox, first, expected_current_revision=None) is True
    assert _append(sandbox, first) is False
    assert sandbox.store.get("binding", "binding-tts", 1) == first


def test_other_document_cannot_take_a_published_revision(sandbox: Sandbox) -> None:
    _append(sandbox, publication(1, "binding-rev-1"), expected_current_revision=None)

    assert _append(sandbox, publication(1, "binding-rev-other")) is False
    assert sandbox.store.get("binding", "binding-tts", 1) == publication(1, "binding-rev-1")


def test_missing_publication_is_absent_not_defaulted(sandbox: Sandbox) -> None:
    assert sandbox.store.get("binding", "binding-tts", 7) is None


def test_listing_is_ordered_and_scoped_by_kind(sandbox: Sandbox) -> None:
    _append(sandbox, publication(2, "binding-rev-2"), expected_current_revision=None)
    _append(sandbox, publication(1, "binding-rev-1"), expected_current_revision="2")
    _append(sandbox, Publication("profile", "profile-main", 1, "profile-rev-1", 1))

    assert [(row.object_id, row.revision) for row in sandbox.store.list_kind("binding")] == [
        ("binding-tts", 1),
        ("binding-tts", 2),
    ]
    assert [row.object_id for row in sandbox.store.list_kind("profile")] == ["profile-main"]


def test_runtime_role_cannot_rewrite_or_remove_publications(sandbox: Sandbox) -> None:
    _append(sandbox, publication(1, "binding-rev-1"), expected_current_revision=None)
    runtime = psycopg.connect(
        make_conninfo(sandbox.url, user=sandbox.role, password=PASSWORD), autocommit=True
    )
    try:
        runtime.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute("UPDATE speech_publications SET revision_id = 'rewritten'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute("DELETE FROM speech_publications")
    finally:
        runtime.close()
    assert sandbox.store.get("binding", "binding-tts", 1) == publication(1, "binding-rev-1")


def create_binding(
    admin: psycopg.Connection[tuple[object, ...]], definition: dict[str, object]
) -> None:
    """Insert one prepared binding revision exactly as the draft writer would."""
    binding_id = str(definition["binding_id"])
    key = "sha256:" + "a1" * 32
    request = "sha256:" + "b2" * 32
    document = "sha256:" + "c3" * 32
    admin.execute("DELETE FROM speech_binding_revisions")
    admin.execute("DELETE FROM speech_binding_commands")
    admin.execute("DELETE FROM speech_binding_families")
    admin.execute(
        "INSERT INTO speech_binding_families (binding_id, latest_revision) VALUES (%s, %s)",
        (binding_id, "1"),
    )
    admin.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, %s, 1, %s, %s)",
        (key, binding_id, "{}", request),
    )
    admin.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, "
        " definition_json, definition_digest, request_digest, created_at_us) "
        "VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s)",
        (
            binding_id,
            "1",
            "binding-rev-1",
            key,
            encode_document(definition).decode("utf-8"),
            document,
            request,
            1_700_000_000_000_000,
        ),
    )


DEFINITION: dict[str, object] = {
    "schema_version": "1.0.0",
    "binding_id": "tts-quality",
    "revision": "1",
    "revision_id": "binding-rev-1",
    "logical_model": "qwen3-tts-customvoice",
    "tasks": ["tts_file", "tts_realtime"],
    "engine": {
        "kind": "vllm_omni",
        "image_digest": "sha256:" + "1" * 64,
        "runtime_version": "vllm-omni-2026.10",
    },
    "artifact_set": [
        {
            "role": "weights",
            "artifact_id": "model-qwen3-tts",
            "digest": "sha256:" + "2" * 64,
            "media_type": "application/x-safetensors",
            "size_bytes": 1,
        }
    ],
    "execution_recipe": {
        "recipe_revision": "1",
        "capacity_manifest_id": "cap-tts-quality-1",
        "stage_roles": ["tts_talker", "tts_code2wav"],
        "requires_complete_reservation": True,
    },
    "parameter_domains": {
        "tts": {
            "languages": ["zh", "en"],
            "audio_formats": ["mp3", "wav", "pcm"],
            "sample_rates_hz": [24000],
            "text_commit_modes": ["explicit", "sentence"],
            "audio_delivery_modes": ["complete", "chunked"],
            "supports_instructions": True,
            "supports_speed": True,
            "speed_range": {"minimum": 0.75, "maximum": 1.25},
            "max_input_codepoints": 2000,
            "supports_seed": False,
        }
    },
    "release_tier": "production",
    "publication": {
        "status": "draft",
        "published_at": None,
        "retired_at": None,
        "capacity_manifest_id": None,
        "qualification_report_id": None,
        "disabled_reason": None,
    },
    "created_at": "2026-10-27T08:00:00Z",
}


def test_catalogue_lists_only_published_bindings(sandbox: Sandbox) -> None:
    create_binding(sandbox.admin, DEFINITION)

    assert sandbox.catalog.list_models() == []

    _append(
        sandbox,
        Publication("binding", "tts-quality", 1, "binding-rev-1", 1_700_000_000_000_000),
        expected_current_revision=None,
    )
    models = sandbox.catalog.list_models()

    assert [model["id"] for model in models] == ["tts-quality"]
    assert models[0]["nexus"]["logical_model"] == "qwen3-tts-customvoice"
    assert models[0]["nexus"]["tasks"] == ["tts_file", "tts_realtime"]
    assert models[0]["nexus"]["readiness"]["admission_state"] == "unavailable"


def test_publication_naming_another_document_does_not_list_a_model(sandbox: Sandbox) -> None:
    create_binding(sandbox.admin, DEFINITION)
    _append(
        sandbox,
        Publication("binding", "tts-quality", 1, "binding-rev-other", 1),
        expected_current_revision=None,
    )

    assert sandbox.catalog.list_models() == []


def test_catalogue_lists_one_model_at_the_latest_published_revision(sandbox: Sandbox) -> None:
    create_binding(sandbox.admin, DEFINITION)
    recipe = DEFINITION["execution_recipe"]
    assert isinstance(recipe, dict)
    create_binding_revision(
        sandbox.admin,
        {
            **DEFINITION,
            "revision": "2",
            "revision_id": "binding-rev-2",
            "logical_model": "qwen3-tts-customvoice-v2",
            "execution_recipe": {
                **recipe,
                "capacity_manifest_id": "cap-tts-quality-2",
            },
        },
    )
    _append(
        sandbox,
        publication(1, "binding-rev-1", object_id="tts-quality"),
        expected_current_revision=None,
    )
    _append(
        sandbox,
        publication(2, "binding-rev-2", object_id="tts-quality"),
        expected_current_revision="1",
    )

    models = sandbox.catalog.list_models()

    assert len(models) == 1
    assert models[0]["id"] == "tts-quality"
    assert models[0]["nexus"]["binding"]["revision"] == "2"
    assert models[0]["nexus"]["logical_model"] == "qwen3-tts-customvoice-v2"


def create_binding_revision(
    admin: psycopg.Connection[tuple[object, ...]], definition: dict[str, object]
) -> None:
    """Append a second prepared revision without changing the original fixture helper."""
    binding_id = str(definition["binding_id"])
    revision = str(definition["revision"])
    revision_id = str(definition["revision_id"])
    key = "sha256:" + ("d" + revision).ljust(64, "d")
    request = "sha256:" + ("e" + revision).ljust(64, "e")
    document = "sha256:" + ("f" + revision).ljust(64, "f")
    admin.execute(
        "UPDATE speech_binding_families SET latest_revision=%s WHERE binding_id=%s",
        (revision, binding_id),
    )
    admin.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, %s, 1, %s, %s)",
        (key, binding_id, "{}", request),
    )
    admin.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, "
        " definition_json, definition_digest, request_digest, created_at_us) "
        "VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s)",
        (
            binding_id,
            revision,
            revision_id,
            key,
            encode_document(definition).decode("utf-8"),
            document,
            request,
            1_700_000_000_000_000 + int(revision),
        ),
    )


def test_snapshot_exposes_the_monotonic_catalogue_revision(sandbox: Sandbox) -> None:
    create_binding(sandbox.admin, DEFINITION)

    empty = sandbox.catalog.snapshot()
    assert empty.catalog_revision == 1
    assert empty.models == ()

    _append(
        sandbox,
        Publication("binding", "tts-quality", 1, "binding-rev-1", 1_700_000_000_000_000),
        expected_current_revision=None,
    )
    populated = sandbox.catalog.snapshot()

    assert populated.catalog_revision == 2
    assert populated.generated_at_us == 1_700_000_000_000_000
    assert populated.capability_models[0]["binding"] == {
        "id": "tts-quality",
        "revision": "1",
    }
    assert populated.capability_models[0]["parameter_domains"]["tts"]["audio_formats"] == [
        "mp3",
        "wav",
        "pcm",
    ]


async def test_models_conditional_get_over_real_postgres_and_http(sandbox: Sandbox) -> None:
    create_binding(sandbox.admin, DEFINITION)
    _append(
        sandbox,
        Publication("binding", "tts-quality", 1, "binding-rev-1", 1_700_000_000_000_000),
        expected_current_revision=None,
    )
    app = create_app(settings=_http_settings(), catalogue=sandbox.catalog)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        first = await http.get("/v1/models/tts-quality")
        assert first.status_code == 200, first.text
        etag = first.headers["etag"]
        assert etag.startswith('"sha256:')

        matched = await http.get("/v1/models/tts-quality", headers={"If-None-Match": etag})
        weak = await http.get("/v1/models/tts-quality", headers={"If-None-Match": "W/" + etag})
        missed = await http.get(
            "/v1/models/tts-quality", headers={"If-None-Match": '"sha256:' + "0" * 64 + '"'}
        )
        assert matched.status_code == weak.status_code == 304
        assert matched.content == weak.content == b""
        assert matched.headers["etag"] == etag
        assert missed.status_code == 200
        assert missed.json() == first.json()

        recipe = DEFINITION["execution_recipe"]
        assert isinstance(recipe, dict)
        create_binding_revision(
            sandbox.admin,
            {
                **DEFINITION,
                "revision": "2",
                "revision_id": "binding-rev-2",
                "logical_model": "qwen3-tts-customvoice-v2",
                "execution_recipe": {**recipe, "capacity_manifest_id": "cap-tts-quality-2"},
            },
        )
        _append(
            sandbox,
            Publication("binding", "tts-quality", 2, "binding-rev-2", 1_700_000_000_000_002),
            expected_current_revision="1",
        )

        changed = await http.get("/v1/models/tts-quality")
        stale = await http.get("/v1/models/tts-quality", headers={"If-None-Match": etag})
        refreshed = await http.get(
            "/v1/models/tts-quality", headers={"If-None-Match": changed.headers["etag"]}
        )
        listed = await http.get("/v1/models")
        assert changed.status_code == 200
        assert changed.headers["etag"] != etag
        assert changed.json()["nexus"]["logical_model"] == "qwen3-tts-customvoice-v2"
        assert stale.status_code == 200
        assert refreshed.status_code == 304
        assert listed.headers["etag"] != changed.headers["etag"]


PREDEFINED_ROLES = (
    "pg_read_all_data",
    "pg_write_all_data",
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
    "pg_signal_backend",
    "pg_monitor",
    "pg_read_all_settings",
    "pg_read_all_stats",
)


def _create_reader(sandbox: Sandbox) -> str:
    reader = "sn_pub_reader_" + uuid.uuid4().hex[:12]
    sandbox.admin.execute(f"CREATE ROLE \"{reader}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in model_catalog_reader_grants(sandbox.schema, reader):
        sandbox.admin.execute(grant)
    return reader


def _drop_reader(sandbox: Sandbox, reader: str) -> None:
    sandbox.admin.execute(f'DROP OWNED BY "{reader}"')
    sandbox.admin.execute(f'DROP ROLE IF EXISTS "{reader}"')


def _assert_reader_rejected(sandbox: Sandbox, reader: str) -> None:
    runtime = _reader_runtime(sandbox, reader)
    with pytest.raises(ValueError):
        runtime.open()
    assert runtime.catalogue is None
    runtime.close()


def test_reader_receives_only_the_published_surface(sandbox: Sandbox) -> None:
    reader = _create_reader(sandbox)
    try:
        privileges = sandbox.admin.execute(
            "SELECT "
            "pg_catalog.has_table_privilege(%s,%s,'SELECT') AS can_select_view, "
            "pg_catalog.has_table_privilege(%s,%s,'SELECT') AS can_select_drafts, "
            "pg_catalog.has_table_privilege(%s,%s,"
            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS can_write",
            (
                reader,
                f"{sandbox.schema}.speech_published_binding_revisions",
                reader,
                f"{sandbox.schema}.speech_binding_revisions",
                reader,
                f"{sandbox.schema}.speech_publications",
            ),
        ).fetchone()
        assert privileges == (True, False, False)

        options = sandbox.admin.execute(
            "SELECT c.reloptions FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname='speech_published_binding_revisions'",
            (sandbox.schema,),
        ).fetchone()
        assert options is not None
        reloptions = options[0]
        assert isinstance(reloptions, list)
        assert "security_barrier=true" in reloptions
        assert "security_invoker=false" in reloptions

        draft_table = f'"{sandbox.schema}".speech_binding_revisions'
        sandbox.admin.execute(f'GRANT SELECT ON {draft_table} TO "{reader}"')
        try:
            _assert_reader_rejected(sandbox, reader)
        finally:
            sandbox.admin.execute(f'REVOKE SELECT ON {draft_table} FROM "{reader}"')

        sandbox.admin.execute(f'GRANT SELECT (definition_json) ON {draft_table} TO "{reader}"')
        try:
            _assert_reader_rejected(sandbox, reader)
        finally:
            sandbox.admin.execute(
                f'REVOKE SELECT (definition_json) ON {draft_table} FROM "{reader}"'
            )

        runtime = _reader_runtime(sandbox, reader)
        runtime.open()
        runtime.close()
    finally:
        _drop_reader(sandbox, reader)


def test_reader_rejects_every_predefined_role(sandbox: Sandbox) -> None:
    reader = _create_reader(sandbox)
    try:
        for predefined in PREDEFINED_ROLES:
            sandbox.admin.execute(f'GRANT "{predefined}" TO "{reader}"')
            try:
                _assert_reader_rejected(sandbox, reader)
            finally:
                sandbox.admin.execute(f'REVOKE "{predefined}" FROM "{reader}"')

        runtime = _reader_runtime(sandbox, reader)
        runtime.open()
        runtime.close()
    finally:
        _drop_reader(sandbox, reader)


def test_reader_rejects_sequence_and_database_privileges(sandbox: Sandbox) -> None:
    reader = _create_reader(sandbox)
    try:
        runtime = _reader_runtime(sandbox, reader)
        runtime.open()
        runtime.close()

        current = sandbox.admin.execute("SELECT current_database()").fetchone()
        assert current is not None
        database = str(current[0])
        sandbox.admin.execute(f'GRANT TEMP ON DATABASE "{database}" TO "{reader}"')
        try:
            _assert_reader_rejected(sandbox, reader)
        finally:
            sandbox.admin.execute(f'REVOKE TEMP ON DATABASE "{database}" FROM "{reader}"')

        sandbox.admin.execute(f'GRANT CREATE ON DATABASE "{database}" TO "{reader}"')
        try:
            _assert_reader_rejected(sandbox, reader)
        finally:
            sandbox.admin.execute(f'REVOKE CREATE ON DATABASE "{database}" FROM "{reader}"')

        sandbox.admin.execute(f'CREATE SEQUENCE "{sandbox.schema}".reader_probe_seq')
        try:
            sandbox.admin.execute(
                f'GRANT USAGE ON SEQUENCE "{sandbox.schema}".reader_probe_seq TO "{reader}"'
            )
            _assert_reader_rejected(sandbox, reader)
        finally:
            sandbox.admin.execute(f'DROP SEQUENCE "{sandbox.schema}".reader_probe_seq')

        runtime = _reader_runtime(sandbox, reader)
        runtime.open()
        runtime.close()
    finally:
        _drop_reader(sandbox, reader)


def test_reader_view_exposes_only_published_binding_revisions(sandbox: Sandbox) -> None:
    create_binding(sandbox.admin, DEFINITION)
    create_binding_revision(
        sandbox.admin,
        {
            **DEFINITION,
            "revision": "2",
            "revision_id": "binding-rev-2",
            "logical_model": "qwen3-tts-customvoice-v2",
        },
    )
    _append(
        sandbox,
        Publication("binding", "tts-quality", 1, "binding-rev-1", 1_700_000_000_000_000),
        expected_current_revision=None,
    )

    reader = _create_reader(sandbox)
    try:
        runtime = _reader_runtime(sandbox, reader)
        runtime.open()
        catalogue = runtime.catalogue
        assert catalogue is not None
        assert [model["id"] for model in catalogue.list_models()] == ["tts-quality"]
        runtime.close()

        connection = psycopg.connect(
            make_conninfo(sandbox.url, user=reader, password=PASSWORD), autocommit=True
        )
        try:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM speech_binding_revisions")
            rows = connection.execute(
                "SELECT binding_id, revision, revision_id FROM "
                "speech_published_binding_revisions ORDER BY revision"
            ).fetchall()
            assert rows == [("tts-quality", "1", "binding-rev-1")]
            assert (
                connection.execute(
                    "SELECT definition_json FROM speech_published_binding_revisions "
                    "WHERE revision='2'"
                ).fetchall()
                == []
            )
        finally:
            connection.close()
    finally:
        _drop_reader(sandbox, reader)


def _reader_runtime(sandbox: Sandbox, reader: str) -> ModelCatalogRuntime:
    return ModelCatalogRuntime(
        Settings(
            service_name="speechnexus-catalogue-reader-test",
            database_url=SecretStr(sandbox.url),
            database_schema=sandbox.schema,
            model_catalog_reader_database_url=SecretStr(
                make_conninfo(sandbox.url, user=reader, password=PASSWORD)
            ),
            object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
            object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
            object_storage_bucket="test",
        )
    )


def _migration_config(sandbox: Sandbox) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = sandbox.schema
    return config


def _downgrade_pronunciation(sandbox: Sandbox, *, isolation_level: str | None = None) -> None:
    engine = create_engine(
        sandbox.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
        isolation_level=isolation_level,
    )
    config = _migration_config(sandbox)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.downgrade(config, "0017_job_submissions")
    finally:
        engine.dispose()


def test_pronunciation_downgrade_refuses_existing_publications(sandbox: Sandbox) -> None:
    pronunciation = Publication("pronunciation", "pron-main", 1, "pron-rev-1", 1)
    _append(sandbox, pronunciation)

    with pytest.raises(RuntimeError, match="Pronunciation publications require preservation"):
        _downgrade_pronunciation(sandbox)

    assert sandbox.store.get("pronunciation", "pron-main", 1) == pronunciation


def test_pronunciation_downgrade_requires_read_committed(sandbox: Sandbox) -> None:
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        _downgrade_pronunciation(sandbox, isolation_level="REPEATABLE READ")

    sandbox.admin.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('pronunciation', 'pron-main', '1', 'pron-rev-1', 1)"
    )


def test_empty_pronunciation_downgrade_reverts_the_kind_constraint(sandbox: Sandbox) -> None:
    _downgrade_pronunciation(sandbox)

    version = sandbox.admin.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ("0017_job_submissions",)
    with pytest.raises(psycopg.errors.CheckViolation):
        sandbox.admin.execute(
            "INSERT INTO speech_publications "
            "(kind, object_id, revision, revision_id, published_at_us) "
            "VALUES ('pronunciation', 'pron-main', '1', 'pron-rev-1', 1)"
        )


async def test_retirement_advances_catalog_revision_and_etag(sandbox: Sandbox) -> None:
    """AC1: a lifecycle tombstone changes the version without another publication row."""
    create_binding(sandbox.admin, DEFINITION)
    published = Publication("binding", "tts-quality", 1, "binding-rev-1", 1_700_000_000_000_000)
    assert _append(sandbox, published, expected_current_revision=None) is True
    before = sandbox.catalog.snapshot()
    assert before.catalog_revision == 2
    assert [model["id"] for model in before.models] == ["tts-quality"]
    jobs = PostgresPublicJobs(sandbox.pool)
    assert jobs.catalog_revision() == before.catalog_revision
    before_job = jobs.latest_binding("tts-quality")
    assert before_job is not None
    assert before_job.publication == published

    app = create_app(settings=_http_settings(), catalogue=sandbox.catalog)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        first = await http.get("/v1/models")
        assert first.status_code == 200, first.text
        etag_before = first.headers["etag"]
        assert [item["id"] for item in first.json()["data"]] == ["tts-quality"]
        count_before = scalar_int(sandbox.admin, "SELECT count(*) FROM speech_publications")
        retired_at_us = 1_700_000_000_000_100

        assert sandbox.store.retire(published, retired_at_us=retired_at_us) is True

        after = sandbox.catalog.snapshot()
        count_after = scalar_int(sandbox.admin, "SELECT count(*) FROM speech_publications")
        assert count_after == count_before
        assert after.catalog_revision == before.catalog_revision + 1
        assert after.generated_at_us == retired_at_us
        assert after.models == ()
        assert sandbox.store.get("binding", "tts-quality", 1) == published
        assert jobs.catalog_revision() == after.catalog_revision
        assert jobs.latest_binding("tts-quality") is None

        retired = await http.get("/v1/models")
        assert retired.status_code == 200, retired.text
        etag_after = retired.headers["etag"]
        assert retired.json()["data"] == []
        assert etag_after != etag_before
        old_tag = await http.get("/v1/models", headers={"If-None-Match": etag_before})
        new_tag = await http.get("/v1/models", headers={"If-None-Match": etag_after})
        assert old_tag.status_code == 200
        assert new_tag.status_code == 304

        assert sandbox.store.retire(published, retired_at_us=retired_at_us + 1) is False
        assert sandbox.catalog.snapshot().catalog_revision == after.catalog_revision
        print(
            "catalog_revision_before="
            f"{before.catalog_revision} etag_before={etag_before} "
            "catalog_revision_after="
            f"{after.catalog_revision} etag_after={etag_after} "
            f"publication_rows={count_after}"
        )


def test_retiring_the_newest_revision_does_not_fall_back_to_an_older_one(
    sandbox: Sandbox,
) -> None:
    create_binding(sandbox.admin, DEFINITION)
    create_binding_revision(
        sandbox.admin,
        {
            **DEFINITION,
            "revision": "2",
            "revision_id": "binding-rev-2",
            "logical_model": "qwen3-tts-customvoice-v2",
        },
    )
    older = publication(1, "binding-rev-1", object_id="tts-quality")
    newest = publication(2, "binding-rev-2", object_id="tts-quality")
    assert _append(sandbox, older, expected_current_revision=None) is True
    assert _append(sandbox, newest, expected_current_revision="1") is True
    jobs = PostgresPublicJobs(sandbox.pool)
    resolved = jobs.latest_binding("tts-quality")
    assert resolved is not None
    assert resolved.publication == newest

    assert sandbox.store.retire(newest, retired_at_us=1_700_000_000_000_100) is True

    assert jobs.latest_binding("tts-quality") is None
    assert sandbox.catalog.list_models() == []
    assert sandbox.store.get("binding", "tts-quality", 1) == older


def test_evidence_and_idempotent_publication_do_not_advance_catalog_revision(
    sandbox: Sandbox,
) -> None:
    create_binding(sandbox.admin, DEFINITION)
    published = Publication("binding", "tts-quality", 1, "binding-rev-1", 1_700_000_000_000_000)
    assert _append(sandbox, published, expected_current_revision=None) is True
    after_publication = sandbox.catalog.snapshot().catalog_revision
    assert after_publication == 2

    sandbox.admin.execute(
        "INSERT INTO speech_binding_publication_evidence "
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us) "
        "VALUES ('binding', 'tts-quality', '1', 'binding-rev-1', "
        "'cap-tts-quality-1', 'qrep-tts-quality-1', 'operator_cli', 1700000000000001)"
    )
    assert sandbox.catalog.snapshot().catalog_revision == after_publication

    assert _append(sandbox, published) is False
    assert sandbox.catalog.snapshot().catalog_revision == after_publication


def test_concurrent_publications_receive_distinct_catalog_revisions(sandbox: Sandbox) -> None:
    import threading

    assert sandbox.catalog.snapshot().catalog_revision == 1
    url = make_conninfo(sandbox.url, user=sandbox.role, password=PASSWORD)
    first_inserted = threading.Event()
    second_started = threading.Event()
    allow_first_commit = threading.Event()
    revisions: dict[str, int] = {}
    errors: list[BaseException] = []

    def insert_and_read(label: str, object_id: str, revision: int) -> None:
        connection = psycopg.connect(url)
        try:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            connection.execute(
                "INSERT INTO speech_publications "
                "(kind, object_id, revision, revision_id, published_at_us) "
                "VALUES ('binding', %s, %s, %s, %s)",
                (object_id, str(revision), f"binding-rev-{label}", revision),
            )
            revisions[label] = scalar_int(
                connection, "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE"
            )
            if label == "a":
                first_inserted.set()
                assert allow_first_commit.wait(10)
            connection.commit()
        except BaseException as error:
            connection.rollback()
            errors.append(error)
        finally:
            connection.close()

    first = threading.Thread(target=insert_and_read, args=("a", "concurrent-a", 1))

    def insert_second() -> None:
        second_started.set()
        insert_and_read("b", "concurrent-b", 1)

    second = threading.Thread(target=insert_second)
    first.start()
    assert first_inserted.wait(10)
    second.start()
    assert second_started.wait(10)
    allow_first_commit.set()
    first.join(10)
    second.join(10)

    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert revisions == {"a": 2, "b": 3}
    assert revisions["a"] != revisions["b"]
    assert sandbox.catalog.snapshot().catalog_revision == 3
    assert scalar_int(sandbox.admin, "SELECT count(*) FROM speech_publications") == 2
    print(f"concurrent_catalog_revisions={revisions}")
