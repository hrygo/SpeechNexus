"""PostgreSQL qualification lookup and minimum writer permissions, synthetic facts only."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    qualification_reader_grants,
    qualification_writer_grants,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
    require_builtin_voice,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def pg() -> Iterator[tuple[psycopg.Connection[dict[str, Any]], str, str]]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_bv_" + suffix, "sn_bv_r_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = schema
    with engine.connect() as migration_connection:
        config.attributes["connection"] = migration_connection
        command.upgrade(config, "head")
    engine.dispose()
    connection = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    connection.execute(f'SET search_path TO "{schema}", pg_catalog')
    connection.execute(f'CREATE ROLE "{role}"')
    try:
        yield connection, schema, role
    finally:
        connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        connection.execute(f'DROP ROLE "{role}"')
        connection.close()


def _require(cursor: psycopg.Cursor[dict[str, Any]], generation: int = 1) -> None:
    require_builtin_voice(
        cursor,
        voice_id="voice-vivian",
        revision=1,
        revision_id="voice-rev-1",
        binding_revision_id="binding-rev-1",
        instance_id="instance-1",
        instance_generation=generation,
        host_node_id="host-1",
        gateway_id="gateway-1",
        policy_revision_id="policy-1",
    )


def test_migration_admission_revocation_and_reader_permissions(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, role = pg
    with db.cursor() as cursor, pytest.raises(AdmissionRejectedError):
        _require(cursor)
    expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    facts = {
        "instance_id": "instance-1",
        "generation": "1",
        "host_node_id": "host-1",
        "gateway_id": "gateway-1",
        "gateway_policy_revision_id": "policy-1",
        "source_revision": "source-rev",
        "source_digest": "sha256:" + "1" * 64,
        "native_speaker_id": "Vivian",
        "source_approval_id": "source-1",
        "qualification_report_id": "report-1",
        "qualification_report_digest": "sha256:" + "1" * 64,
        "capacity_manifest_id": "manifest-1",
        "capacity_manifest_digest": "sha256:" + "1" * 64,
        "probe_evidence_digest": "sha256:" + "1" * 64,
        "verifier_id": "builtin-customvoice-v1",
        "verifier_version": "1",
        "required_checks": [
            "source_approved",
            "artifact_integrity",
            "engine_identity",
            "voice_speaker_binding",
            "wav_complete",
            "output_bounds",
            "native_terminal",
            "stop_release",
        ],
        "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "model_revision": "rev-1",
        "model_artifact_digest": "sha256:" + "1" * 64,
        "image_digest": "sha256:" + "1" * 64,
        "gateway_policy_digest": "sha256:" + "1" * 64,
        "binding_id": "binding-tts",
        "binding_revision": "1",
        "binding_revision_id": "binding-rev-1",
        "checked_at_us": 1_767_225_600_000_000,
        "expires_at_us": int(expiry.timestamp()) * 1_000_000,
        "qualified_capabilities": {},
        "public_document": {
            "resource_type": "voice_revision",
            "voice_id": "voice-vivian",
            "revision": "1",
            "object": "nexus.voice_revision",
            "display_name": "Vivian",
            "capabilities": {
                "languages": ["en"],
                "sample_rates_hz": [24000],
                "audio_formats": ["wav"],
                "modes": ["complete"],
            },
            "validation": {
                "validation_id": "report-1",
                "status": "passed",
                "checked_at": "2026-01-01T00:00:00Z",
            },
            "published_at": "2026-01-01T00:00:00Z",
            "retired_at": None,
            "disabled_reason": None,
            "source": {
                "type": "builtin",
                "draft_id": None,
                "draft_revision": None,
                "source_asset_digest": None,
            },
            "status": "published",
            "model_binding": {"id": "binding-tts", "revision": "1"},
        },
    }
    db.execute(
        "INSERT INTO speech_builtin_voice_source_approvals VALUES (%s,%s,%s,%s,%s,%s)",
        ("source-1", "voice-vivian", "source-rev", facts["source_digest"], "Vivian", expiry),
    )
    db.execute(
        "INSERT INTO speech_builtin_voice_approvals "
        "(approval_id,voice_id,revision,revision_id,binding_revision_id,source_approval_id,"
        "facts,expires_at) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
        (
            "approval-1",
            "voice-vivian",
            "1",
            "voice-rev-1",
            "binding-rev-1",
            "source-1",
            json.dumps(facts),
            expiry,
        ),
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO speech_builtin_voice_approvals "
            "(approval_id,voice_id,revision,revision_id,binding_revision_id,source_approval_id,"
            "facts,expires_at) VALUES "
            "('approval-other','voice-other','1','rev-other','binding-rev-1',"
            "'source-1','{}'::jsonb,%s)",
            (expiry,),
        )
    db.execute(
        "INSERT INTO speech_publications VALUES ('voice','voice-vivian','1','voice-rev-1',1)"
    )
    db.execute(
        "INSERT INTO speech_publication_heads VALUES ('voice','voice-vivian','1','voice-rev-1',1)"
    )
    with db.cursor() as cursor, pytest.raises(AdmissionRejectedError):
        _require(cursor)
    db.execute(
        "INSERT INTO speech_builtin_voice_qualifications VALUES "
        "('voice-vivian','1','voice-rev-1','binding-rev-1','approval-1',%s::jsonb,'Vivian')",
        (json.dumps(facts["public_document"]),),
    )
    with db.cursor() as cursor:
        _require(cursor)
    db.execute(
        "INSERT INTO speech_publications VALUES ('voice','voice-vivian','2','voice-rev-2',2)"
    )
    db.execute(
        "UPDATE speech_publication_heads SET revision='2',revision_id='voice-rev-2',"
        "updated_at_us=2 WHERE kind='voice' AND object_id='voice-vivian'"
    )
    with db.cursor() as cursor:
        _require(cursor)
    with db.cursor() as cursor, pytest.raises(AdmissionRejectedError):
        _require(cursor, 2)
    # Revocation and first admission serialize on the voice advisory lock.
    second = psycopg.connect(os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], row_factory=dict_row)
    second.execute(f'SET search_path TO "{schema}", pg_catalog')
    pid = second.info.backend_pid
    lock_sql = (
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:catalog:' || current_schema() || ':voice:' || %s, 0))"
    )

    def blocked_admission() -> None:
        with second.transaction():
            second.execute(lock_sql, ("voice-vivian",))
            with second.cursor() as cursor, pytest.raises(AdmissionRejectedError):
                _require(cursor)

    with ThreadPoolExecutor(max_workers=1) as executor, db.transaction():
        db.execute(lock_sql, ("voice-vivian",))
        future = executor.submit(blocked_admission)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = db.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone()
            if state and state["wait_event_type"] == "Lock":
                break
        else:
            raise AssertionError("admission did not wait on the voice lock")
        db.execute(
            "INSERT INTO speech_builtin_voice_revocations (approval_id,reason) "
            "VALUES ('approval-1','synthetic revocation')"
        )
    future.result(timeout=3)
    second.close()
    with db.cursor() as cursor, pytest.raises(AdmissionRejectedError):
        _require(cursor)
    for grant in qualification_reader_grants(schema, role):
        db.execute(grant)
    with db.transaction():
        db.execute(f'SET LOCAL ROLE "{role}"')
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(
                "INSERT INTO speech_builtin_voice_source_approvals VALUES "
                "('bad','voice-vivian','x','x','Vivian',clock_timestamp())"
            )
    for grant in qualification_writer_grants(schema, role):
        db.execute(grant)
    with db.transaction():
        db.execute(f'SET LOCAL ROLE "{role}"')
        db.execute(
            "INSERT INTO speech_builtin_voice_source_approvals "
            "(source_approval_id,voice_id,source_revision,source_digest,"
            "native_speaker_id,expires_at) "
            "VALUES ('source-two','voice-vivian','source-rev',%s,'Vivian',%s)",
            (facts["source_digest"], expiry),
        )
        row = db.execute(
            "SELECT approved_by, approved_at FROM speech_builtin_voice_source_approvals "
            "WHERE source_approval_id='source-two'"
        ).fetchone()
        assert row is not None and row["approved_by"] == role and row["approved_at"] is not None
        db.execute(
            "INSERT INTO speech_builtin_voice_approvals "
            "(approval_id,voice_id,revision,revision_id,binding_revision_id,"
            "source_approval_id,facts,expires_at) VALUES "
            "('approval-two','voice-vivian','2','voice-rev-2','binding-rev-1',"
            "'source-two',%s::jsonb,%s)",
            (json.dumps(facts), expiry),
        )
        row = db.execute(
            "SELECT approved_by, approved_at FROM speech_builtin_voice_approvals "
            "WHERE approval_id='approval-two'"
        ).fetchone()
        assert row is not None and row["approved_by"] == role and row["approved_at"] is not None
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(
                "INSERT INTO speech_builtin_voice_source_approvals "
                "(source_approval_id,voice_id,source_revision,source_digest,native_speaker_id,"
                "expires_at,approved_by) VALUES ('source-three','voice-vivian','source-rev',"
                "%s,'Vivian',%s,'forged')",
                (facts["source_digest"], expiry),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(
                "INSERT INTO speech_builtin_voice_approvals "
                "(approval_id,voice_id,revision,revision_id,binding_revision_id,"
                "source_approval_id,facts,expires_at,approved_by) VALUES "
                "('approval-three','voice-vivian','3','voice-rev-3','binding-rev-1',"
                "'source-two',%s::jsonb,%s,'forged')",
                (json.dumps(facts), expiry),
            )


def test_publication_requires_approval_and_replays_original(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    import hashlib

    import rfc8785
    from psycopg.conninfo import make_conninfo
    from tests.unit.backend.test_binding_release_evidence import packet

    from speechnexus_backend.builtin_voice_publication import (
        BuiltinVoicePublicationRejectedError,
        publish_builtin_voice,
    )
    from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
    from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
        builtin_publisher_grants,
    )
    from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
    from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
    from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

    db, schema, role = pg
    db.execute(f'ALTER ROLE "{role}" LOGIN')
    for grant in (*publication_grants(schema, role), *builtin_publisher_grants(schema, role)):
        db.execute(grant)
    pool = speech_pool(
        make_conninfo(os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], user=role),
        schema=schema,
        max_size=1,
    )
    pool.open()
    database_row = db.execute("SELECT current_database() AS name").fetchone()
    installation_row = db.execute(
        "SELECT installation_id FROM speech_publication_installation"
    ).fetchone()
    assert database_row is not None and installation_row is not None
    dbname = database_row["name"]
    installation = str(installation_row["installation_id"])
    bundle = packet()
    manifest, report, definition = bundle["manifest"], bundle["report"], bundle["definition"]
    tts_pin = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "deploy/engines/file-tts-customvoice/artifact-pin.json"
        ).read_text()
    )
    bundle["pin"] = tts_pin
    manifest["binding_revision_id"] = report["binding_revision_id"] = "binding-rev-1"
    manifest["manifest_id"] = report["capacity_manifest_id"] = "manifest-1"
    report["report_id"] = manifest["qualification"]["qualification_report_id"] = "report-1"
    report["binding"] = {"id": "binding-tts", "revision": "1"}
    manifest["supported_tasks"] = definition["tasks"] = ["tts_file"]
    definition["execution_recipe"]["capacity_manifest_id"] = "manifest-1"
    definition["execution_recipe"]["stage_roles"] = ["tts_connector"]
    stage = manifest["stage_graph"][0]
    stage["stage_role"] = "tts_connector"
    stage["model_revision_digest"] = (
        "sha256:" + hashlib.sha256(rfc8785.dumps(tts_pin["model"])).hexdigest()
    )
    expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    report["expires_at"] = manifest["qualification"]["expires_at"] = (
        (expiry + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    caps = {
        "languages": ["en"],
        "audio_formats": ["wav"],
        "sample_rates_hz": [24000],
        "modes": ["complete"],
        "max_input_codepoints": 400,
    }
    definition["parameter_domains"]["tts"] = caps | {"audio_delivery_modes": ["complete"]}
    manifest["qualification"]["config_bundle_digest"] = (
        "sha256:"
        + hashlib.sha256(
            rfc8785.dumps({"artifact_pin": tts_pin, "binding_definition": definition})
        ).hexdigest()
    )
    raw_scenarios = json.dumps(bundle["evidence"]).encode()
    report["evidence_digest"] = "sha256:" + hashlib.sha256(raw_scenarios).hexdigest()
    raw_manifest = json.dumps(manifest).encode()
    raw_report = json.dumps(report).encode()
    manifest_digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
    report_digest = "sha256:" + hashlib.sha256(raw_report).hexdigest()
    image_digest = report["software"]["engine_components"][stage["stage_id"]]["image_digest"]
    args = {
        "key_digest": "sha256:" + "a" * 64,
        "approval_id": "approval-1",
        "database": dbname,
        "schema": schema,
        "installation_id": installation,
        "expected_head": None,
        "artifact_pin": json.dumps(tts_pin).encode(),
        "manifest": raw_manifest,
        "report": raw_report,
        "scenario_evidence": raw_scenarios,
    }
    try:
        store = PostgresPublications(pool)
        with pytest.raises(BuiltinVoicePublicationRejectedError, match="not_qualified"):
            publish_builtin_voice(store, **args)
        digest = "sha256:" + "1" * 64
        caps = {
            "languages": ["en"],
            "audio_formats": ["wav"],
            "sample_rates_hz": [24000],
            "modes": ["complete"],
            "max_input_codepoints": 400,
        }
        doc = {
            "resource_type": "voice_revision",
            "voice_id": "voice-vivian",
            "revision": "1",
            "object": "nexus.voice_revision",
            "status": "published",
            "display_name": "Vivian",
            "source": {
                "type": "builtin",
                "draft_id": None,
                "draft_revision": None,
                "source_asset_digest": None,
            },
            "model_binding": {"id": "binding-tts", "revision": "1"},
            "capabilities": caps,
            "validation": {
                "validation_id": "report-1",
                "status": "passed",
                "checked_at": "2026-01-01T00:00:00Z",
            },
            "published_at": "2026-01-01T00:00:00Z",
            "retired_at": None,
            "disabled_reason": None,
        }
        expires_us = int(expiry.timestamp()) * 1_000_000
        facts = {
            "source_approval_id": "source-1",
            "source_revision": "source-rev",
            "source_digest": digest,
            "qualification_report_id": "report-1",
            "qualification_report_digest": report_digest,
            "capacity_manifest_id": "manifest-1",
            "capacity_manifest_digest": manifest_digest,
            "probe_evidence_digest": digest,
            "verifier_id": "builtin-customvoice-v1",
            "verifier_version": "1",
            "required_checks": [
                "source_approved",
                "artifact_integrity",
                "engine_identity",
                "voice_speaker_binding",
                "wav_complete",
                "output_bounds",
                "native_terminal",
                "stop_release",
            ],
            "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "model_revision": tts_pin["model"]["revision"],
            "model_artifact_digest": stage["model_revision_digest"],
            "image_digest": image_digest,
            "generation": "1",
            "instance_id": "instance-1",
            "host_node_id": "host-1",
            "gateway_id": "gateway-1",
            "gateway_policy_revision_id": "policy-1",
            "gateway_policy_digest": digest,
            "binding_id": "binding-tts",
            "binding_revision": "1",
            "binding_revision_id": "binding-rev-1",
            "native_speaker_id": "Vivian",
            "checked_at_us": 1_767_225_600_000_000,
            "expires_at_us": expires_us,
            "public_document": doc,
            "qualified_capabilities": caps,
        }
        db.execute(
            "INSERT INTO speech_builtin_voice_source_approvals VALUES (%s,%s,%s,%s,%s,%s)",
            ("source-1", "voice-vivian", "source-rev", digest, "Vivian", expiry),
        )
        db.execute(
            "INSERT INTO speech_builtin_voice_approvals VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                "approval-1",
                "voice-vivian",
                "1",
                "voice-rev-1",
                "binding-rev-1",
                "source-1",
                json.dumps(facts),
                expiry,
            ),
        )
        db.execute(
            "INSERT INTO speech_publications VALUES ('binding','binding-tts','1','binding-rev-1',1)"
        )
        db.execute(
            "INSERT INTO speech_publication_heads VALUES "
            "('binding','binding-tts','1','binding-rev-1',1)"
        )
        db.execute("INSERT INTO speech_binding_families VALUES ('binding-tts','1')")
        db.execute(
            "INSERT INTO speech_binding_commands VALUES (%s,'binding-tts',1,'{}',%s)",
            (digest, digest),
        )
        db.execute(
            "INSERT INTO speech_binding_revisions VALUES "
            "('binding-tts','1','binding-rev-1',%s,1,%s,%s,%s,1)",
            (digest, json.dumps(definition), digest, digest),
        )
        db.execute(
            "INSERT INTO speech_binding_publication_evidence VALUES "
            "('binding','binding-tts','1','binding-rev-1',"
            "'manifest-1','report-1','operator_cli',1,%s,%s)",
            (manifest_digest, report_digest),
        )
        with db.transaction():
            db.execute(f'SET LOCAL ROLE "{role}"')
            for table in (
                "speech_builtin_voice_source_approvals",
                "speech_builtin_voice_approvals",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
                    db.execute(f"INSERT INTO {table} DEFAULT VALUES")

        def assert_no_voice_publication() -> None:
            for table in ("speech_publications", "speech_publication_heads"):
                row = db.execute(f"SELECT count(*) AS n FROM {table} WHERE kind='voice'").fetchone()
                assert row is not None and row["n"] == 0
            for table in (
                "speech_builtin_voice_qualifications",
                "speech_builtin_voice_publish_intents",
            ):
                row = db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
                assert row is not None and row["n"] == 0

        with pytest.raises(PublicationUnavailableError):
            publish_builtin_voice(store, **(args | {"report": raw_report + b" "}))
        assert_no_voice_publication()
        db.execute(
            "UPDATE speech_builtin_voice_source_approvals SET source_revision='changed' "
            "WHERE source_approval_id='source-1'"
        )
        with pytest.raises(BuiltinVoicePublicationRejectedError, match="evidence_mismatch"):
            publish_builtin_voice(store, **args)
        assert_no_voice_publication()
        db.execute(
            "UPDATE speech_builtin_voice_source_approvals SET source_revision='source-rev' "
            "WHERE source_approval_id='source-1'"
        )
        result = publish_builtin_voice(store, **args)
        assert result["status"] == "created"
        db.execute(
            "INSERT INTO speech_builtin_voice_revocations (approval_id,reason) "
            "VALUES ('approval-1','revoked')"
        )
        assert publish_builtin_voice(store, **args)["status"] == "replay"
        with pytest.raises(BuiltinVoicePublicationRejectedError, match="idempotency_conflict"):
            publish_builtin_voice(store, **(args | {"report": b"changed"}))
        with pytest.raises(BuiltinVoicePublicationRejectedError, match="idempotency_conflict"):
            publish_builtin_voice(store, **(args | {"expected_head": "1"}))
    finally:
        pool.close()
