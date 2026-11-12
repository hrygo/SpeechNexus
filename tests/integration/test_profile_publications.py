"""Isolated PG production CAS; all qualification success is a TEST DOUBLE."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.profile_examples import profile_request
from tests.unit.test_profile_lifecycle import request

from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.domain.profile_drafts import ProfileDraft
from speechnexus_backend.speech.domain.profile_lifecycle import (
    ProfileLifecycleCommand,
    ProfilePublicationIneligibleError,
    ProfilePublicationObservation,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.profile_draft_schema import profile_draft_grants
from speechnexus_backend.speech.infrastructure.profile_publication_schema import (
    profile_publication_grants,
)
from speechnexus_backend.speech.infrastructure.profile_publications_postgres import PostgresProfiles
from speechnexus_backend.speech.interfaces.profile_drafts import prepare_profile_draft
from speechnexus_backend.speech.interfaces.profile_lifecycle import (
    prepare_profile_activation,
    prepare_profile_publication,
    prepare_profile_retirement,
)

pytestmark = pytest.mark.integration


def migration_head(config: Config) -> str:
    """Return the single current head so a new migration never breaks this assertion."""
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


class SyntheticVerifier:
    """TEST DOUBLE: shape-bound observation, never real release qualification."""

    reject = False
    calls = 0

    def verify(
        self, command: ProfileLifecycleCommand, target: ProfileDraft
    ) -> ProfilePublicationObservation:
        self.calls += 1
        if self.reject:
            raise ProfilePublicationIneligibleError
        return ProfilePublicationObservation(
            command.profile_id,
            command.revision,
            document_digest(target.definition),
            command.request_digest,
            "sha256:" + "a" * 64,
        )


@dataclass
class Sandbox:
    admin: Any
    pool: SpeechPool
    port: PostgresProfiles
    verifier: SyntheticVerifier
    schema: str
    role: str
    url: str

    def publish(
        self, revision: str = "1", expected: str | None = None, key: str = "publication-key"
    ) -> ProfileLifecycleCommand:
        body = request()["body"]
        body["expected_revision"] = revision
        body["expected_current_revision"] = expected
        body["validation"]["profile_id"] = profile_request()["profile_id"]
        body["validation"]["revision"] = revision
        return prepare_profile_publication(profile_request()["profile_id"], revision, body, key)

    def activate(
        self, revision: str = "1", expected: str | None = None, key: str = "activation-key"
    ) -> ProfileLifecycleCommand:
        return prepare_profile_activation(
            profile_request()["profile_id"], revision, {"expected_current_revision": expected}, key
        )

    def retirement(
        self,
        body: dict[str, Any],
        key: str = "retirement-key",
        revision: str = "1",
    ) -> ProfileLifecycleCommand:
        return prepare_profile_retirement(profile_request()["profile_id"], revision, body, key)

    def draft(self, revision: str = "1") -> None:
        result = self.port.create(
            prepare_profile_draft(
                {
                    **profile_request(),
                    "expected_latest_revision": None if revision == "1" else str(int(revision) - 1),
                },
                "draft-key-" + revision,
            )
        )
        assert result.draft is not None and result.draft.revision == revision

    def snapshot(self) -> dict[str, Any]:
        return {
            t: self.admin.execute("SELECT * FROM " + t + " ORDER BY 1,2").fetchall()
            for t in (
                "speech_profile_families",
                "speech_profile_revisions",
                "speech_profile_publications",
                "speech_profile_lifecycle_intents",
                "speech_profile_lifecycle_results",
            )
        }


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not evidence"
        )
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_profile_" + suffix, "sn_profile_role_" + suffix
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = schema
    pool = None
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
        admin.execute(f'SET search_path TO "{schema}",pg_catalog')
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_profile_test'")
        for grant in (
            *profile_draft_grants(schema, role),
            *profile_publication_grants(schema, role),
        ):
            admin.execute(grant)
        pool = speech_pool(
            make_conninfo(url, user=role, password="synthetic_profile_test"),
            schema=schema,
            max_size=4,
        )
        pool.open()
        verifier = SyntheticVerifier()
        yield Sandbox(
            admin, pool, PostgresProfiles(pool, verifier=verifier), verifier, schema, role, url
        )
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()
        engine.dispose()


def test_publication_activation_replay_before_cas_and_same_key_conflict(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    first = s.port.mutate(s.publish())
    assert first.outcome == "completed"
    s.draft("2")
    assert s.port.mutate(s.publish("2", "1", "publish-second")).outcome == "completed"
    before = s.snapshot()
    s.verifier.reject = True
    replay = s.port.mutate(s.publish())
    assert replay.outcome == "replayed" and replay.publication == first.publication
    assert s.port.recover_lifecycle(s.publish()).publication == first.publication
    assert s.snapshot() == before
    assert s.port.mutate(s.publish(expected="2")).error_code == "idempotency_conflict"
    assert s.port.mutate(s.activate("1", "2")).outcome == "completed"
    rejected = s.activate("2", "2", "stale-key")
    assert s.port.mutate(rejected).error_code == "configuration_changed"
    assert s.port.mutate(s.activate("2", "1", "advance-key")).outcome == "completed"
    assert s.port.mutate(rejected).error_code == "configuration_changed"


def test_unregistered_evidence_rolls_back_all_facts_and_qualification_failure_is_stable(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    s.draft()
    before = s.snapshot()
    unavailable = PostgresProfiles(s.pool)
    assert unavailable.mutate(s.publish()).outcome == "unknown"
    assert s.snapshot() == before
    s.verifier.reject = True
    assert s.port.mutate(s.publish()).error_code == "profile_not_eligible"
    after = s.snapshot()
    s.verifier.reject = False
    assert s.port.mutate(s.publish()).error_code == "profile_not_eligible"
    assert s.snapshot() == after
    assert not after["speech_profile_publications"]
    assert after["speech_profile_families"][0]["current_revision"] is None


def test_competing_activations_wait_on_family_and_only_one_cas_wins(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    s.port.mutate(s.publish())
    s.draft("2")
    s.port.mutate(s.publish("2", "1", "publish-second"))
    s.draft("3")
    s.port.mutate(s.publish("3", "2", "publish-third"))
    blocker = psycopg.connect(s.url)
    try:
        blocker.execute(f'SET search_path TO "{s.schema}",pg_catalog')
        blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [
                workers.submit(s.port.mutate, s.activate(r, "3", "racing-" + r)) for r in ("1", "2")
            ]
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    n = s.admin.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                        "AND cardinality(pg_blocking_pids(pid))>0",
                        (s.role,),
                    ).fetchone()["n"]
                    if n == 2:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("Two actual independent PG lock waits were not observed")
            finally:
                blocker.rollback()
            results = [f.result(timeout=5) for f in futures]
        assert sorted(r.outcome for r in results) == ["completed", "rejected"]
        assert [r.error_code for r in results if r.outcome == "rejected"] == [
            "configuration_changed"
        ]
    finally:
        blocker.close()


def test_same_target_concurrent_activation_is_one_documented_noop(sandbox: Sandbox) -> None:
    """Both requests are the contract's no-op; the family row is not written."""
    s = sandbox
    s.draft()
    s.port.mutate(s.publish())
    s.draft("2")
    s.port.mutate(s.publish("2", "1", "publish-second"))
    assert s.port.mutate(s.activate("1", "2", "back-to-one")).outcome == "completed"
    before = s.snapshot()
    xmin_before = s.admin.execute(
        "SELECT xmin::text AS xmin FROM speech_profile_families"
    ).fetchone()["xmin"]
    blocker = psycopg.connect(s.url)
    try:
        blocker.execute(f'SET search_path TO "{s.schema}",pg_catalog')
        blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
        commands = [s.activate("1", "1", "same-" + suffix) for suffix in ("alpha", "bravo")]
        blocked_queries: list[str] = []
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(s.port.mutate, command) for command in commands]
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    blocked = s.admin.execute(
                        "SELECT query FROM pg_stat_activity WHERE usename=%s "
                        "AND cardinality(pg_blocking_pids(pid))>0",
                        (s.role,),
                    ).fetchall()
                    if len(blocked) == 2:
                        blocked_queries = [row["query"] or "" for row in blocked]
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("Two actual independent PG lock waits were not observed")
            finally:
                blocker.rollback()
            results = [f.result(timeout=5) for f in futures]
        # The family FOR UPDATE is the only serializing statement here. A
        # same-value terminal UPDATE would also block, so the text must prove
        # the row lock came from FOR UPDATE rather than the skipped write.
        assert len(blocked_queries) == 2
        assert all(
            "FOR UPDATE" in query and "speech_profile_families" in query
            for query in blocked_queries
        ), blocked_queries
        assert [r.outcome for r in results] == ["completed", "completed"]
        assert [r.publication.draft.revision for r in results if r.publication] == ["1", "1"]
        assert results[0].publication == results[1].publication
        for command in commands:
            recovered = s.port.recover_lifecycle(command)
            assert recovered.outcome == "replayed"
            assert recovered.publication == results[0].publication
    finally:
        blocker.close()
    after = s.snapshot()
    # Only the per-key receipts differ; the family row keeps its value and version.
    assert after["speech_profile_families"] == before["speech_profile_families"]
    assert after["speech_profile_publications"] == before["speech_profile_publications"]
    assert (
        s.admin.execute("SELECT xmin::text AS xmin FROM speech_profile_families").fetchone()["xmin"]
        == xmin_before
    )
    assert all(row["current_revision"] == "1" for row in after["speech_profile_families"])
    assert (
        len(after["speech_profile_lifecycle_results"])
        == len(before["speech_profile_lifecycle_results"]) + 2
    )


