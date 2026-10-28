"""Real PostgreSQL lifecycle: retire, rollback/activate, audit and pinned Jobs."""

from __future__ import annotations

import json
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
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_publications_storage import (
    DEFINITION,
    create_binding,
    create_binding_revision,
    publication,
)
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.binding_runtime import BindingLifecycleRuntime
from speechnexus_backend.speech.application.public_jobs import (
    ModelNotPublishedError,
    PublicJobs,
)
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresAuditEvents,
    PostgresBindingLifecycle,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.model_catalog_postgres import PostgresModelCatalog
from speechnexus_backend.speech.infrastructure.model_catalog_sql import list_published_bindings
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    binding_lifecycle_grants,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import (
    append_publication,
    find_current_publication,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    ACTIVATE_PATH,
    RETIRE_PATH,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_lifecycle_test"
RETIRE_URL = RETIRE_PATH.format(binding_id="tts-quality", revision="2")
ACTIVATE_1 = ACTIVATE_PATH.format(binding_id="tts-quality", revision="1")
ACTIVATE_2 = ACTIVATE_PATH.format(binding_id="tts-quality", revision="2")
ACTIVATE_9 = ACTIVATE_PATH.format(binding_id="tts-quality", revision="9")


@dataclass
class Sandbox:
    pool: SpeechPool
    admin: psycopg.Connection[tuple[object, ...]]
    schema: str
    role: str
    url: str
    lifecycle: PostgresBindingLifecycle
    store: PostgresPublications
    catalogue: PostgresModelCatalog
    facts: PostgresPublicJobs
    jobs: PublicJobs


def scalar(connection: psycopg.Connection[tuple[object, ...]], query: str) -> int:
    row = connection.execute(query).fetchone()
    assert row is not None and isinstance(row[0], int)
    return int(row[0])


def test_0022_upgrade_preserves_publications_and_legacy_visibility() -> None:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_life_migration_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    admin = psycopg.connect(url, autocommit=True)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "0022_catalog_revision_state")
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        create_binding(admin, DEFINITION)
        for revision in (2, 10):
            create_binding_revision(
                admin,
                {**DEFINITION, "revision": str(revision), "revision_id": f"binding-rev-{revision}"},
            )
        # Timestamps deliberately disagree with revision order. The latest
        # retired voice must remain absent rather than exposing revision 2.
        admin.execute(
            "INSERT INTO speech_publications "
            "(kind, object_id, revision, revision_id, published_at_us) VALUES "
            "('binding','tts-quality','2','binding-rev-2',200), "
            "('binding','tts-quality','10','binding-rev-10',100), "
            "('voice','voice-retired','2','voice-rev-2',200), "
            "('voice','voice-retired','10','voice-rev-10',100)"
        )
        admin.execute(
            "INSERT INTO speech_publication_retirements "
            "(kind, object_id, revision, revision_id, retired_at_us) "
            "VALUES ('voice','voice-retired','10','voice-rev-10',300)"
        )
        publications_before = admin.execute(
            "SELECT * FROM speech_publications ORDER BY kind, object_id, revision::numeric"
        ).fetchall()
        retirements_before = admin.execute(
            "SELECT kind, object_id, revision, revision_id, retired_at_us "
            "FROM speech_publication_retirements ORDER BY kind, object_id, revision::numeric"
        ).fetchall()
        revisions_before = admin.execute(
            "SELECT * FROM speech_binding_revisions ORDER BY binding_id, revision::numeric"
        ).fetchall()
        state_before = admin.execute("SELECT * FROM speech_catalog_state").fetchall()
        # This is the 0022 visibility rule, before heads exist.
        visible_before = admin.execute(
            "SELECT p.kind, p.object_id, p.revision, p.revision_id "
            "FROM speech_publications p WHERE NOT EXISTS ("
            "SELECT 1 FROM speech_publications n WHERE n.kind=p.kind "
            "AND n.object_id=p.object_id AND n.revision::numeric > p.revision::numeric) "
            "AND NOT EXISTS (SELECT 1 FROM speech_publication_retirements r "
            "WHERE r.kind=p.kind AND r.object_id=p.object_id "
            "AND r.revision=p.revision AND r.revision_id=p.revision_id) "
            "ORDER BY p.kind, p.object_id"
        ).fetchall()
        assert visible_before == [("binding", "tts-quality", "10", "binding-rev-10")]

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "0023_publication_lifecycle")
            migration.upgrade(config, "0023_publication_lifecycle")

        assert (
            admin.execute(
                "SELECT * FROM speech_publications ORDER BY kind, object_id, revision::numeric"
            ).fetchall()
            == publications_before
        )
        assert (
            admin.execute(
                "SELECT kind, object_id, revision, revision_id, retired_at_us "
                "FROM speech_publication_retirements ORDER BY kind, object_id, revision::numeric"
            ).fetchall()
            == retirements_before
        )
        assert admin.execute("SELECT * FROM speech_catalog_state").fetchall() == state_before
        assert (
            admin.execute(
                "SELECT * FROM speech_binding_revisions ORDER BY binding_id, revision::numeric"
            ).fetchall()
            == revisions_before
        )
        assert admin.execute(
            "SELECT mode, reason FROM speech_publication_retirements"
        ).fetchall() == [("retire", None)]
        heads = admin.execute(
            "SELECT kind, object_id, revision, revision_id FROM speech_publication_heads "
            "ORDER BY kind, object_id"
        ).fetchall()
        assert heads == visible_before
        with admin.cursor(row_factory=dict_row) as cursor:
            current = find_current_publication(cursor, "binding", "tts-quality")
            assert current is not None and current.revision == 10
            assert find_current_publication(cursor, "voice", "voice-retired") is None
            models = list_published_bindings(cursor)
        assert [(row["object_id"], row["revision"]) for row in models] == [("tts-quality", "10")]
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "0024_binding_publish_intents")
            migration.upgrade(config, "0024_binding_publish_intents")
        assert (
            admin.execute(
                "SELECT kind, object_id, revision, revision_id FROM speech_publication_heads "
                "ORDER BY kind, object_id"
            ).fetchall()
            == heads
        )
        assert admin.execute("SELECT * FROM speech_catalog_state").fetchall() == state_before
        assert admin.execute("SELECT count(*) FROM speech_binding_publish_intents").fetchone() == (
            0,
        )
        assert admin.execute("SELECT count(*) FROM speech_publication_installation").fetchone() == (
            1,
        )
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="identity require preservation"):
                migration.downgrade(config, "0023_publication_lifecycle")
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not lifecycle evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_life_" + suffix, "sn_life_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    pool = None
    try:
        for grant in (
            *binding_lifecycle_grants(schema, role),
            *job_runtime_grants(schema, role),
            *public_job_grants(schema, role),
            *job_dispatch_grants(schema, role),
            *public_job_submission_grants(schema, role),
            f"GRANT SELECT ON speech_workspaces, speech_publications, "
            f"speech_publication_heads, speech_catalog_state, "
            f"speech_publication_retirements, speech_binding_revisions, "
            f"speech_published_binding_revisions, speech_assets, "
            f'speech_pronunciation_revisions TO "{role}"',
        ):
            admin.execute(grant)
        pool = speech_pool(
            make_conninfo(url, user=role, password=PASSWORD),
            schema=schema,
            max_size=2,
            name="life-" + suffix,
        )
        pool.open()
        adapter = PostgresPublicJobs(pool, timeout_seconds=3)
        yield Sandbox(
            pool=pool,
            admin=admin,
            schema=schema,
            role=role,
            url=url,
            lifecycle=PostgresBindingLifecycle(pool, timeout_seconds=3),
            store=PostgresPublications(pool, timeout_seconds=3),
            catalogue=PostgresModelCatalog(pool, timeout_seconds=3),
            facts=adapter,
            jobs=PublicJobs(adapter, adapter),
        )
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def _body(*, revision: int = 2) -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic lifecycle job"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": f"synthetic-lifecycle-{revision}",
    }


