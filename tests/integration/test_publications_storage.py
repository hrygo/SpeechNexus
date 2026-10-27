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
from psycopg.conninfo import make_conninfo
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_publication_test"


@dataclass
class Sandbox:
    store: PostgresPublications
    schema: str
    role: str
    url: str


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
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
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in publication_grants(schema, role):
        admin.execute(grant)
    pool = speech_pool(
        make_conninfo(url, user=role, password=PASSWORD), schema=schema, max_size=2, name="pub-test"
    )
    pool.open()
    try:
        yield Sandbox(PostgresPublications(pool), schema, role, url)
    finally:
        pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def publication(revision: int, revision_id: str, object_id: str = "binding-tts") -> Publication:
    return Publication(
        "binding", object_id, revision, revision_id, 1_700_000_000_000_000 + revision
    )


def test_append_is_insert_only_and_idempotent(sandbox: Sandbox) -> None:
    first = publication(1, "binding-rev-1")

    assert sandbox.store.append(first) is True
    assert sandbox.store.append(first) is False
    assert sandbox.store.get("binding", "binding-tts", 1) == first


def test_other_document_cannot_take_a_published_revision(sandbox: Sandbox) -> None:
    sandbox.store.append(publication(1, "binding-rev-1"))

    assert sandbox.store.append(publication(1, "binding-rev-other")) is False
    assert sandbox.store.get("binding", "binding-tts", 1) == publication(1, "binding-rev-1")


def test_missing_publication_is_absent_not_defaulted(sandbox: Sandbox) -> None:
    assert sandbox.store.get("binding", "binding-tts", 7) is None


def test_listing_is_ordered_and_scoped_by_kind(sandbox: Sandbox) -> None:
    sandbox.store.append(publication(2, "binding-rev-2"))
    sandbox.store.append(publication(1, "binding-rev-1"))
    sandbox.store.append(Publication("profile", "profile-main", 1, "profile-rev-1", 1))

    assert [(row.object_id, row.revision) for row in sandbox.store.list_kind("binding")] == [
        ("binding-tts", 1),
        ("binding-tts", 2),
    ]
    assert [row.object_id for row in sandbox.store.list_kind("profile")] == ["profile-main"]


def test_runtime_role_cannot_rewrite_or_remove_publications(sandbox: Sandbox) -> None:
    sandbox.store.append(publication(1, "binding-rev-1"))
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