def test_draft_retired_disabled_and_missing_cannot_be_activated(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.activate()).error_code == "profile_not_published"
    assert s.port.mutate(s.activate("99", key="missing-key")).error_code == "profile_not_found"
    s.port.mutate(s.publish())
    s.admin.execute(
        "UPDATE speech_profile_publications SET status='retired',"
        "retired_at_us=published_at_us WHERE revision='1'"
    )
    assert s.port.mutate(s.activate("1", "1", "retired-key")).error_code == "profile_not_published"
    s.admin.execute(
        "UPDATE speech_profile_publications SET status='disabled',"
        "disabled_reason='synthetic' WHERE revision='1'"
    )
    assert s.port.mutate(s.activate("1", "1", "disabled-key")).error_code == "profile_not_published"


def test_intent_update_is_rejected_while_row_lock_and_normal_mutations_work(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    legacy = s.publish(key="legacy-intent-key")
    s.admin.execute(
        "INSERT INTO speech_profile_lifecycle_intents "
        "(key_digest,operation,profile_id,revision,intent_json,request_digest,recorded_at_us) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            legacy.key_digest,
            legacy.operation,
            legacy.profile_id,
            legacy.revision,
            legacy.intent.decode("utf-8"),
            legacy.request_digest,
            1_000_000,
        ),
    )
    with (
        pytest.raises(psycopg.errors.IntegrityError, match="append-only"),
        s.pool.connection() as connection,
    ):
        connection.execute(
            "UPDATE speech_profile_lifecycle_intents SET key_digest=%s WHERE key_digest=%s",
            ("sha256:" + "0" * 64, legacy.key_digest),
        )
    assert s.admin.execute(
        "SELECT key_digest FROM speech_profile_lifecycle_intents WHERE key_digest=%s",
        (legacy.key_digest,),
    ).fetchall() == [{"key_digest": legacy.key_digest}]
    assert s.port.recover_lifecycle(legacy).outcome == "unknown"
    with s.pool.connection() as connection:
        locked = connection.execute(
            "SELECT key_digest FROM speech_profile_lifecycle_intents "
            "WHERE key_digest=%s FOR UPDATE",
            (legacy.key_digest,),
        ).fetchone()
    assert locked == {"key_digest": legacy.key_digest}
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    assert s.port.mutate(s.activate("1", "1", "normal-activation")).outcome == "completed"


