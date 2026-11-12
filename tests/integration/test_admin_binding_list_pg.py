"""Real isolated PostgreSQL proof for the administrative model binding listing.

The list projection joins the family allocation head, the immutable revision
document it names, the publication row and retirement tombstone for that exact
revision, and the mutable publication head. These cases exercise the assembled
HTTP route against a real schema, so the response is the contract-validated
shape rather than a hand-built dictionary, and they cover the empty registry,
deterministic keyset boundaries, the status filter and the read-only identity.

Publishing uses the real operator command; retirement and disable are written
directly because the list only reads the resulting tombstone/head state and the
lifecycle writer has its own integration coverage.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.binding_list_runtime import READABLE, BindingListRuntime
from speechnexus_backend.publish_binding import publish_binding
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.binding_publication import BindingPublishIntent
from speechnexus_backend.speech.application.binding_summaries import BindingSummaryService
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import (
    PostgresBindingDrafts,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.binding_summaries_postgres import (
    PostgresBindingSummaryReads,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft
from speechnexus_backend.speech.interfaces.http.model_bindings import cursor_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_binding_list"
URL = "/v1/nexus/admin/model-bindings"
CAPACITY = "cap-asr-quality-7"
REPORT = "qrep-asr-quality-7"
ROLES = ("writer", "publication", "list")


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, Any]]
    schema: str
    urls: dict[str, str]
    pools: dict[str, SpeechPool]
    service: BindingSummaryService

    def grant(self, role: str, statements: tuple[str, ...]) -> None:
        for statement in statements:
            self.admin.execute(statement)


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not projection evidence"
        )
    suffix = uuid.uuid4().hex[:10]
    schema = "sn_bindlist_" + suffix
    names = {role: f"sn_bindlist_{role}_{suffix}" for role in ROLES}
    engine = create_engine(
        base.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        base, row_factory=dict_row, autocommit=True
    )
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
    for statement in (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{names["list"]}"',
        *(f'GRANT SELECT ON "{schema}"."{table}" TO "{names["list"]}"' for table in READABLE),
    ):
        admin.execute(statement)
    urls = {role: make_conninfo(base, user=names[role], password=PASSWORD) for role in ROLES}
    pools = {
        role: speech_pool(urls[role], schema=schema, max_size=2, name=f"bindlist-{role}-" + suffix)
        for role in ROLES
    }
    for pool in pools.values():
        pool.open()
    try:
        yield Sandbox(
            admin=admin,
            schema=schema,
            urls=urls,
            pools=pools,
            service=BindingSummaryService(PostgresBindingSummaryReads(pools["list"])),
        )
    finally:
        for pool in pools.values():
            pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in ROLES:
            admin.execute(f'DROP ROLE IF EXISTS "{names[role]}"')
        admin.close()


def settings_for(sandbox: Sandbox, *, reader_url: str | None = None) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "process_role": "api",
            "database_schema": sandbox.schema,
            "binding_list_reader_database_url": (
                sandbox.urls["list"] if reader_url is None else reader_url
            ),
            "asset_allowed_hosts": ("test",),
            "binding_list_read_concurrency": 1,
        }
    )


@pytest.fixture
async def http(sandbox: Sandbox) -> Any:
    app = create_app(
        settings=settings_for(sandbox),
        binding_summaries=sandbox.service,
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield sandbox, client


def create_draft(
    sandbox: Sandbox,
    binding_id: str,
    key: str,
    *,
    logical_model: str,
    release_tier: str = "experimental",
) -> BindingDraft:
    body = {
        **binding_request(),
        "binding_id": binding_id,
        "logical_model": logical_model,
        "release_tier": release_tier,
    }
    result = PostgresBindingDrafts(sandbox.pools["writer"]).create(prepare_binding_draft(body, key))
    assert result.outcome == "created", result
    assert result.draft is not None
    return result.draft


def publish(sandbox: Sandbox, draft: BindingDraft, key: str) -> None:
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
        capacity_manifest_id=CAPACITY,
        qualification_report_id=REPORT,
        expected_current_revision=None,
        intent=intent,
    )
    assert result.created is True


def add_tombstone(
    sandbox: Sandbox, draft: BindingDraft, *, mode: str = "retire", reason: str | None = None
) -> None:
    sandbox.admin.execute(
        f'INSERT INTO "{sandbox.schema}".speech_publication_retirements '
        "(kind, object_id, revision, revision_id, retired_at_us, mode, reason) "
        "VALUES ('binding', %s, %s, %s, 1700000000000001, %s, %s)",
        (draft.binding_id, draft.revision, draft.revision_id, mode, reason),
    )
    sandbox.admin.execute(
        f'DELETE FROM "{sandbox.schema}".speech_publication_heads '
        "WHERE kind = 'binding' AND object_id = %s",
        (draft.binding_id,),
    )


async def get(client: AsyncClient, **parameters: Any) -> httpx.Response:
    return await client.get(
        URL, params=parameters, headers={"X-Request-ID": "request-binding-list"}
    )


async def test_three_families_are_projected_field_by_field(http: Any) -> None:
    sandbox, client = http
    create_draft(sandbox, "asr-alpha", "list-alpha-1", logical_model="asr-alpha-model")
    beta = create_draft(
        sandbox,
        "asr-beta",
        "list-beta-1",
        logical_model="asr-beta-model",
        release_tier="production",
    )
    publish(sandbox, beta, "list-beta-publish")
    create_draft(
        sandbox,
        "asr-beta",
        "list-beta-2",
        logical_model="asr-beta-model",
        release_tier="production",
    )
    gamma = create_draft(
        sandbox,
        "asr-gamma",
        "list-gamma-1",
        logical_model="asr-gamma-model",
        release_tier="best_effort",
    )
    publish(sandbox, gamma, "list-gamma-publish")
    add_tombstone(sandbox, gamma)

    response = await get(client)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "request-binding-list"
    assert response.json() == {
        "object": "nexus.list",
        "data": [
            {
                "binding_id": "asr-alpha",
                "latest_revision": "1",
                "current_revision": None,
                "logical_model": "asr-alpha-model",
                "tasks": ["asr_file", "asr_full_segment_review"],
                "release_tier": "experimental",
                "publication_status": "draft",
            },
            {
                "binding_id": "asr-beta",
                "latest_revision": "2",
                "current_revision": "1",
                "logical_model": "asr-beta-model",
                "tasks": ["asr_file", "asr_full_segment_review"],
                "release_tier": "production",
                "publication_status": "draft",
            },
            {
                "binding_id": "asr-gamma",
                "latest_revision": "1",
                "current_revision": None,
                "logical_model": "asr-gamma-model",
                "tasks": ["asr_file", "asr_full_segment_review"],
                "release_tier": "best_effort",
                "publication_status": "retired",
            },
        ],
        "has_more": False,
        "next_cursor": None,
    }


async def test_empty_registry_returns_an_empty_list(http: Any) -> None:
    _, client = http
    response = await get(client)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "object": "nexus.list",
        "data": [],
        "has_more": False,
        "next_cursor": None,
    }


async def test_keyset_pages_are_deterministic_at_boundaries(http: Any) -> None:
    sandbox, client = http
    # Insertion order c, a, b: a stable order cannot be an accident of the scan.
    for suffix in ("c", "a", "b"):
        create_draft(
            sandbox, f"page-{suffix}", f"page-{suffix}-key", logical_model=f"page-{suffix}-model"
        )

    first = await get(client, limit=2)
    assert first.status_code == 200, first.text
    assert [row["binding_id"] for row in first.json()["data"]] == ["page-a", "page-b"]
    assert first.json()["has_more"] is True
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    # Repeating the same page is stable and does not duplicate or skip a row.
    assert (await get(client, limit=2)).json() == first.json()

    last = await get(client, limit=2, cursor=cursor)
    assert last.status_code == 200, last.text
    assert [row["binding_id"] for row in last.json()["data"]] == ["page-c"]
    assert last.json()["has_more"] is False
    assert last.json()["next_cursor"] is None

    # One row per page, no duplicate and no missing identity.
    seen: list[str] = []
    cursor = None
    for _ in range(4):
        response = await get(client, limit=1, **({"cursor": cursor} if cursor else {}))
        assert response.status_code == 200, response.text
        seen.extend(row["binding_id"] for row in response.json()["data"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == ["page-a", "page-b", "page-c"]

    # A syntactically valid cursor past the last identity is an empty page, not
    # the first page again.
    past = await get(client, cursor=cursor_for("zzz-past-end", None))
    assert past.status_code == 200
    assert past.json()["data"] == []
    assert past.json()["has_more"] is False


async def test_status_filter_and_cursor_scope(http: Any) -> None:
    sandbox, client = http
    draft = create_draft(sandbox, "filter-draft", "filter-draft-1", logical_model="filter-draft")
    published = create_draft(
        sandbox, "filter-published", "filter-pub-1", logical_model="filter-published"
    )
    publish(sandbox, published, "filter-pub-publish")
    second = create_draft(
        sandbox, "filter-published-2", "filter-pub-2", logical_model="filter-published-2"
    )
    publish(sandbox, second, "filter-pub-2-publish")
    retired = create_draft(
        sandbox, "filter-retired", "filter-ret-1", logical_model="filter-retired"
    )
    publish(sandbox, retired, "filter-ret-publish")
    add_tombstone(sandbox, retired)
    disabled = create_draft(
        sandbox, "filter-disabled", "filter-dis-1", logical_model="filter-disabled"
    )
    publish(sandbox, disabled, "filter-dis-publish")
    add_tombstone(sandbox, disabled, mode="disable", reason="synthetic disable")

    for status, identities in (
        ("draft", (draft.binding_id,)),
        ("published", (published.binding_id, second.binding_id)),
        ("retired", (retired.binding_id,)),
        ("disabled", (disabled.binding_id,)),
    ):
        response = await get(client, status=status)
        assert response.status_code == 200, response.text
        assert tuple(row["binding_id"] for row in response.json()["data"]) == identities
        assert {row["publication_status"] for row in response.json()["data"]} == {status}

    listed = await get(client)
    assert [row["publication_status"] for row in listed.json()["data"]] == [
        "disabled",
        "draft",
        "published",
        "published",
        "retired",
    ]

    published_cursor = (await get(client, status="published", limit=1)).json()["next_cursor"]
    assert published_cursor is not None
    crossed = await get(client, status="draft", cursor=published_cursor)
    assert crossed.status_code == 400
    assert crossed.json()["error"]["code"] == "invalid_model_binding_cursor"

    for parameters in (
        {"status": "synthetic"},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not-a-cursor"},
        {"cursor": "x" * 513},
        {"unknown": "value"},
    ):
        invalid = await get(client, **parameters)
        assert invalid.status_code == 400, (parameters, invalid.text)
        assert invalid.json()["error"]["type"] == "invalid_request_error"


async def test_disable_status_is_projected(http: Any) -> None:
    sandbox, client = http
    disabled = create_draft(
        sandbox, "asr-disabled", "list-disabled-1", logical_model="asr-disabled-model"
    )
    publish(sandbox, disabled, "list-disabled-publish")
    add_tombstone(sandbox, disabled, mode="disable", reason="synthetic disable")

    response = await get(client, status="disabled")
    assert response.status_code == 200, response.text
    row = response.json()["data"][0]
    assert row["binding_id"] == "asr-disabled"
    assert row["current_revision"] is None
    assert row["publication_status"] == "disabled"


async def test_unreadable_family_fails_closed_instead_of_being_omitted(http: Any) -> None:
    sandbox, client = http
    create_draft(sandbox, "asr-integrity", "integrity-key", logical_model="asr-integrity")
    stored = sandbox.admin.execute(
        f'SELECT definition_digest FROM "{sandbox.schema}".speech_binding_revisions '
        "WHERE binding_id = 'asr-integrity' AND revision = '1'"
    ).fetchone()
    assert stored is not None
    sandbox.admin.execute(
        f'UPDATE "{sandbox.schema}".speech_binding_revisions SET definition_digest = %s '
        "WHERE binding_id = 'asr-integrity' AND revision = '1'",
        ("sha256:" + "0" * 64,),
    )
    broken = await get(client)
    assert broken.status_code == 503
    assert broken.json()["error"]["code"] == "model_binding_service_unavailable"

    sandbox.admin.execute(
        f'UPDATE "{sandbox.schema}".speech_binding_revisions SET definition_digest = %s '
        "WHERE binding_id = 'asr-integrity' AND revision = '1'",
        (stored["definition_digest"],),
    )
    sandbox.admin.execute(
        f'UPDATE "{sandbox.schema}".speech_binding_families SET latest_revision = %s '
        "WHERE binding_id = 'asr-integrity'",
        ("2",),
    )
    missing = await get(client)
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "model_binding_service_unavailable"


def test_the_list_reader_identity_cannot_write(sandbox: Sandbox) -> None:
    create_draft(sandbox, "asr-reader", "reader-key", logical_model="asr-reader")
    with sandbox.pools["list"].connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        for table in READABLE:
            connection.execute(f'SELECT count(*) FROM "{sandbox.schema}"."{table}"').fetchone()
    connection = psycopg.connect(sandbox.urls["list"], autocommit=True)
    connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    try:
        for statement in (
            "INSERT INTO speech_binding_families (binding_id) VALUES ('asr-write')",
            "UPDATE speech_binding_families SET latest_revision = '9'",
            "DELETE FROM speech_binding_families",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
    finally:
        connection.close()


def test_runtime_rejects_a_writer_or_a_narrow_reader_before_serving(
    sandbox: Sandbox,
) -> None:
    runtime = BindingListRuntime(settings_for(sandbox))
    runtime.open()
    try:
        # Bind the opened service to a local so the assertion does not narrow the
        # attribute across close(), which would make the post-close check dead.
        opened = runtime.service
        assert opened is not None
    finally:
        runtime.close()
    assert runtime.service is None

    writer = NamedRole(sandbox, "runtime_writer", writable=True)
    try:
        with pytest.raises(ValueError, match="Binding list reader startup rejected"):
            BindingListRuntime(settings_for(sandbox, reader_url=writer.url)).open()
    finally:
        writer.drop()

    narrow = NamedRole(
        sandbox,
        "runtime_narrow",
        readable=tuple(table for table in READABLE if table != "speech_publication_heads"),
    )
    try:
        # Every relation except the publication head is readable; startup must
        # reject the surface instead of failing later on a partial read.
        with pytest.raises(ValueError, match="Binding list reader startup rejected"):
            BindingListRuntime(settings_for(sandbox, reader_url=narrow.url)).open()
    finally:
        narrow.drop()


@dataclass
class NamedRole:
    sandbox: Sandbox
    name: str
    writable: bool = False
    readable: tuple[str, ...] = READABLE
    role: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.role = f"sn_bindlist_{self.name}_{suffix}"
        self.sandbox.admin.execute(f"CREATE ROLE \"{self.role}\" LOGIN PASSWORD '{PASSWORD}'")
        self.sandbox.admin.execute(
            f'GRANT USAGE ON SCHEMA "{self.sandbox.schema}" TO "{self.role}"'
        )
        for table in self.readable:
            self.sandbox.admin.execute(
                f'GRANT SELECT ON "{self.sandbox.schema}"."{table}" TO "{self.role}"'
            )
        if self.writable:
            self.sandbox.admin.execute(
                f'GRANT INSERT ON "{self.sandbox.schema}".speech_binding_families TO "{self.role}"'
            )
        base = self.sandbox.urls["writer"]
        self.url = make_conninfo(base, user=self.role, password=PASSWORD)

    def drop(self) -> None:
        self.sandbox.admin.execute(f'DROP OWNED BY "{self.role}"')
        self.sandbox.admin.execute(f'DROP ROLE IF EXISTS "{self.role}"')


async def test_route_answers_503_when_no_reader_identity_is_configured(sandbox: Sandbox) -> None:
    # With every model binding surface unconfigured the ingress refuses first.
    app = create_app(
        settings=settings_for(sandbox, reader_url=None),
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await get(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "binding_unavailable"

    # A configured writer keeps the ingress open; the list still reports its own
    # unavailable read service rather than advertising an empty registry.
    app = create_app(
        settings=settings_for(sandbox, reader_url=None),
        bindings=cast(Any, object()),
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await get(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_binding_service_unavailable"
