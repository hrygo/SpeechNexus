"""Exercise the supported revocation command with a limited trust writer."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from speechnexus_backend.builtin_voice_revocation import (
    BuiltinVoiceRevocationRejectedError,
    revoke_builtin_voice,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    qualification_writer_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["source", "qualification"])
def test_supported_revocation_recovers_committed_lost_reply(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("An isolated PostgreSQL DSN is required")
    suffix = uuid.uuid4().hex[:10]
    schema, role = "sn_bvr_" + suffix, "sn_bvr_w_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = schema
    pool = speech_pool(url, schema=schema, max_size=1, name="synthetic-revocation")
    try:
        with engine.connect() as migration_connection:
            config.attributes["connection"] = migration_connection
            command.upgrade(config, "head")
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'CREATE ROLE "{role}"')
            for statement in qualification_writer_grants(schema, role):
                admin.execute(statement)
            admin.execute(f'SET search_path TO "{schema}", pg_catalog')
            installation = admin.execute(
                "SELECT installation_id::text FROM speech_publication_installation"
            ).fetchone()
            assert installation is not None
            database = admin.execute("SELECT current_database()").fetchone()
            assert database is not None
            admin.execute(
                "INSERT INTO speech_builtin_voice_source_approvals "
                "(source_approval_id,voice_id,source_revision,source_digest,"
                "native_speaker_id,expires_at) VALUES "
                "('source-vivian','voice-vivian','source-1',%s,'Vivian',"
                "clock_timestamp()+interval '1 hour')",
                ("sha256:" + "1" * 64,),
            )
            if kind == "qualification":
                admin.execute(
                    "INSERT INTO speech_builtin_voice_approvals "
                    "(approval_id,voice_id,revision,revision_id,binding_revision_id,"
                    "source_approval_id,facts,expires_at) VALUES "
                    "('qualification-vivian','voice-vivian','1','voice-rev-1','binding-rev-1',"
                    "'source-vivian','{}'::jsonb,clock_timestamp()+interval '1 hour')"
                )
        pool.open()
        # Reuse one pooled connection after SET ROLE to exercise the exact grants.
        with pool.connection() as connection:
            connection.execute(f'SET ROLE "{role}"')
        store = PostgresPublications(pool)
        options = {
            "kind": kind,
            "approval_id": "source-vivian" if kind == "source" else "qualification-vivian",
            "reason": "synthetic withdrawal",
            "database": database[0],
            "schema": schema,
            "installation_id": installation[0],
        }
        original_run = store._run
        with monkeypatch.context() as patch:

            def drop_committed_reply(*args: object, **kwargs: object) -> object:
                original_run(*args, **kwargs)  # type: ignore[arg-type]
                raise PublicationUnavailableError

            patch.setattr(store, "_run", drop_committed_reply)
            with pytest.raises(PublicationUnavailableError):
                revoke_builtin_voice(store, **options)
        assert revoke_builtin_voice(store, **options)["outcome"] == "replayed"
        with pytest.raises(BuiltinVoiceRevocationRejectedError, match="intent_conflict"):
            revoke_builtin_voice(store, **{**options, "reason": "changed reason"})
        with pytest.raises(BuiltinVoiceRevocationRejectedError, match="target_changed"):
            revoke_builtin_voice(store, **{**options, "installation_id": str(uuid.uuid4())})
        with pool.connection() as connection:
            table = (
                "speech_builtin_voice_source_revocations"
                if kind == "source"
                else "speech_builtin_voice_revocations"
            )
            count = connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            assert count is not None and count["n"] == 1
    finally:
        pool.close()
        with engine.begin() as cleanup_connection:
            cleanup_connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            cleanup_connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        engine.dispose()
