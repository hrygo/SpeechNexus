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
)

pytestmark = pytest.mark.integration


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
        pytest.skip("Requires explicitly isolated PG; skip is not evidence")
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
            "version_num": "0033_profile_publications"
        }
    finally:
        engine.dispose()