def _seed(sandbox: Sandbox) -> None:
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
            "execution_recipe": {**recipe, "capacity_manifest_id": "cap-tts-quality-2"},
        },
    )
    with sandbox.admin.transaction(), sandbox.admin.cursor(row_factory=dict_row) as cursor:
        assert append_publication(
            cursor,
            publication(1, "binding-rev-1", object_id="tts-quality"),
            expected_current_revision=None,
            expected_catalog_revision=1,
        )
        assert append_publication(
            cursor,
            publication(2, "binding-rev-2", object_id="tts-quality"),
            expected_current_revision="1",
            expected_catalog_revision=2,
        )
    sandbox.admin.execute(
        "INSERT INTO speech_binding_publication_evidence "
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        " qualification_report_id, evidence_source, recorded_at_us) VALUES "
        "('binding','tts-quality','1','binding-rev-1','cap-tts-quality-1',"
        "'qrep-tts-quality-1','operator_cli',1700000000000010),"
        "('binding','tts-quality','2','binding-rev-2','cap-tts-quality-2',"
        "'qrep-tts-quality-2','operator_cli',1700000000000011)"
    )
    sandbox.admin.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('voice','voice-vivian','2','voice-rev-2',1700000000000020)"
    )


def _app(sandbox: Sandbox) -> Any:
    return create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",)}),
        binding_lifecycle=sandbox.lifecycle,
        catalogue=sandbox.catalogue,
        jobs=sandbox.jobs,
    )