def test_writer_and_reader_privileges_and_real_runtime_fail_closed(sandbox: Sandbox) -> None:
    from fastapi.testclient import TestClient
    from tests.unit.backend.test_health import _settings

    from speechnexus_backend.app import create_app
    from speechnexus_backend.settings import Settings
    from speechnexus_backend.speech.infrastructure.profile_publication_schema import (
        profile_publication_reader_grants,
    )

    s = sandbox
    s.draft()
    s.port.mutate(s.publish())
    for statement in (
        "UPDATE speech_profile_publications SET authority_digest='changed'",
        "DELETE FROM speech_profile_publications",
        "UPDATE speech_profile_lifecycle_results SET error_code='changed'",
        "DELETE FROM speech_profile_lifecycle_intents",
        "TRUNCATE speech_profile_publications",
        "CREATE TABLE profile_forbidden (id integer)",
        "SELECT * FROM coordination_jobs",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), s.pool.connection() as connection:
            connection.execute(statement)
    # The writer owns only the lifecycle columns retirement mutates; published
    # evidence must stay non-updatable.
    with s.pool.connection() as connection:
        updatable = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='speech_profile_publications' "
            "AND pg_catalog.has_column_privilege("
            "CURRENT_USER,%s||'.'||table_name,column_name,'UPDATE') ORDER BY column_name",
            (s.schema, s.schema),
        ).fetchall()
    assert updatable == [
        {"column_name": "disabled_reason"},
        {"column_name": "retired_at_us"},
        {"column_name": "status"},
    ]
    reader = s.role + "_read"
    s.admin.execute(f"CREATE ROLE \"{reader}\" LOGIN PASSWORD 'synthetic_profile_test'")
    try:
        for grant in profile_publication_reader_grants(s.schema, reader):
            s.admin.execute(grant)
        with psycopg.connect(
            make_conninfo(s.url, user=reader, password="synthetic_profile_test"), autocommit=True
        ) as connection:
            connection.execute(f'SET search_path TO "{s.schema}",pg_catalog')
            assert connection.execute(
                "SELECT count(*) FROM speech_profile_publications"
            ).fetchone() == (1,)
            for statement in (
                "SELECT * FROM speech_profile_lifecycle_intents",
                "SELECT * FROM speech_profile_lifecycle_results",
                "UPDATE speech_profile_families SET current_revision=NULL",
                "UPDATE speech_profile_publications SET status='retired'",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(statement)
    finally:
        s.admin.execute(f'DROP OWNED BY "{reader}"')
        s.admin.execute(f'DROP ROLE "{reader}"')
    settings = Settings(
        **{
            **_settings().model_dump(),
            "database_schema": s.schema,
            "profile_writer_database_url": make_conninfo(
                s.url, user=s.role, password="synthetic_profile_test"
            ),
            "asset_allowed_hosts": ("testserver",),
        }
    )
    s.draft("2")
    before = s.snapshot()
    app = create_app(settings=settings)
    with TestClient(app) as http:
        from speechnexus_backend.speech.application.profile_documents import decode_document

        command = s.publish("2", "1", "unregistered-runtime-key")
        response = http.post(
            f"/v1/nexus/admin/profiles/{command.profile_id}/revisions/2/publications",
            json=decode_document(command.intent)["body"],
            headers={"Idempotency-Key": "unregistered-runtime-key"},
        )
        assert (
            response.status_code == 503
            and response.json()["error"]["code"] == "profile_result_unknown"
        )
        listing = http.get("/v1/nexus/profiles")
        assert listing.status_code == 200
        assert [p["revision"] for p in listing.json()["data"]] == ["1"]
        assert all("current_revision" not in p for p in listing.json()["data"])
        published = http.get("/v2/nexus/profiles")
        assert published.status_code == 200
        assert [(p["revision"], p["current_revision"]) for p in published.json()["data"]] == [
            ("1", "1")
        ]
        s.admin.execute("UPDATE speech_profile_families SET current_revision=NULL")
        unset = http.get("/v2/nexus/profiles")
        assert [(p["revision"], p["current_revision"]) for p in unset.json()["data"]] == [
            ("1", None)
        ]
        s.admin.execute("UPDATE speech_profile_families SET current_revision='1'")
    assert s.snapshot() == before


@pytest.mark.parametrize(
    ("suffix", "statements", "message"),
    [
        (
            "table_update",
            ['GRANT UPDATE ON "{schema}".speech_profile_lifecycle_results TO "{role}"'],
            "table-level UPDATE",
        ),
        (
            "column_reference",
            ['GRANT REFERENCES (profile_id) ON "{schema}".speech_profile_publications TO "{role}"'],
            "column REFERENCES",
        ),
        (
            "evidence_update",
            [
                'GRANT UPDATE (published_at_us) ON "{schema}".speech_profile_publications '
                'TO "{role}"'
            ],
            "cannot be updatable",
        ),
        (
            "publication_table_update",
            ['GRANT UPDATE ON "{schema}".speech_profile_publications TO "{role}"'],
            "table-level UPDATE",
        ),
        (
            "foreign_insert",
            [
                'CREATE TABLE "{schema}".profile_privilege_probe (probe_id integer)',
                'GRANT INSERT (probe_id) ON "{schema}".profile_privilege_probe TO "{role}"',
            ],
            "other speech table privileges",
        ),
    ],
)
def test_startup_rejects_writer_privilege_escalation(
    sandbox: Sandbox, suffix: str, statements: list[str], message: str
) -> None:
    """A wider grant must fail startup; table-level UPDATE is checked explicitly."""
    from tests.unit.backend.test_health import _settings

    from speechnexus_backend.profile_runtime import ProfileRuntime
    from speechnexus_backend.settings import Settings

    s = sandbox
    role = f"{s.role}_{suffix}"
    s.admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_profile_test'")
    try:
        for grant in (
            *profile_draft_grants(s.schema, role),
            *profile_publication_grants(s.schema, role),
        ):
            s.admin.execute(grant)
        for statement in statements:
            s.admin.execute(statement.format(schema=s.schema, role=role))
        runtime = ProfileRuntime(
            Settings(
                **{
                    **_settings().model_dump(),
                    "database_schema": s.schema,
                    "profile_writer_database_url": make_conninfo(
                        s.url, user=role, password="synthetic_profile_test"
                    ),
                }
            )
        )
        with (
            psycopg.connect(
                make_conninfo(s.url, user=role, password="synthetic_profile_test"),
                row_factory=dict_row,
            ) as connection,
            pytest.raises(ValueError, match=message),
        ):
            runtime._verify_role(connection, role)
    finally:
        s.admin.execute(f'DROP OWNED BY "{role}"')
        s.admin.execute(f'DROP ROLE "{role}"')


def test_published_lists_expose_only_the_published_projection(sandbox: Sandbox) -> None:
    """Public projections never carry a draft body, command identity or replay key."""
    from fastapi.testclient import TestClient
    from speechnexus_contracts.profile_public_http import validate_response
    from tests.unit.backend.test_health import _settings

    from speechnexus_backend.app import create_app
    from speechnexus_backend.settings import Settings

    s = sandbox
    s.draft("1")
    s.draft("2")
    assert s.port.mutate(s.publish("1", None, "leak-publish")).outcome == "completed"
    secret_body = {
        **profile_request(),
        "profile_id": "secret-draft-only",
        "display_name": "SECRET DRAFT BODY",
        "expected_latest_revision": None,
    }
    secret = prepare_profile_draft(secret_body, "secret-draft-only-key")
    created = s.port.create(secret)
    assert created.outcome == "created" and created.draft is not None
    settings = Settings(
        **{
            **_settings().model_dump(),
            "database_schema": s.schema,
            "profile_writer_database_url": make_conninfo(
                s.url, user=s.role, password="synthetic_profile_test"
            ),
            "asset_allowed_hosts": ("testserver",),
        }
    )
    v1_keys = {
        "id",
        "revision",
        "display_name",
        "kind",
        "release_tier",
        "execution_classes",
    }
    app = create_app(settings=settings)
    with TestClient(app) as http:
        v1 = http.get("/v1/nexus/profiles")
        v2 = http.get("/v2/nexus/profiles")
        assert v1.status_code == 200 and v2.status_code == 200
        # Contract acceptance and the fixed projection are asserted separately:
        # adding any field is rejected by additionalProperties:false with 503,
        # and the exact key set below is the projection's own guard.
        validate_response("listProfiles", 200, {"body": v1.json()})
        validate_response("listPublishedProfiles", 200, {"body": v2.json()})
        assert [(item["id"], item["revision"]) for item in v1.json()["data"]] == [
            ("caption-draft", "1")
        ]
        assert [
            (item["id"], item["revision"], item["current_revision"]) for item in v2.json()["data"]
        ] == [("caption-draft", "1", "1")]
        assert {frozenset(item) for item in v1.json()["data"]} == {frozenset(v1_keys)}
        assert {frozenset(item) for item in v2.json()["data"]} == {
            frozenset(v1_keys | {"current_revision"})
        }
        for response in (v1, v2):
            for leaked in (
                "definition",
                "validation",
                "authority_digest",
                "key_digest",
                "request_digest",
                "idempotency",
            ):
                assert leaked not in response.text, leaked
        unpublished = http.get("/v1/nexus/profiles/caption-draft/revisions/2")
        assert unpublished.status_code == 409
        assert unpublished.json()["error"]["code"] == "profile_not_published"
        missing = http.get("/v1/nexus/profiles/caption-draft/revisions/99")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "profile_not_found"


def test_actual_committed_reply_loss_then_original_recovery_is_read_only(sandbox: Sandbox) -> None:
    from typing import cast

    from psycopg_pool import ConnectionPool
    from tests.integration.test_production_jobs import FaultConnection

    s = sandbox
    s.draft()
    command = s.publish()

    def configure(connection: FaultConnection) -> None:
        connection.execute(f'SET search_path TO "{s.schema}",pg_catalog')
        connection.commit()

    with ConnectionPool[FaultConnection](
        make_conninfo(s.url, user=s.role, password="synthetic_profile_test"),
        connection_class=FaultConnection,
        kwargs={"row_factory": dict_row},
        configure=configure,
        min_size=1,
        max_size=1,
        timeout=3,
    ) as pool:
        with pool.connection() as connection:
            armed = connection
        armed.fault = "after"
        result = PostgresProfiles(cast(SpeechPool, pool), verifier=s.verifier).mutate(command)
    assert result.outcome == "unknown"
    before = s.snapshot()
    assert len(before["speech_profile_publications"]) == 1
    recovered = s.port.recover_lifecycle(command)
    assert recovered.outcome == "replayed"
    assert s.port.mutate(command).publication == recovered.publication
    assert s.snapshot() == before


def test_repeat_upgrade_and_refused_downgrade_preserve_history(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    s.port.mutate(s.publish())
    before = s.snapshot()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = s.schema
    engine = create_engine(
        s.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
        with (
            pytest.raises(RuntimeError, match="history requires preservation"),
            engine.connect() as connection,
        ):
            config.attributes["connection"] = connection
            migration.downgrade(config, "0031_tts_settlement")
        assert s.snapshot() == before
        assert s.admin.execute("SELECT version_num FROM alembic_version").fetchone() == {
            "version_num": migration_head(config)
        }
    finally:
        engine.dispose()


def test_retirement_and_disable_are_distinct_durable_terminal_states(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    retired = s.port.mutate(s.retirement({"mode": "retire"}, "retire-pg-key"))
    assert retired.outcome == "completed" and retired.publication is not None
    publication = retired.publication
    assert publication.status == "retired"
    assert publication.retired_at_us is not None
    assert publication.retired_at_us >= publication.published_at_us
    assert publication.disabled_reason is None
    row = s.admin.execute(
        "SELECT status,retired_at_us,disabled_reason FROM speech_profile_publications"
    ).fetchone()
    assert row == {
        "status": "retired",
        "retired_at_us": publication.retired_at_us,
        "disabled_reason": None,
    }
    assert s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchone() == {
        "current_revision": None
    }
    assert s.port.list_publications(limit=5) == ([], False)
    fixed = s.port.get_publication(publication.draft.profile_id, "1")
    assert fixed is not None and fixed.status == "retired"
    after = s.snapshot()
    replay = s.port.mutate(s.retirement({"mode": "retire"}, "retire-pg-key"))
    assert replay.outcome == "replayed" and replay.publication == publication
    assert s.port.recover_lifecycle(
        s.retirement({"mode": "retire"}, "retire-pg-key")
    ).publication == (publication)
    assert s.snapshot() == after
    conflict = s.port.mutate(
        s.retirement({"mode": "disable", "reason": "changed intent"}, "retire-pg-key")
    )
    assert conflict.outcome == "conflict" and conflict.error_code == "idempotency_conflict"
    assert s.snapshot() == after
    assert (
        s.port.mutate(s.retirement({"mode": "retire"}, "retire-terminal-key")).error_code
        == "profile_not_published"
    )
    # The durable rejection is stable for a repeated key too.
    rejected = s.snapshot()
    assert (
        s.port.mutate(s.retirement({"mode": "retire"}, "retire-terminal-key")).error_code
        == "profile_not_published"
    )
    assert s.snapshot() == rejected


def test_disable_records_its_reason_and_retiring_the_current_revision_clears_the_pointer(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    s.draft("2")
    assert s.port.mutate(s.publish("2", "1", "publish-second")).outcome == "completed"
    assert s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchone() == {
        "current_revision": "2"
    }
    disabled = s.port.mutate(
        s.retirement({"mode": "disable", "reason": "synthetic operator disable"}, "disable-2", "2")
    )
    assert disabled.outcome == "completed" and disabled.publication is not None
    assert disabled.publication.status == "disabled"
    assert disabled.publication.disabled_reason == "synthetic operator disable"
    assert disabled.publication.retired_at_us is None
    row = s.admin.execute(
        "SELECT status,retired_at_us,disabled_reason FROM speech_profile_publications "
        "WHERE revision='2'"
    ).fetchone()
    assert row == {
        "status": "disabled",
        "retired_at_us": None,
        "disabled_reason": "synthetic operator disable",
    }
    assert s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchone() == {
        "current_revision": None
    }
    # A non-current revision can also be disabled; the already-cleared pointer is
    # left untouched rather than being re-derived from another revision.
    assert s.port.mutate(
        s.retirement({"mode": "disable", "reason": "historical"}, "disable-1", "1")
    ).outcome == ("completed")
    assert s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchone() == {
        "current_revision": None
    }
    assert s.port.list_publications(limit=5) == ([], False)
    assert s.port.mutate(s.activate("1", None, "activate-disabled")).error_code == (
        "profile_not_published"
    )


def test_concurrent_same_key_retirement_serializes_on_the_original_intent(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    command = s.retirement({"mode": "retire"}, "same-retirement-key")
    blocker = psycopg.connect(s.url)
    try:
        blocker.execute(f'SET search_path TO "{s.schema}",pg_catalog')
        blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(s.port.mutate, command) for _ in range(2)]
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    n = s.admin.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                        "AND cardinality(pg_blocking_pids(pid))>0",
                        (s.role,),
                    ).fetchone()["n"]
                    if n == 2:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("Two actual independent PG lock waits were not observed")
            finally:
                blocker.rollback()
            results = [f.result(timeout=5) for f in futures]
        assert sorted(result.outcome for result in results) == ["completed", "replayed"]
        assert all(result.publication is not None for result in results)
        first = next(result.publication for result in results if result.outcome == "completed")
        assert all(result.publication == first for result in results)
        assert s.admin.execute("SELECT status FROM speech_profile_publications").fetchone() == {
            "status": "retired"
        }
        assert s.admin.execute(
            "SELECT current_revision FROM speech_profile_families"
        ).fetchone() == {"current_revision": None}
        assert s.admin.execute(
            "SELECT count(*) AS n FROM speech_profile_lifecycle_results"
        ).fetchone() == {"n": 2}
    finally:
        blocker.close()


def test_concurrent_distinct_key_retirements_only_one_changes_the_facts(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    commands = [
        s.retirement({"mode": "retire"}, "distinct-retire-" + suffix) for suffix in ("a", "b")
    ]
    blocker = psycopg.connect(s.url)
    try:
        blocker.execute(f'SET search_path TO "{s.schema}",pg_catalog')
        blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(s.port.mutate, command) for command in commands]
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    n = s.admin.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                        "AND cardinality(pg_blocking_pids(pid))>0",
                        (s.role,),
                    ).fetchone()["n"]
                    if n == 2:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("Two actual independent PG lock waits were not observed")
            finally:
                blocker.rollback()
            results = [f.result(timeout=5) for f in futures]
        assert sorted(result.outcome for result in results) == ["completed", "rejected"]
        rejected = next(result for result in results if result.outcome == "rejected")
        assert rejected.error_code == "profile_not_published"
        assert s.admin.execute("SELECT status FROM speech_profile_publications").fetchone() == {
            "status": "retired"
        }
    finally:
        blocker.close()


def test_real_runtime_serves_retirement_with_minimum_writer_grants(sandbox: Sandbox) -> None:
    """The composed API answers the contract path with its real writer role."""
    from fastapi.testclient import TestClient
    from tests.unit.backend.test_health import _settings

    from speechnexus_backend.app import create_app
    from speechnexus_backend.settings import Settings

    s = sandbox
    identity = profile_request()["profile_id"]
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    settings = Settings(
        **{
            **_settings().model_dump(),
            "database_schema": s.schema,
            "profile_writer_database_url": make_conninfo(
                s.url, user=s.role, password="synthetic_profile_test"
            ),
            "asset_allowed_hosts": ("testserver",),
        }
    )
    path = f"/v1/nexus/admin/profiles/{identity}/revisions/1/retirements"
    with TestClient(create_app(settings=settings)) as http:
        response = http.post(
            path, json={"mode": "retire"}, headers={"Idempotency-Key": "runtime-retire-key"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["publication"] == {
            "status": "retired",
            "published_at": body["publication"]["published_at"],
            "retired_at": body["publication"]["retired_at"],
            "disabled_reason": None,
        }
        replay = http.post(
            path, json={"mode": "retire"}, headers={"Idempotency-Key": "runtime-retire-key"}
        )
        assert replay.status_code == 200 and replay.json() == body
        conflict = http.post(
            path,
            json={"mode": "disable", "reason": "changed intent"},
            headers={"Idempotency-Key": "runtime-retire-key"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        missing_reason = http.post(
            path, json={"mode": "disable"}, headers={"Idempotency-Key": "runtime-disable-key"}
        )
        assert missing_reason.status_code == 400
        assert missing_reason.json()["error"]["code"] == "invalid_profile_request"
        missing = http.post(
            f"/v1/nexus/admin/profiles/{identity}/revisions/99/retirements",
            json={"mode": "retire"},
            headers={"Idempotency-Key": "runtime-missing-key"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "profile_not_found"
        published = http.get("/v2/nexus/profiles")
        assert published.status_code == 200 and published.json()["data"] == []
        fixed = http.get(f"/v1/nexus/profiles/{identity}/revisions/1")
        assert fixed.status_code == 200 and fixed.json()["publication"]["status"] == "retired"
    assert s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchone() == {
        "current_revision": None
    }


def _publication_state_check(query: Any) -> str:
    row = query.execute(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conname='speech_profile_publications_check' "
        "AND conrelid='speech_profile_publications'::regclass"
    ).fetchone()
    assert row is not None
    return str(row["definition"])


def test_profile_retirement_migration_widens_both_stored_checks() -> None:
    """0050 admits ``retire`` and a bounded disable reason without rewriting rows."""
    from alembic import command
    from tests.integration.test_public_job_progress_migration_pg import _isolated_schema, _url

    insert = (
        "INSERT INTO speech_profile_lifecycle_intents "
        "(key_digest,operation,profile_id,revision,intent_json,request_digest,recorded_at_us) "
        "VALUES (%s,%s,'caption-draft','1','{}',%s,1)"
    )
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0050_profile_retirements")
        assert "length(" in _publication_state_check(query)
        query.execute(insert, ("sha256:" + "a" * 64, "retire", "sha256:" + "b" * 64))
        assert query.execute(
            "SELECT operation FROM speech_profile_lifecycle_intents"
        ).fetchall() == [{"operation": "retire"}]
        with pytest.raises(RuntimeError, match="require preservation"):
            command.downgrade(config, "0049_workspace_commands")
        assert query.execute("SELECT version_num FROM alembic_version").fetchone() == {
            "version_num": "0050_profile_retirements"
        }
        query.execute("DELETE FROM speech_profile_lifecycle_intents")
        command.downgrade(config, "0049_workspace_commands")
        assert query.execute("SELECT version_num FROM alembic_version").fetchone() == {
            "version_num": "0049_workspace_commands"
        }
        # The previous revision keeps the laxer state CHECK and the narrower
        # operation CHECK, so a downgrade is a real reversal of both.
        assert "length(" not in _publication_state_check(query)
        with pytest.raises(psycopg.errors.CheckViolation):
            query.execute(insert, ("sha256:" + "c" * 64, "retire", "sha256:" + "d" * 64))
        command.upgrade(config, "head")
        assert query.execute("SELECT version_num FROM alembic_version").fetchone() == {
            "version_num": "0050_profile_retirements"
        }
        assert "length(" in _publication_state_check(query)
        query.execute(insert, ("sha256:" + "c" * 64, "retire", "sha256:" + "d" * 64))
