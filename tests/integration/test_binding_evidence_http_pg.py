"""Real isolated PostgreSQL proof for the release-evidence identity reads.

These cases drive the assembled HTTP routes against a migrated schema, so the
response is contract-validated instead of hand-built. They cover a proven
absence, recorded digest presence and absence, the earliest recording time over
several publication rows, conflicting digests failing closed, and the read-only
identity: a role without SELECT gets 503 while its own INSERT/UPDATE/DELETE are
denied.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.publish_binding import publish_binding
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.binding_evidence import EvidenceKind
from speechnexus_backend.speech.application.binding_publication import BindingPublishIntent
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft, BindingUnavailableError
from speechnexus_backend.speech.infrastructure.binding_admin_reader_postgres import (
    PostgresBindingAdminReader,
)
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import (
    PostgresBindingDrafts,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft
from speechnexus_backend.speech.interfaces.http.binding_evidence import stamp

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_binding_evidence"
CAPACITY_URL = "/v1/nexus/admin/capacity-manifests/"
QUALIFICATION_URL = "/v1/nexus/admin/qualification-reports/"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
ROLES = ("writer", "publication", "reader", "noread")
EVIDENCE = "speech_binding_publication_evidence"


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, Any]]
    schema: str
    urls: dict[str, str]
    pools: dict[str, SpeechPool]


def _database_url() -> str:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not original evidence"
        )
    return base


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    base = _database_url()
    suffix = uuid.uuid4().hex[:10]
    schema = "sn_bindevidence_" + suffix
    names = {role: f"sn_bindevidence_{role}_{suffix}" for role in ROLES}
    admin: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        base, row_factory=dict_row, autocommit=True
    )
    pools: dict[str, SpeechPool] = {}
    engine = create_engine(
        base.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["schema"] = schema
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        for role in ROLES:
            admin.execute(f"CREATE ROLE \"{names[role]}\" LOGIN PASSWORD '{PASSWORD}'")
        for statement in binding_draft_grants(schema, names["writer"]):
            admin.execute(statement)
        for declaration in (
            binding_draft_grants,
            publication_grants,
            publication_evidence_grants,
        ):
            for statement in declaration(schema, names["publication"]):
                admin.execute(statement)
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{names["reader"]}"')
        admin.execute(f'GRANT SELECT ON "{schema}".{EVIDENCE} TO "{names["reader"]}"')
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{names["noread"]}"')
        urls = {role: make_conninfo(base, user=names[role], password=PASSWORD) for role in ROLES}
        pools = {
            role: speech_pool(
                urls[role], schema=schema, max_size=2, name=f"bindevidence-{role}-" + suffix
            )
            for role in ROLES
        }
        for pool in pools.values():
            pool.open()
        yield Sandbox(admin=admin, schema=schema, urls=urls, pools=pools)
    finally:
        for pool in pools.values():
            pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in ROLES:
            admin.execute(f'DROP ROLE IF EXISTS "{names[role]}"')
        admin.close()


def settings_for(sandbox: Sandbox) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "process_role": "api",
            "database_schema": sandbox.schema,
            "asset_allowed_hosts": ("test",),
        }
    )


def app_for(sandbox: Sandbox, role: str = "reader") -> Any:
    return create_app(
        settings=settings_for(sandbox),
        binding_evidence=PostgresBindingAdminReader(sandbox.pools[role]),
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )


def create_draft(
    sandbox: Sandbox, binding_id: str, key: str, capacity_manifest_id: str
) -> BindingDraft:
    body = binding_request()
    body["binding_id"] = binding_id
    body["execution_recipe"]["capacity_manifest_id"] = capacity_manifest_id
    result = PostgresBindingDrafts(sandbox.pools["writer"]).create(prepare_binding_draft(body, key))
    assert result.outcome == "created", result
    assert result.draft is not None
    return result.draft


def publish(
    sandbox: Sandbox,
    draft: BindingDraft,
    key: str,
    *,
    capacity_manifest_id: str,
    qualification_report_id: str,
    capacity_digest: str | None = None,
    qualification_digest: str | None = None,
) -> None:
    observation = PostgresPublications(sandbox.pools["publication"]).observe_binding_head(
        draft.binding_id
    )
    intent = BindingPublishIntent.from_request(
        installation_id=str(observation["installation_id"]),
        database=str(observation["database_name"]),
        schema=sandbox.schema,
        expected_current_revision=None,
        expected_catalog_revision=int(observation["catalog_revision"]),
        idempotency_key=key,
    )
    result = publish_binding(
        database_url=sandbox.urls["publication"],
        schema=sandbox.schema,
        binding_id=draft.binding_id,
        revision=int(draft.revision),
        revision_id=draft.revision_id,
        capacity_manifest_id=capacity_manifest_id,
        qualification_report_id=qualification_report_id,
        capacity_manifest_digest=capacity_digest,
        qualification_report_digest=qualification_digest,
        expected_current_revision=None,
        intent=intent,
    )
    assert result.created is True


def publish_pair(
    sandbox: Sandbox,
    binding_id: str,
    *,
    manifest_id: str,
    report_id: str,
    key: str,
    capacity_digest: str | None = None,
    qualification_digest: str | None = None,
) -> None:
    draft = create_draft(sandbox, binding_id, key + "-draft", manifest_id)
    publish(
        sandbox,
        draft,
        key + "-publish",
        capacity_manifest_id=manifest_id,
        qualification_report_id=report_id,
        capacity_digest=capacity_digest,
        qualification_digest=qualification_digest,
    )


def recorded_at_us(value: str) -> int:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return round((moment - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 1_000_000)


async def get(app: Any, url: str) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(url, headers={"X-Request-ID": "request-binding-evidence"})


async def test_empty_evidence_store_proves_absence(sandbox: Sandbox) -> None:
    response = await get(app_for(sandbox), CAPACITY_URL + "capm-absent-1")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "binding_evidence_not_found"
    assert body["error"]["retryable"] is False


async def test_published_evidence_projects_recorded_facts(sandbox: Sandbox) -> None:
    publish_pair(
        sandbox,
        "asr-alpha",
        manifest_id="capm-alpha-1",
        report_id="qrep-alpha-1",
        key="alpha",
    )
    publish_pair(
        sandbox,
        "asr-beta",
        manifest_id="capm-beta-1",
        report_id="qrep-beta-1",
        key="beta",
        capacity_digest=DIGEST_A,
        qualification_digest=DIGEST_B,
    )
    app = app_for(sandbox)

    capacity = await get(app, CAPACITY_URL + "capm-alpha-1")
    assert capacity.status_code == 200, capacity.text
    assert capacity.headers["cache-control"] == "no-store"
    assert capacity.json() == {
        "id": "capm-alpha-1",
        "recorded_at": capacity.json()["recorded_at"],
    }
    row = sandbox.admin.execute(
        f'SELECT recorded_at_us FROM "{sandbox.schema}".{EVIDENCE} WHERE capacity_manifest_id = %s',
        ("capm-alpha-1",),
    ).fetchone()
    assert row is not None
    assert capacity.json()["recorded_at"] == stamp(int(row["recorded_at_us"]))
    assert "digest" not in capacity.json() and "source" not in capacity.json()

    recorded = await get(app, QUALIFICATION_URL + "qrep-beta-1")
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["digest"] == DIGEST_B
    assert "source" not in recorded.json()

    manifest_with_digest = await get(app, CAPACITY_URL + "capm-beta-1")
    assert manifest_with_digest.status_code == 200, manifest_with_digest.text
    assert manifest_with_digest.json()["digest"] == DIGEST_A


async def test_unknown_identifier_is_404_after_a_successful_read(sandbox: Sandbox) -> None:
    publish_pair(
        sandbox,
        "asr-alpha",
        manifest_id="capm-alpha-1",
        report_id="qrep-alpha-1",
        key="alpha",
    )
    app = app_for(sandbox)

    assert (await get(app, CAPACITY_URL + "capm-alpha-1")).status_code == 200
    missing = await get(app, CAPACITY_URL + "capm-unknown-1")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "binding_evidence_not_found"


async def test_earliest_recording_time_wins_across_publications(sandbox: Sandbox) -> None:
    publish_pair(
        sandbox,
        "asr-alpha",
        manifest_id="capm-shared-1",
        report_id="qrep-alpha-1",
        key="alpha",
        capacity_digest=DIGEST_A,
    )
    publish_pair(
        sandbox,
        "asr-beta",
        manifest_id="capm-shared-1",
        report_id="qrep-beta-1",
        key="beta",
        capacity_digest=DIGEST_A,
    )
    # The evidence table records one timestamp per publication row; pin two
    # different values so the projection must choose the earliest deterministically.
    sandbox.admin.execute(
        f'UPDATE "{sandbox.schema}".{EVIDENCE} SET recorded_at_us = 1700000000000002 '
        "WHERE object_id = 'asr-alpha'"
    )
    sandbox.admin.execute(
        f'UPDATE "{sandbox.schema}".{EVIDENCE} SET recorded_at_us = 1700000000000001 '
        "WHERE object_id = 'asr-beta'"
    )

    response = await get(app_for(sandbox), CAPACITY_URL + "capm-shared-1")

    assert response.status_code == 200, response.text
    assert response.json()["recorded_at"] == "2023-11-14T22:13:20.000001Z"


async def test_conflicting_digests_fail_closed(sandbox: Sandbox) -> None:
    publish_pair(
        sandbox,
        "asr-alpha",
        manifest_id="capm-shared-1",
        report_id="qrep-alpha-1",
        key="alpha",
        capacity_digest=DIGEST_A,
    )
    publish_pair(
        sandbox,
        "asr-beta",
        manifest_id="capm-shared-1",
        report_id="qrep-beta-1",
        key="beta",
        capacity_digest=DIGEST_B,
    )

    response = await get(app_for(sandbox), CAPACITY_URL + "capm-shared-1")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "binding_unavailable"
    assert body["error"]["retryable"] is True


async def test_role_without_select_is_unavailable_not_absence(sandbox: Sandbox) -> None:
    response = await get(app_for(sandbox, "noread"), CAPACITY_URL + "capm-absent-1")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "binding_unavailable"
    assert response.json()["error"]["retryable"] is True
    with pytest.raises(BindingUnavailableError):
        PostgresBindingAdminReader(sandbox.pools["noread"]).recorded_evidence(
            EvidenceKind.CAPACITY_MANIFEST, "capm-absent-1"
        )


def test_reader_role_cannot_change_recorded_evidence(sandbox: Sandbox) -> None:
    reader = sandbox.pools["reader"]
    with reader.connection() as connection:
        for statement in (
            f'INSERT INTO "{sandbox.schema}".{EVIDENCE} (kind, object_id, revision, '
            "revision_id, capacity_manifest_id, qualification_report_id, evidence_source, "
            "recorded_at_us) VALUES ('binding', 'asr-x', '1', 'mbrev-x', 'capm-x', "
            "'qrep-x', 'operator_cli', 1)",
            f'UPDATE "{sandbox.schema}".{EVIDENCE} SET capacity_manifest_id = %s',
            f'DELETE FROM "{sandbox.schema}".{EVIDENCE}',
        ):
            with pytest.raises(InsufficientPrivilege), connection.transaction():
                connection.execute(statement, ("capm-y",) if "%s" in statement else ())