@pytest.mark.anyio
async def test_retire_rollback_etag_selection_inflight_jobs_and_audit(sandbox: Sandbox) -> None:
    _seed(sandbox)
    before = sandbox.catalogue.snapshot()
    assert before.catalog_revision == 4
    assert [model["id"] for model in before.models] == ["tts-quality"]
    binding_before = sandbox.facts.latest_binding("tts-quality")
    assert binding_before is not None and binding_before.publication.revision == 2
    job = sandbox.jobs.submit(
        _body(), request_id="lifecycle-job-request", idempotency_key="lifecycle-job-key"
    )
    pinned = sandbox.jobs.get(job.job_id).submission.effective_configuration_json
    assert json.loads(pinned)["models"]["tts"]["revision"] == "2"
    publications_before = scalar(sandbox.admin, "SELECT count(*) FROM speech_publications")

    app = _app(sandbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        listed = await http.get("/v1/models")
        assert listed.status_code == 200, listed.text
        etag_before = listed.headers["etag"]
        assert [item["id"] for item in listed.json()["data"]] == ["tts-quality"]

        retired = await http.post(
            RETIRE_URL,
            json={"mode": "retire", "reason": "superseded by revision 3"},
            headers={"Idempotency-Key": "retire-revision-2"},
        )
        assert retired.status_code == 200, retired.text
        retired_body = retired.json()
        assert retired_body["publication"]["status"] == "retired"
        assert retired_body["publication"]["retired_at"] is not None
        retired_at = retired_body["publication"]["retired_at"]

        after_retire = sandbox.catalogue.snapshot()
        assert after_retire.catalog_revision == before.catalog_revision + 1
        assert after_retire.models == ()
        assert (
            scalar(sandbox.admin, "SELECT count(*) FROM speech_publications") == publications_before
        )
        assert scalar(sandbox.admin, "SELECT count(*) FROM speech_publication_heads") == 0
        assert sandbox.store.get("binding", "tts-quality", 2) == publication(
            2, "binding-rev-2", object_id="tts-quality"
        )

        empty = await http.get("/v1/models")
        assert empty.status_code == 200, empty.text
        assert empty.json()["data"] == []
        assert empty.headers["etag"] != etag_before
        etag_retired = empty.headers["etag"]
        stale = await http.get("/v1/models", headers={"If-None-Match": etag_before})
        assert stale.status_code == 200

        assert sandbox.facts.latest_binding("tts-quality") is None
        with pytest.raises(ModelNotPublishedError):
            sandbox.jobs.submit(
                _body(revision=3), request_id="blocked-request", idempotency_key="blocked-key"
            )
        assert sandbox.jobs.get(job.job_id).submission.effective_configuration_json == pinned
        stored = sandbox.admin.execute(
            "SELECT effective_configuration_json FROM speech_job_submissions WHERE job_id=%s",
            (job.job_id,),
        ).fetchone()
        assert stored is not None and stored[0] == pinned

        replay = await http.post(
            RETIRE_URL,
            json={"mode": "retire", "reason": "superseded by revision 3"},
            headers={"Idempotency-Key": "retire-revision-2"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["publication"]["retired_at"] == retired_at
        assert sandbox.catalogue.snapshot().catalog_revision == after_retire.catalog_revision

        duplicate = await http.post(
            RETIRE_URL,
            json={"mode": "retire", "reason": "a second operator"},
            headers={"Idempotency-Key": "retire-revision-2-again"},
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["error"]["code"] == "revision_already_retired"

        unknown = await http.post(
            RETIRE_PATH.format(binding_id="tts-quality", revision="9"),
            json={"mode": "retire"},
            headers={"Idempotency-Key": "retire-revision-9"},
        )
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["error"]["code"] == "binding_not_found"

        reactivate_retired = await http.post(ACTIVATE_2, json={"expected_current_revision": None})
        assert reactivate_retired.status_code == 409, reactivate_retired.text
        assert reactivate_retired.json()["error"]["code"] == "revision_not_published"

        stale_cas = await http.post(ACTIVATE_1, json={"expected_current_revision": "2"})
        assert stale_cas.status_code == 409, stale_cas.text
        assert stale_cas.json()["error"]["code"] == "configuration_changed"

        rolled_back = await http.post(ACTIVATE_1, json={"expected_current_revision": None})
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["publication"]["status"] == "published"
        assert rolled_back.json()["revision"] == "1"

        restored = await http.get("/v1/models")
        assert restored.status_code == 200, restored.text
        assert [item["id"] for item in restored.json()["data"]] == ["tts-quality"]
        assert restored.json()["data"][0]["nexus"]["logical_model"] == ("qwen3-tts-customvoice")
        assert restored.headers["etag"] not in (etag_before, etag_retired)
        assert sandbox.catalogue.snapshot().catalog_revision == after_retire.catalog_revision + 1

    selected = sandbox.facts.latest_binding("tts-quality")
    assert selected is not None and selected.publication.revision == 1
    assert scalar(sandbox.admin, "SELECT count(*) FROM speech_publications") == publications_before
    head = sandbox.admin.execute(
        "SELECT revision, revision_id FROM speech_publication_heads WHERE object_id='tts-quality'"
    ).fetchone()
    assert head == ("1", "binding-rev-1")
    retirement = sandbox.admin.execute(
        "SELECT mode, reason FROM speech_publication_retirements "
        "WHERE object_id='tts-quality' AND revision='2'"
    ).fetchone()
    assert retirement == ("retire", "superseded by revision 3")

    events, cursor = PostgresAuditEvents(sandbox.pool).list(limit=20, cursor=None, action=None)
    assert cursor is None
    assert [event["action"] for event in events] == [
        "model_binding.activate",
        "model_binding.retire",
    ]
    activate_event, retire_event = events
    assert activate_event["result_status"] == "published"
    assert activate_event["revision"] == "1"
    assert retire_event["result_status"] == "retired"
    assert retire_event["revision"] == "2"
    assert retire_event["disabled_reason"] is None
    assert retire_event["actor_type"] == "anonymous"
    assert retire_event["change_summary_json"]["reason"] == "superseded by revision 3"
    assert len(retire_event["request_id"]) <= 128 and retire_event["request_id"]


def test_lifecycle_runtime_accepts_only_the_least_privilege_role(sandbox: Sandbox) -> None:
    life = "sn_life_only_" + uuid.uuid4().hex[:10]
    sandbox.admin.execute(f"CREATE ROLE \"{life}\" LOGIN PASSWORD '{PASSWORD}'")
    try:
        for grant in binding_lifecycle_grants(sandbox.schema, life):
            sandbox.admin.execute(grant)
        runtime = BindingLifecycleRuntime(
            _settings().model_copy(
                update={
                    "database_schema": sandbox.schema,
                    "binding_lifecycle_database_url": SecretStr(
                        make_conninfo(sandbox.url, user=life, password=PASSWORD)
                    ),
                }
            )
        )
        runtime.open()
        assert runtime.provider is not None
        runtime.close()

        sandbox.admin.execute(
            f'GRANT SELECT ON "{sandbox.schema}".speech_binding_families TO "{life}"'
        )
        rejected = BindingLifecycleRuntime(
            _settings().model_copy(
                update={
                    "database_schema": sandbox.schema,
                    "binding_lifecycle_database_url": SecretStr(
                        make_conninfo(sandbox.url, user=life, password=PASSWORD)
                    ),
                }
            )
        )
        with pytest.raises(ValueError, match="Binding lifecycle startup rejected"):
            rejected.open()
        rejected.close()
    finally:
        sandbox.admin.execute(f'DROP OWNED BY "{life}"')
        sandbox.admin.execute(f'DROP ROLE IF EXISTS "{life}"')
