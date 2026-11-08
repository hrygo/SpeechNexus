"""Isolated PostgreSQL source decision: issuer, target and original receipt."""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from speechnexus_backend.builtin_voice_source_approval import (
    BuiltinSourceApprovalError,
    approve_builtin_voice_source,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    builtin_source_approval_writer_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

pytestmark = pytest.mark.integration


def test_source_writer_authority_replay_and_narrow_grants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not evidence"
        )
    suffix = uuid.uuid4().hex[:10]
    schema, writer, business = "sn_src_" + suffix, "sn_src_w_" + suffix, "sn_src_b_" + suffix
    qualification_role = "sn_src_q_" + suffix
    password = "synthetic_source_password"
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = schema
    writer_pool = speech_pool(
        make_conninfo(url, user=writer, password=password),
        schema=schema,
        max_size=1,
        name="test-source-writer",
    )
    owner_pool = speech_pool(url, schema=schema, max_size=1, name="test-source-owner")
    try:
        with engine.connect() as migration_connection:
            config.attributes["connection"] = migration_connection
            command.upgrade(config, "head")
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE \"{writer}\" LOGIN PASSWORD 'synthetic_source_password'")
            admin.execute(f"CREATE ROLE \"{business}\" LOGIN PASSWORD 'synthetic_source_password'")
            admin.execute(f'CREATE ROLE "{qualification_role}" NOLOGIN')
            for statement in builtin_source_approval_writer_grants(schema, writer):
                admin.execute(statement)
            admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{business}"')
            admin.execute(f'SET search_path TO "{schema}",pg_catalog')
            installation = admin.execute(
                "SELECT installation_id::text FROM speech_publication_installation"
            ).fetchone()
            database = admin.execute("SELECT current_database()").fetchone()
            assert installation is not None and database is not None
        writer_pool.open()
        owner_pool.open()
        store = PostgresPublications(writer_pool, timeout_seconds=10)
        expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=4)
        args: dict[str, Any] = {
            "database": database[0],
            "schema": schema,
            "installation_id": installation[0],
            "source_approval_id": "source-test",
            "voice_id": "voice-vivian",
            "source_revision": "synthetic-revision",
            "source_digest": "sha256:" + "a" * 64,
            "native_speaker_id": "Vivian",
            "expires_at": expiry,
        }
        with pytest.raises(BuiltinSourceApprovalError, match="target_changed"):
            approve_builtin_voice_source(store, **(args | {"installation_id": str(uuid.uuid4())}))
        for wrong in ({"database": "other_database"}, {"schema": "other_schema"}):
            with pytest.raises(BuiltinSourceApprovalError, match="target_changed"):
                approve_builtin_voice_source(store, **(args | wrong))
        with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
            approve_builtin_voice_source(PostgresPublications(owner_pool), **args)
        original_run = store._run
        with monkeypatch.context() as patch:

            def lost_reply(*a: Any, **kw: Any) -> Any:
                original_run(*a, **kw)
                raise PublicationUnavailableError

            patch.setattr(store, "_run", lost_reply)
            with pytest.raises(PublicationUnavailableError):
                approve_builtin_voice_source(store, **args)
        assert approve_builtin_voice_source(store, **args)["outcome"] == "replayed"
        for changed in (
            {"voice_id": "voice-other"},
            {"source_revision": "different"},
            {"source_digest": "sha256:" + "b" * 64},
        ):
            with pytest.raises(BuiltinSourceApprovalError, match="intent_conflict"):
                approve_builtin_voice_source(store, **(args | changed))
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'SET search_path TO "{schema}",pg_catalog')
            row = admin.execute(
                "SELECT approved_by,approved_at FROM speech_builtin_voice_source_approvals"
            ).fetchone()
            assert row is not None and row[0] == writer and row[1] is not None
            admin.execute(
                "INSERT INTO speech_builtin_voice_source_revocations (source_approval_id,reason) "
                "VALUES ('source-test','synthetic revocation')"
            )
            deadline = time.monotonic() + 8
            while True:
                expired = admin.execute(
                    "SELECT expires_at<=clock_timestamp() "
                    "FROM speech_builtin_voice_source_approvals"
                ).fetchone()
                assert expired is not None
                if expired[0]:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("source did not expire")
                time.sleep(0.02)
        assert approve_builtin_voice_source(store, **args)["outcome"] == "replayed"
        with pytest.raises(BuiltinSourceApprovalError, match="approval_expired"):
            approve_builtin_voice_source(store, **(args | {"source_approval_id": "source-other"}))
        waiting = args | {
            "source_approval_id": "source-waiting",
            "expires_at": datetime.now(UTC) + timedelta(seconds=3),
        }
        with psycopg.connect(url) as blocker:
            blocker.execute(f'SET search_path TO "{schema}",pg_catalog')
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'speechnexus:catalog:'||current_schema()||':voice:'||%s,0))",
                (waiting["voice_id"],),
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(approve_builtin_voice_source, store, **waiting)
                deadline = time.monotonic() + 8
                with psycopg.connect(url, autocommit=True) as observer:
                    while not observer.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                        (blocker.info.backend_pid,),
                    ).fetchall():
                        if pending.done() or time.monotonic() >= deadline:
                            pytest.fail("source approval did not wait on the voice lock")
                        time.sleep(0.01)
                    while True:
                        expired = observer.execute(
                            "SELECT %s::timestamptz<=clock_timestamp()", (waiting["expires_at"],)
                        ).fetchone()
                        assert expired is not None
                        if expired[0]:
                            break
                        if time.monotonic() >= deadline:
                            pytest.fail("source approval did not expire during lock wait")
                        time.sleep(0.02)
                blocker.commit()
                with pytest.raises(BuiltinSourceApprovalError, match="approval_expired"):
                    pending.result(timeout=10)
        relation_wait = args | {
            "source_approval_id": "source-relation-wait",
            "expires_at": datetime.now(UTC) + timedelta(seconds=3),
        }
        with psycopg.connect(url) as blocker:
            blocker.execute(
                f'LOCK TABLE "{schema}".speech_builtin_voice_source_approvals IN SHARE MODE'
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(approve_builtin_voice_source, store, **relation_wait)
                deadline = time.monotonic() + 8
                with psycopg.connect(url, autocommit=True) as observer:
                    while not observer.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                        (blocker.info.backend_pid,),
                    ).fetchall():
                        if pending.done() or time.monotonic() >= deadline:
                            pytest.fail("source insert did not wait on the relation lock")
                        time.sleep(0.01)
                    while True:
                        expired = observer.execute(
                            "SELECT %s::timestamptz<=clock_timestamp()",
                            (relation_wait["expires_at"],),
                        ).fetchone()
                        assert expired is not None
                        if expired[0]:
                            break
                        if time.monotonic() >= deadline:
                            pytest.fail("source approval did not expire during relation wait")
                        time.sleep(0.02)
                blocker.commit()
                with pytest.raises(BuiltinSourceApprovalError, match="approval_expired"):
                    pending.result(timeout=10)
        with psycopg.connect(url, autocommit=True) as admin:
            row = admin.execute(
                f'SELECT 1 FROM "{schema}".speech_builtin_voice_source_approvals '
                "WHERE source_approval_id='source-relation-wait'"
            ).fetchone()
            assert row is None
        from speechnexus_backend.approve_builtin_voice_source import main

        monkeypatch.setenv(
            "SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL",
            make_conninfo(url, user=writer, password=password),
        )
        cli_values = {
            "database": database[0],
            "schema": schema,
            "installation-id": installation[0],
            "source-approval-id": "source-cli",
            "voice-id": "voice-vivian",
            "source-revision": "synthetic-revision",
            "source-digest": "sha256:" + "a" * 64,
            "native-speaker-id": "Vivian",
            "expires-at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        }
        main([item for key, value in cli_values.items() for item in ("--" + key, value)])
        output = capsys.readouterr()
        assert '"outcome": "recorded"' in output.out and password not in output.out + output.err
        for role, table in (
            (writer, "speech_builtin_voice_approvals"),
            (writer, "speech_publications"),
            (business, "speech_builtin_voice_source_approvals"),
        ):
            with (
                psycopg.connect(make_conninfo(url, user=role, password=password)) as restricted,
                pytest.raises(psycopg.errors.InsufficientPrivilege),
            ):
                restricted.execute(f'INSERT INTO "{schema}"."{table}" DEFAULT VALUES')
        for privilege in ("UPDATE (facts)", "INSERT (facts)"):
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(
                    f'GRANT {privilege} ON "{schema}".speech_builtin_voice_approvals TO "{writer}"'
                )
            with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
                approve_builtin_voice_source(store, **args)
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(
                    f'REVOKE {privilege} ON "{schema}".speech_builtin_voice_approvals '
                    f'FROM "{writer}"'
                )
        for privilege, table in (
            ("INSERT (approved_by)", "speech_builtin_voice_source_approvals"),
            ("UPDATE (source_digest)", "speech_builtin_voice_source_approvals"),
            ("INSERT", "speech_builtin_voice_source_revocations"),
            ("UPDATE (intent)", "speech_builtin_voice_probe_authorizations"),
            ("DELETE", "speech_publication_heads"),
            ("TRIGGER", "speech_builtin_voice_qualifications"),
            ("REFERENCES (source_approval_id)", "speech_builtin_voice_source_approvals"),
        ):
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(f'GRANT {privilege} ON "{schema}"."{table}" TO "{writer}"')
            with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
                approve_builtin_voice_source(store, **args)
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(f'REVOKE {privilege} ON "{schema}"."{table}" FROM "{writer}"')
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(
                f'GRANT UPDATE (facts) ON "{schema}".speech_builtin_voice_approvals '
                f'TO "{qualification_role}"'
            )
            admin.execute(
                f'GRANT "{qualification_role}" TO "{writer}" WITH INHERIT FALSE, SET TRUE'
            )
        with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
            approve_builtin_voice_source(store, **args)
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'REVOKE "{qualification_role}" FROM "{writer}"')
            admin.execute(
                f'REVOKE UPDATE (facts) ON "{schema}".speech_builtin_voice_approvals '
                f'FROM "{qualification_role}"'
            )
            admin.execute(f'GRANT CREATE ON SCHEMA "{schema}" TO "{qualification_role}"')
            admin.execute(
                f'GRANT "{qualification_role}" TO "{writer}" WITH INHERIT FALSE, SET TRUE'
            )
        with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
            approve_builtin_voice_source(store, **args)
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'REVOKE "{qualification_role}" FROM "{writer}"')
            admin.execute(f'REVOKE CREATE ON SCHEMA "{schema}" FROM "{qualification_role}"')
        for predefined in ("pg_write_server_files", "pg_execute_server_program"):
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(f'GRANT "{predefined}" TO "{writer}" WITH INHERIT FALSE, SET TRUE')
            with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
                approve_builtin_voice_source(store, **args)
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(f'REVOKE "{predefined}" FROM "{writer}"')
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'GRANT pg_read_all_data TO "{writer}" WITH INHERIT TRUE, SET FALSE')
        with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
            approve_builtin_voice_source(store, **args)
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'REVOKE pg_read_all_data FROM "{writer}"')
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(
                f'GRANT INSERT ON "{schema}".speech_builtin_voice_approvals TO "{writer}"'
            )
        with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
            approve_builtin_voice_source(store, **args)
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(
                f'REVOKE INSERT ON "{schema}".speech_builtin_voice_approvals FROM "{writer}"'
            )
            owner = admin.execute("SELECT current_user").fetchone()
            assert owner is not None
            admin.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(owner[0]), sql.Identifier(writer))
            )
        with pytest.raises(BuiltinSourceApprovalError, match="writer_identity_forbidden"):
            approve_builtin_voice_source(store, **args)
    finally:
        writer_pool.close()
        owner_pool.close()
        with engine.begin() as cleanup:
            cleanup.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            cleanup.execute(text(f'DROP ROLE IF EXISTS "{writer}"'))
            cleanup.execute(text(f'DROP ROLE IF EXISTS "{business}"'))
            cleanup.execute(text(f'DROP ROLE IF EXISTS "{qualification_role}"'))
        engine.dispose()
