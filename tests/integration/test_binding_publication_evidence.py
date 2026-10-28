"""Real PostgreSQL proof for publication evidence, honest GET and backfill.

Synthetic identities prove storage, projection and permission semantics only;
they do not qualify a production deployment, real speech engine or read replica.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from speechnexus_contracts.model_bindings import parse as parse_binding
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.backfill_binding_evidence import main as backfill_main
from speechnexus_backend.publish_binding import publish_binding
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.binding_publication import BindingPublishIntent
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import (
    PostgresBindingDrafts,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    TABLE as EVIDENCE,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
    publication_grants,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_publication_evidence"
PATH = "/v1/nexus/admin/model-bindings"
CAPACITY = "cap-asr-quality-7"
REPORT = "qrep-asr-quality-7"
ROLES = ("writer", "publication", "reader", "catalogue")
POOL_STATS = frozenset(
    (
        "pool_min",
        "pool_max",
        "pool_size",
        "pool_available",
        "requests_waiting",
        "requests_num",
        "requests_queued",
        "requests_wait_ms",
        "requests_errors",
        "usage_ms",
        "returns_bad",
        "connections_num",
        "connections_ms",
        "connections_errors",
        "connections_lost",
    )
)


@dataclass
class Sandbox:
    admin: psycopg.Connection[tuple[object, ...]]
    schema: str
    urls: dict[str, str]
    pools: dict[str, SpeechPool]


@pytest.fixture
def sandbox(request: pytest.FixtureRequest) -> Iterator[Sandbox]:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.skip("Configure an isolated PostgreSQL DSN; skip is not evidence")
    suffix = uuid.uuid4().hex[:10]
    schema = "sn_evi_" + suffix
    names = {role: f"sn_evi_{role}_{suffix}" for role in ROLES}
    engine = create_engine(
        base.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    legacy_migration_test = (
        request.node.name == "test_downgrade_refuses_nonempty_evidence_and_preserves_publications"
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(
                config, "0021_publication_evidence" if legacy_migration_test else "head"
            )
    finally:
        engine.dispose()
    admin = psycopg.connect(base, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    for role in ROLES:
        admin.execute(f"CREATE ROLE \"{names[role]}\" LOGIN PASSWORD '{PASSWORD}'")
    grants: tuple[tuple[str, Callable[[str, str], tuple[str, ...]]], ...] = (
        (names["writer"], binding_draft_grants),
        (names["publication"], binding_draft_grants),
        (names["publication"], publication_grants),
        (names["publication"], publication_evidence_grants),
        (names["reader"], binding_admin_reader_grants),
        (names["catalogue"], model_catalog_reader_grants),
    )
    if legacy_migration_test:
        # This test only writes drafts and exercises the 0021 downgrade.
        # Later runtime grants refer to tables introduced after that revision.
        grants = ((names["writer"], binding_draft_grants),)
    for role, declaration in grants:
        for statement in declaration(schema, role):
            admin.execute(statement)
    urls = {role: make_conninfo(base, user=names[role], password=PASSWORD) for role in ROLES}
    pools: dict[str, SpeechPool] = {}
    for role in ROLES:
        pool = speech_pool(urls[role], schema=schema, max_size=2, name=f"evi-{role}-" + suffix)
        pool.open()
        pools[role] = pool
    try:
        yield Sandbox(admin, schema, urls, pools)
    finally:
        for pool in pools.values():
            pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in ROLES:
            admin.execute(f'DROP ROLE IF EXISTS "{names[role]}"')
        admin.close()


def settings(sandbox: Sandbox) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "database_schema": sandbox.schema,
            "binding_writer_database_url": sandbox.urls["writer"],
            "binding_admin_reader_database_url": sandbox.urls["reader"],
            "asset_allowed_hosts": ("testserver", "test", "127.0.0.1"),
            "binding_receive_concurrency": 1,
            "binding_transaction_timeout_seconds": 2,
        }
    )


def fixed(body: dict[str, Any], revision: str = "1") -> str:
    return PATH + f"/{body['binding_id']}/revisions/{revision}"


def create_draft(sandbox: Sandbox, key: str, *, binding_id: str = "asr-quality") -> BindingDraft:
    body = {**binding_request(), "binding_id": binding_id}
    result = PostgresBindingDrafts(sandbox.pools["writer"]).create(prepare_binding_draft(body, key))
    assert result.outcome == "created", result
    assert result.draft is not None
    return result.draft


def add_legacy_publication(sandbox: Sandbox, draft: BindingDraft) -> None:
    """Simulate a B-era publication written without evidence."""
    sandbox.admin.execute(
        f'INSERT INTO "{sandbox.schema}".speech_publications '
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding', %s, %s, %s, 1700000000000000)",
        (draft.binding_id, draft.revision, draft.revision_id),
    )


def counts(sandbox: Sandbox) -> tuple[int, int, int]:
    row = sandbox.admin.execute(
        f'SELECT (SELECT count(*) FROM "{sandbox.schema}".speech_publications), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_revisions), '
        f'(SELECT count(*) FROM "{sandbox.schema}".{EVIDENCE})'
    ).fetchone()
    assert row is not None
    publications, revisions, evidence = row
    assert isinstance(publications, int) and isinstance(revisions, int)
    assert isinstance(evidence, int)
    return publications, revisions, evidence


class PublicationWaitSampler:
    """Observe only the actual borrowed connection, using a separate bounded admin session."""

    def __init__(self, pool: SpeechPool) -> None:
        self.pool = pool
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.pid: int | None = None
        self.samples: list[dict[str, object]] = []
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[SpeechConnection]:
        with self.pool.connection(timeout=timeout) as connection:
            self.pid = connection.info.backend_pid
            self.ready.set()
            yield connection

    def _sample(self) -> None:
        try:
            if not self.ready.wait(timeout=4) or self.stopping.is_set() or self.pid is None:
                return
            with psycopg.connect(
                os.environ[VARIABLE],
                autocommit=True,
                connect_timeout=2,
                options="-c statement_timeout=500",
            ) as observer:
                while not self.stopping.is_set() and len(self.samples) < 64:
                    row = observer.execute(
                        "SELECT state, wait_event_type, wait_event, "
                        "cardinality(pg_blocking_pids(pid)) FROM pg_stat_activity WHERE pid=%s",
                        (self.pid,),
                    ).fetchone()
                    if row is not None:
                        state, event_type, event, blockers = row
                        if state not in {
                            "active",
                            "idle",
                            "idle in transaction",
                            "fastpath function call",
                            "disabled",
                            None,
                        }:
                            state = "unknown"
                        if not isinstance(event_type, str) or not re.fullmatch(
                            r"[A-Za-z0-9_]{1,64}", event_type
                        ):
                            event_type = None
                        if not isinstance(event, str) or not re.fullmatch(
                            r"[A-Za-z0-9_]{1,64}", event
                        ):
                            event = None
                        sample = {
                            "state": state,
                            "wait_event_type": event_type,
                            "wait_event": event,
                            "blockers": blockers,
                        }
                        if not self.samples or self.samples[-1] != sample:
                            self.samples.append(sample)
                    self.stopping.wait(0.05)
        except Exception:
            # Diagnostics cannot change test or product behavior.
            return

    def close(self) -> None:
        self.stopping.set()
        self.ready.set()
        self.thread.join(timeout=3)


def publish_intent(
    sandbox: Sandbox, binding_id: str, key: str, *, sample_waits: bool = False
) -> BindingPublishIntent:
    pool = sandbox.pools["publication"]
    sampler = None
    if sample_waits:
        # Diagnostic startup must not replace a real publication result.
        with suppress(Exception):
            sampler = PublicationWaitSampler(pool)
    started = time.monotonic()
    finished = started
    try:
        try:
            observed_pool = cast(SpeechPool, sampler) if sampler is not None else pool
            observation = PostgresPublications(observed_pool).observe_binding_head(binding_id)
        finally:
            finished = time.monotonic()
            if sampler is not None:
                with suppress(Exception):
                    sampler.close()
    except PublicationUnavailableError as error:
        # Diagnostic data is intentionally limited to types and numeric pool facts;
        # exception text, query text, connection information and parameters stay private.
        try:
            chain: list[dict[str, str]] = []
            seen: set[int] = set()
            current: BaseException | None = error
            while current is not None and id(current) not in seen and len(chain) < 16:
                seen.add(id(current))
                entry = {"type": type(current).__name__}
                sqlstate = getattr(current, "sqlstate", None)
                if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
                    entry["sqlstate"] = sqlstate
                if isinstance(current, psycopg.errors.QueryCanceled):
                    primary = current.diag.message_primary
                    entry["cancel_reason"] = {
                        "canceling statement due to statement timeout": "statement_timeout",
                        "canceling statement due to user request": "user_cancel",
                        "canceling statement due to lock timeout": "lock_timeout",
                    }.get(primary or "", "unknown")
                chain.append(entry)
                current = current.__context__
            stats = {
                name: value
                for name, value in pool.get_stats().items()
                if name in POOL_STATS and type(value) is int
            }
            print(
                json.dumps(
                    {
                        "chain": chain,
                        "elapsed_ms": round((finished - started) * 1000),
                        "pool_closed": pool.closed,
                        "pool_stats": stats,
                        "wait_samples": sampler.samples if sampler is not None else [],
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        except Exception:
            # Never replace the original failure if diagnostic collection fails.
            pass
        raise
    return BindingPublishIntent.from_request(
        installation_id=str(observation["installation_id"]),
        database=str(observation["database_name"]),
        schema=sandbox.schema,
        expected_current_revision=None,
        expected_catalog_revision=int(observation["catalog_revision"]),
        idempotency_key=key,
    )


def connection(sandbox: Sandbox, role: str) -> psycopg.Connection[tuple[object, ...]]:
    conn = psycopg.connect(sandbox.urls[role], autocommit=True)
    conn.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    return conn


def test_publish_intent_failure_reports_only_safe_diagnostics(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = PublicationUnavailableError("synthetic-private-exception")

    def fail_observation(_store: PostgresPublications, _binding_id: str) -> dict[str, Any]:
        try:
            raise psycopg.errors.InsufficientPrivilege("synthetic-private-sql-parameter")
        except psycopg.Error:
            raise failure from None

    monkeypatch.setattr(PostgresPublications, "observe_binding_head", fail_observation)
    with pytest.raises(PublicationUnavailableError) as raised:
        publish_intent(sandbox, "synthetic-private-binding", "synthetic-private-key")
    assert raised.value is failure
    output = capsys.readouterr()
    assert output.out == ""
    assert "synthetic-private" not in output.err
    diagnostic = json.loads(output.err)
    assert diagnostic["chain"] == [
        {"type": "PublicationUnavailableError"},
        {"type": "InsufficientPrivilege", "sqlstate": "42501"},
    ]
    assert type(diagnostic["elapsed_ms"]) is int and diagnostic["elapsed_ms"] >= 0
    assert diagnostic["pool_closed"] is False
    assert diagnostic["pool_stats"]["pool_max"] == 2
    assert all(type(value) is int for value in diagnostic["pool_stats"].values())


def test_publish_intent_diagnostic_observes_real_table_lock(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = create_draft(sandbox, "diagnostic-lock")
    with psycopg.connect(os.environ[VARIABLE]) as holder:
        holder.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        holder.execute("LOCK TABLE speech_publication_installation IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(PublicationUnavailableError):
            publish_intent(sandbox, draft.binding_id, "diagnostic-lock", sample_waits=True)
    output = capsys.readouterr()
    assert output.out == ""
    diagnostic = json.loads(output.err)
    assert {
        "type": "QueryCanceled",
        "sqlstate": "57014",
        "cancel_reason": "statement_timeout",
    } in diagnostic["chain"]
    assert any(
        sample["state"] == "active"
        and sample["wait_event_type"] == "Lock"
        and sample["wait_event"] == "relation"
        and sample["blockers"] >= 1
        for sample in diagnostic["wait_samples"]
    )
    assert sandbox.schema not in output.err


def test_get_reports_draft_published_and_unavailable(sandbox: Sandbox) -> None:
    body = binding_request()
    with TestClient(create_app(settings=settings(sandbox))) as client:
        created = client.post(PATH, json=body, headers={"Idempotency-Key": "evidence-http-key"})
        assert created.status_code == 201, created.text
        created_wire = created.json()
        draft = PostgresBindingDrafts(sandbox.pools["writer"]).get(body["binding_id"], "1")
        assert draft is not None

        draft_wire = client.get(fixed(body)).json()
        assert draft_wire["publication"] == created_wire["publication"]
        assert draft_wire["publication"]["status"] == "draft"
        assert draft_wire["publication"]["capacity_manifest_id"] is None

        result = publish_binding(
            database_url=sandbox.urls["publication"],
            schema=sandbox.schema,
            binding_id=draft.binding_id,
            revision=int(draft.revision),
            revision_id=draft.revision_id,
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            expected_current_revision=None,
            intent=publish_intent(sandbox, draft.binding_id, "evidence-test", sample_waits=True),
        )
        assert result.created is True

        published = client.get(fixed(body))
        assert published.status_code == 200, published.text
        wire = published.json()
        parse_binding("ModelBindingRevision", wire)
        assert wire["publication"]["status"] == "published"
        assert wire["publication"]["capacity_manifest_id"] == CAPACITY
        assert wire["publication"]["qualification_report_id"] == REPORT
        assert wire["publication"]["published_at"].endswith("Z")
        assert wire["publication"]["retired_at"] is None
        assert wire["publication"]["disabled_reason"] is None

        legacy = {**binding_request(), "binding_id": "legacy-binding"}
        legacy_created = client.post(
            PATH, json=legacy, headers={"Idempotency-Key": "legacy-http-key"}
        )
        assert legacy_created.status_code == 201, legacy_created.text
        legacy_draft = PostgresBindingDrafts(sandbox.pools["writer"]).get("legacy-binding", "1")
        assert legacy_draft is not None
        add_legacy_publication(sandbox, legacy_draft)

        unavailable = client.get(fixed(legacy))
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "binding_unavailable"

    jobs = PostgresPublicJobs(sandbox.pools["publication"])
    assert jobs.latest_binding("legacy-binding") is None, (
        "public resolution must not select a publication that admin GET cannot project"
    )
    assert counts(sandbox) == (2, 2, 1)


def test_writer_reader_and_catalogue_permissions_are_narrow(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox, "permission-key")
    add_legacy_publication(sandbox, draft)
    evidence_sql = (
        f'INSERT INTO "{sandbox.schema}".{EVIDENCE} '
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us) "
        "VALUES ('binding', %s, %s, %s, %s, %s, 'backfill', 1)"
    )
    arguments = (draft.binding_id, draft.revision, draft.revision_id, CAPACITY, REPORT)

    writer = connection(sandbox, "writer")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute(f'SELECT count(*) FROM "{sandbox.schema}".speech_publications')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute(evidence_sql, arguments)
    finally:
        writer.close()

    reader = connection(sandbox, "reader")
    try:
        reader.execute(f'SELECT count(*) FROM "{sandbox.schema}".{EVIDENCE}').fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(evidence_sql, arguments)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(
                f'INSERT INTO "{sandbox.schema}".speech_publications '
                "(kind, object_id, revision, revision_id, published_at_us) "
                "VALUES ('binding', 'reader-attempt', '1', 'mbrev-reader-0001', 1)"
            )
    finally:
        reader.close()

    catalogue = connection(sandbox, "catalogue")
    try:
        catalogue.execute(f'SELECT count(*) FROM "{sandbox.schema}".speech_publications').fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            catalogue.execute(f'SELECT count(*) FROM "{sandbox.schema}".{EVIDENCE}').fetchone()
    finally:
        catalogue.close()

    assert counts(sandbox) == (1, 1, 0)


def test_backfill_preflight_dry_run_apply_idempotent_conflict_and_rollback(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = create_draft(sandbox, "backfill-first")
    add_legacy_publication(sandbox, first)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.urls["publication"])
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)

    with pytest.raises(SystemExit) as blocked:
        backfill_main(["--preflight"])
    assert blocked.value.code == 10
    assert "preflight publications=1" in capsys.readouterr().out

    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            [
                {
                    "object_id": first.binding_id,
                    "revision": first.revision,
                    "revision_id": first.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": REPORT,
                }
            ]
        )
    )
    backfill_main(["--dry-run", "--mapping", str(mapping)])
    dry = capsys.readouterr().out
    assert "insert=1" in dry and "unresolved_after=0" in dry

    backfill_main(["--apply", "--mapping", str(mapping)])
    applied = capsys.readouterr().out
    assert "inserted=1" in applied and "evidence_after=1" in applied
    assert counts(sandbox) == (1, 1, 1)

    backfill_main(["--apply", "--mapping", str(mapping)])
    replay = capsys.readouterr().out
    assert "inserted=0" in replay and "skipped=1" in replay
    assert "evidence_before=1" in replay and "evidence_after=1" in replay
    assert counts(sandbox) == (1, 1, 1)

    backfill_main(["--preflight"])
    assert "unresolved=0" in capsys.readouterr().out

    conflicting = tmp_path / "conflicting.json"
    conflicting.write_text(
        json.dumps(
            [
                {
                    "object_id": first.binding_id,
                    "revision": first.revision,
                    "revision_id": first.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": "qrep-other-0001",
                }
            ]
        )
    )
    with pytest.raises(SystemExit) as conflict:
        backfill_main(["--apply", "--mapping", str(conflicting)])
    assert conflict.value.code == 11
    assert counts(sandbox) == (1, 1, 1)

    second = create_draft(sandbox, "backfill-second", binding_id="second-binding")
    third = create_draft(sandbox, "backfill-third", binding_id="third-binding")
    add_legacy_publication(sandbox, second)
    add_legacy_publication(sandbox, third)
    rollback = tmp_path / "rollback.json"
    rollback.write_text(
        json.dumps(
            [
                {
                    "object_id": second.binding_id,
                    "revision": second.revision,
                    "revision_id": second.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": REPORT,
                },
                {
                    "object_id": third.binding_id,
                    "revision": third.revision,
                    "revision_id": third.revision_id,
                    "capacity_manifest_id": "cap-wrong-0001",
                    "qualification_report_id": REPORT,
                },
            ]
        )
    )
    with pytest.raises(SystemExit) as rejected:
        backfill_main(["--apply", "--mapping", str(rollback)])
    assert rejected.value.code == 11
    assert counts(sandbox) == (3, 3, 1), "a later mapping failure must roll back earlier inserts"


def test_backfill_export_preserves_recorded_evidence(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    draft = create_draft(sandbox, "export-key")
    add_legacy_publication(sandbox, draft)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.urls["publication"])
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)
    mapping = tmp_path / "export.json"
    mapping.write_text(
        json.dumps(
            [
                {
                    "object_id": draft.binding_id,
                    "revision": draft.revision,
                    "revision_id": draft.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": REPORT,
                }
            ]
        )
    )
    backfill_main(["--apply", "--mapping", str(mapping)])
    capsys.readouterr()

    backfill_main(["--export"])

    exported = json.loads(capsys.readouterr().out)
    assert exported[0]["capacity_manifest_id"] == CAPACITY
    assert exported[0]["qualification_report_id"] == REPORT
    assert exported[0]["evidence_source"] == "backfill"


def test_downgrade_refuses_nonempty_evidence_and_preserves_publications(
    sandbox: Sandbox,
) -> None:
    draft = create_draft(sandbox, "downgrade-key")
    add_legacy_publication(sandbox, draft)
    sandbox.admin.execute(
        f'INSERT INTO "{sandbox.schema}".{EVIDENCE} '
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us) "
        "VALUES ('binding', %s, %s, %s, %s, %s, 'operator_cli', 1)",
        (draft.binding_id, draft.revision, draft.revision_id, CAPACITY, REPORT),
    )
    before = counts(sandbox)
    assert before == (1, 1, 1)
    engine = create_engine(
        os.environ[VARIABLE].replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = sandbox.schema
    try:
        with engine.connect() as db:
            config.attributes["connection"] = db
            with pytest.raises(RuntimeError, match="export and preservation"):
                migration.downgrade(config, "0020_artifacts")
    finally:
        engine.dispose()

    sandbox.admin.execute(f'DELETE FROM "{sandbox.schema}".{EVIDENCE}')
    engine = create_engine(
        os.environ[VARIABLE].replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as db:
            config.attributes["connection"] = db
            migration.downgrade(config, "0020_artifacts")
    finally:
        engine.dispose()

    remaining = sandbox.admin.execute(
        "SELECT to_regclass(%s)", (f"{sandbox.schema}.{EVIDENCE}",)
    ).fetchone()
    assert remaining is not None and remaining[0] is None
    kept = sandbox.admin.execute(
        f'SELECT (SELECT count(*) FROM "{sandbox.schema}".speech_publications), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_revisions)'
    ).fetchone()
    assert kept == (1, 1), "downgrade must not touch publication or revision rows"
