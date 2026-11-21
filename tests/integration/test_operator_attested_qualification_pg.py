"""PostgreSQL acceptance for the operator-attested qualification writer.

Every fact here is synthetic: a synthetic binding publication, synthetic release
evidence and a synthetic registration ledger. The point is not that a real engine
was measured -- it is that the two qualification sources are recorded, told apart
by the durable ledger, and that the attestation path cannot pretend to be a
controller observation.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
import rfc8785
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from tests.support.portable_pg import connect as portable_pg_connect
from tests.unit.backend.test_binding_release_evidence import packet

from speechnexus_backend.builtin_voice_publication import (
    BuiltinVoicePublicationRejectedError,
    _recorded_source,
    publish_builtin_voice,
)
from speechnexus_backend.operator_attested_qualification import (
    record_operator_attestation,
)
from speechnexus_backend.qualification_provenance import (
    REPORT_SQL,
)
from speechnexus_backend.qualification_provenance import (
    main as provenance_main,
)
from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.domain.operator_attestation import (
    OPERATOR_CHECKS,
    AttestationRequest,
    OperatorAttestationError,
    TrustDeclaration,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    builtin_publisher_grants,
    operator_attestation_writer_grants,
    qualification_reader_grants,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
    require_builtin_voice,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.qualification_source_evidence import (
    SOURCE_UNVERIFIED,
    qualification_source_evidenced_sql,
)

pytestmark = pytest.mark.integration

INSTANCE = "instance-tts-1"
GENERATION = 1
FENCING = 7
POOL = "pool-tts"
WORKSPACE = "wsp-tts"
POLICY = "policy-1"
HOST = "host-1"
GATEWAY = "gateway-1"
TEMPLATE = "template-tts-customvoice"
VOICE = "voice-vivian"
REVISION = "1"
REVISION_ID = "voice-rev-1"
BINDING = "binding-tts"
BINDING_REVISION = "1"
BINDING_REVISION_ID = "binding-rev-1"
SOURCE = "source-1"
APPROVAL = "approval-att-1"
ATTESTATION = "att-tts-1"
PASSWORD = "synthetic_operator_attestation"
RUNTIME_TABLES = (
    "runtime_instance_intents",
    "runtime_instance_heads",
    "engine_instances",
    "resource_pools",
    "host_nodes",
    "workspace_quotas",
    "admission_leases",
    "execution_stop_evidence",
    "runtime_intent_deliveries",
    "admission_attempts",
    "admission_operations",
    "admission_requests",
    "coordination_jobs",
    "coordination_outbox",
)


@pytest.fixture
def pg() -> Iterator[tuple[psycopg.Connection[dict[str, Any]], str, str]]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not evidence"
        )
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_oa_" + suffix, "sn_oa_r_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = schema
    with engine.connect() as migration_connection:
        config.attributes["connection"] = migration_connection
        command.upgrade(config, "head")
    engine.dispose()
    connection = portable_pg_connect(url, autocommit=True, row_factory=dict_row)
    connection.execute(f'SET search_path TO "{schema}", pg_catalog')
    connection.execute(f'CREATE ROLE "{role}"')
    try:
        yield connection, schema, role
    finally:
        connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        for name in _roles(schema):
            connection.execute(f'DROP ROLE IF EXISTS "{name}"')
        connection.close()


def _roles(schema: str) -> list[str]:
    suffix = schema.removeprefix("sn_oa_")
    return ["sn_oa_r_" + suffix, "sn_oa_r_" + suffix + "_oa"]


def _timestamp(us: int) -> str:
    return (
        datetime.fromtimestamp(us / 1_000_000, tz=UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class Ledger:
    """The published binding, the release evidence and the registration facts."""

    def __init__(self, db: psycopg.Connection[dict[str, Any]], schema: str) -> None:
        self.db = db
        self.schema = schema
        now = datetime.now(UTC).replace(microsecond=0)
        self.now = now
        bundle = packet()
        manifest, report, definition = bundle["manifest"], bundle["report"], bundle["definition"]
        self.pin = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "deploy/engines/file-tts-customvoice/artifact-pin.json"
            ).read_text()
        )
        manifest["binding_revision_id"] = report["binding_revision_id"] = BINDING_REVISION_ID
        manifest["manifest_id"] = report["capacity_manifest_id"] = "manifest-1"
        report["report_id"] = manifest["qualification"]["qualification_report_id"] = "report-1"
        report["binding"] = {"id": BINDING, "revision": BINDING_REVISION}
        manifest["supported_tasks"] = definition["tasks"] = ["tts_file"]
        definition["execution_recipe"]["capacity_manifest_id"] = "manifest-1"
        definition["execution_recipe"]["stage_roles"] = ["tts_connector"]
        stage = manifest["stage_graph"][0]
        stage["stage_role"] = "tts_connector"
        stage["model_revision_digest"] = (
            "sha256:" + hashlib.sha256(rfc8785.dumps(self.pin["model"])).hexdigest()
        )
        report["expires_at"] = manifest["qualification"]["expires_at"] = (
            (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
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
                rfc8785.dumps({"artifact_pin": self.pin, "binding_definition": definition})
            ).hexdigest()
        )
        self.raw_scenarios = json.dumps(bundle["evidence"]).encode()
        report["evidence_digest"] = "sha256:" + hashlib.sha256(self.raw_scenarios).hexdigest()
        self.raw_manifest = json.dumps(manifest).encode()
        self.raw_report = json.dumps(report).encode()
        self.definition = definition
        self.manifest_digest = "sha256:" + hashlib.sha256(self.raw_manifest).hexdigest()
        self.report_digest = "sha256:" + hashlib.sha256(self.raw_report).hexdigest()
        self.image_digest = report["software"]["engine_components"][stage["stage_id"]][
            "image_digest"
        ]
        self.model_digest = stage["model_revision_digest"]
        self.capabilities = caps
        self.expires_at = now + timedelta(hours=1)
        database_row = db.execute("SELECT current_database() AS name").fetchone()
        installation_row = db.execute(
            "SELECT installation_id FROM speech_publication_installation"
        ).fetchone()
        assert database_row is not None and installation_row is not None
        self.database = str(database_row["name"])
        self.installation_id = str(installation_row["installation_id"])
        self.digest = "sha256:" + "1" * 64
        self.pools: list[Any] = []
        self._registration()
        self._publication()

    def _registration(self) -> None:
        db = self.db
        db.execute(
            "INSERT INTO host_nodes (host_node_id,memory_architecture,node_state,topology_digest) "
            "VALUES (%s,'unified','active',%s)",
            (HOST, "sha256:" + "c" * 64),
        )
        db.execute(
            "INSERT INTO resource_pools (pool_id,service_class,pool_state,owner_id,fencing_token,"
            "owner_expires_at_us,owner_state) VALUES (%s,'file_tts','ready','scheduler',%s,%s,"
            "'active')",
            (POOL, FENCING, int((self.now + timedelta(minutes=10)).timestamp() * 1_000_000)),
        )
        db.execute(
            "INSERT INTO workspace_quotas (workspace_id,max_attempts,policy_revision) "
            "VALUES (%s,%s,%s)",
            (WORKSPACE, 5, POLICY),
        )
        db.execute(
            "INSERT INTO engine_instances (instance_id,instance_generation,host_node_id,pool_id,"
            "gateway_id,binding_revision_id,manifest_id,approved_template_id,management_mode,"
            "loading_policy,config_digest,instance_state,max_slots,last_fencing_token,version,"
            "base_budget_kind) VALUES "
            "(%s,%s,%s,%s,%s,%s,'manifest-1',%s,'operator_external','eager',%s,'ready',1,%s,2,"
            "'steady')",
            (
                INSTANCE,
                GENERATION,
                HOST,
                POOL,
                GATEWAY,
                BINDING_REVISION_ID,
                TEMPLATE,
                "sha256:" + "d" * 64,
                FENCING,
            ),
        )
        db.execute(
            "INSERT INTO runtime_instance_heads (instance_id,current_generation,"
            "generation_high_water,version) VALUES (%s,%s,%s,1)",
            (INSTANCE, GENERATION, GENERATION),
        )

    def _publication(self) -> None:
        db = self.db
        db.execute(
            "INSERT INTO speech_builtin_voice_source_approvals VALUES (%s,%s,%s,%s,%s,%s)",
            (SOURCE, VOICE, "source-rev", self.digest, "Vivian", self.expires_at),
        )
        db.execute(
            "INSERT INTO speech_publications VALUES ('binding',%s,%s,%s,1)",
            (BINDING, BINDING_REVISION, BINDING_REVISION_ID),
        )
        db.execute(
            "INSERT INTO speech_publication_heads VALUES ('binding',%s,%s,%s,1)",
            (BINDING, BINDING_REVISION, BINDING_REVISION_ID),
        )
        db.execute("INSERT INTO speech_binding_families VALUES (%s,'1')", (BINDING,))
        db.execute(
            "INSERT INTO speech_binding_commands VALUES (%s,%s,1,'{}',%s)",
            (self.digest, BINDING, self.digest),
        )
        db.execute(
            "INSERT INTO speech_binding_revisions VALUES (%s,'1',%s,%s,1,%s,%s,%s,1)",
            (
                BINDING,
                BINDING_REVISION_ID,
                self.digest,
                json.dumps(self.definition),
                self.digest,
                self.digest,
            ),
        )
        db.execute(
            "INSERT INTO speech_binding_publication_evidence VALUES "
            "('binding',%s,'1',%s,'manifest-1','report-1','operator_cli',1,%s,%s)",
            (BINDING, BINDING_REVISION_ID, self.manifest_digest, self.report_digest),
        )

    def release(self) -> dict[str, bytes]:
        return {
            "artifact_pin": json.dumps(self.pin).encode(),
            "manifest": self.raw_manifest,
            "report": self.raw_report,
            "scenario_evidence": self.raw_scenarios,
        }

    def declaration(self, **changes: Any) -> dict[str, Any]:
        from speechnexus_backend.speech.domain.operator_attestation import REQUIRED_GAPS

        value: dict[str, Any] = {
            "format": "speechnexus-operator-attested-engine-v1",
            "declared_by": "ops-oncall",
            "declared_at": _timestamp(int(self.now.timestamp() * 1_000_000) - 3_600_000_000),
            "installation": {
                "database": self.database,
                "schema": self.schema,
                "installation_id": self.installation_id,
            },
            "scope": {"lifecycle_controller": "operator_external"},
            "trusted_instances": [INSTANCE],
            "acknowledged_gaps": sorted(REQUIRED_GAPS),
            "max_attestation_validity_seconds": 86_400,
        }
        value.update(changes)
        return value

    def lifecycle(self, **changes: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "format": "speechnexus-external-lifecycle-v1",
            "lifecycle_controller": "operator_external",
            "declared_by": "ops-oncall",
            "instance_id": INSTANCE,
            "instance_generation": GENERATION,
            "fencing_token": FENCING,
            "engine_identity": {
                "image_digest": self.image_digest,
                "template_id": TEMPLATE,
                "engine_auth": "vllm_api_key_v1",
            },
        }
        value.update(changes)
        return value

    def request(self, **changes: Any) -> dict[str, Any]:
        observed_us = int(self.now.timestamp() * 1_000_000) - 60_000_000
        value: dict[str, Any] = {
            "format": "speechnexus-operator-attested-qualification-v1",
            "attestation_id": ATTESTATION,
            "approval_id": APPROVAL,
            "installation": {
                "database": self.database,
                "schema": self.schema,
                "installation_id": self.installation_id,
            },
            "voice": {
                "voice_id": VOICE,
                "revision": REVISION,
                "revision_id": REVISION_ID,
                "source_approval_id": SOURCE,
            },
            "binding": {"binding_id": BINDING, "binding_revision_id": BINDING_REVISION_ID},
            "engine": {
                "instance_id": INSTANCE,
                "instance_generation": GENERATION,
                "fencing_token": FENCING,
                "template_id": TEMPLATE,
                "image_digest": self.image_digest,
                "template_document": {
                    "format": "speechnexus-template-v1",
                    "template": {
                        "template_id": TEMPLATE,
                        "image_digest": self.image_digest,
                        "engine_auth": "vllm_api_key_v1",
                    },
                },
                "lifecycle_document": self.lifecycle(),
            },
            "model": {
                "model_id": self.pin["model"]["repository"],
                "model_revision": self.pin["model"]["revision"],
                "model_artifact_digest": self.model_digest,
            },
            "capabilities": self.capabilities,
            "window": {
                "observed_at": _timestamp(observed_us),
                "expires_at": _timestamp(observed_us + 3_600_000_000),
            },
        }
        value.update(changes)
        return value

    def gateway_evidence(self, **changes: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "gateway_policy_digest": "sha256:" + "e" * 64,
            "policy_revision_id": POLICY,
            "voice_id": VOICE,
            "voice_revision": REVISION,
            "voice_revision_id": REVISION_ID,
            "native_speaker_id": "Vivian",
            "binding_revision_id": BINDING_REVISION_ID,
            "node_id": HOST,
            "gateway_id": GATEWAY,
            "instance_id": INSTANCE,
            "instance_generation": GENERATION,
            "observed_at": _timestamp(int(self.now.timestamp() * 1_000_000) - 1_000_000),
        }
        value.update(changes)
        return value

    def count(self, table: str) -> int:
        row = self.db.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()
        assert row is not None
        return int(row["n"])

    def snapshot(self) -> dict[str, int]:
        return {table: self.count(table) for table in RUNTIME_TABLES}

    def probe_authorization(
        self,
        authorization: str = "probe-auth-1",
        *,
        source_approval_id: str = SOURCE,
        voice_id: str = VOICE,
    ) -> str:
        """The row only the controller path can write; it is what backs the label."""
        self.db.execute(
            "INSERT INTO speech_builtin_voice_probe_authorizations "
            "(authorization_id,source_approval_id,voice_id,intent) "
            "VALUES (%s,%s,%s,'{}'::jsonb)",
            (authorization, source_approval_id, voice_id),
        )
        return authorization

    def writer(self, role: str, password: str = PASSWORD) -> PostgresPublications:
        """The separate minimal identity that may only append one attestation."""
        identity = role + "_oa"
        self.db.execute(f'CREATE ROLE "{identity}"')
        self.db.execute(f"ALTER ROLE \"{identity}\" LOGIN PASSWORD '{password}'")
        for grant in operator_attestation_writer_grants(self.schema, identity):
            self.db.execute(grant)
        return self._store(identity, password)

    def reader(self, suffix: str = "_qr", password: str = PASSWORD) -> str:
        """Provision the read-only audit identity from ``qualification_reader_grants``."""
        identity = f"{self.schema[:20]}{suffix}"
        self.db.execute(f'CREATE ROLE "{identity}"')
        self.db.execute(f"ALTER ROLE \"{identity}\" LOGIN PASSWORD '{password}'")
        for grant in qualification_reader_grants(self.schema, identity):
            self.db.execute(grant)
        return identity

    def read_rows_as(self, identity: str, password: str = PASSWORD) -> list[dict[str, Any]]:
        """Run the shipped report SQL as that identity, not as the schema owner."""
        with portable_pg_connect(
            make_conninfo(
                os.environ["SPEECHNEXUS_TEST_DATABASE_URL"],
                user=identity,
                password=password,
            ),
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(f'SET search_path TO "{self.schema}", pg_catalog')
            return connection.execute(REPORT_SQL).fetchall()

    def publisher(self, role: str, password: str = PASSWORD) -> PostgresPublications:
        self.db.execute(f"ALTER ROLE \"{role}\" LOGIN PASSWORD '{password}'")
        for grant in (
            *publication_grants(self.schema, role),
            *builtin_publisher_grants(self.schema, role),
        ):
            self.db.execute(grant)
        return self._store(role, password)

    def _store(self, role: str, password: str) -> PostgresPublications:
        pool = speech_pool(
            make_conninfo(
                os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], user=role, password=password
            ),
            schema=self.schema,
            max_size=1,
        )
        pool.open()
        self.pools.append(pool)
        return PostgresPublications(pool, timeout_seconds=3)

    def attest(self, store: PostgresPublications, **overrides: Any) -> dict[str, Any]:
        declaration = TrustDeclaration.parse(overrides.pop("declaration", self.declaration()))
        request = AttestationRequest.parse(overrides.pop("request", self.request()))
        evidence = overrides.pop("gateway_evidence", self.gateway_evidence())
        release = self.release()
        for name, value in overrides.items():
            release[name] = value
        return record_operator_attestation(
            store,
            declaration=declaration,
            request=request,
            gateway_evidence=evidence,
            workspace_id=WORKSPACE,
            **release,
        )

    def publish(
        self,
        store: PostgresPublications,
        approval_id: str = APPROVAL,
        key_digest: str = "sha256:" + "f" * 64,
    ) -> dict[str, Any]:
        head = self.db.execute(
            "SELECT revision FROM speech_publication_heads WHERE kind='voice' AND object_id=%s",
            (VOICE,),
        ).fetchone()
        expected_head = None if head is None else str(head["revision"])
        return publish_builtin_voice(
            store,
            key_digest=key_digest,
            approval_id=approval_id,
            database=self.database,
            schema=self.schema,
            installation_id=self.installation_id,
            expected_head=expected_head,
            **self.release(),
        )


def _report(db: psycopg.Connection[dict[str, Any]], schema: str) -> list[dict[str, Any]]:
    out, err = io.StringIO(), io.StringIO()
    rows = _report_rows(db, schema)
    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    code = provenance_main(
        ["--database-url", "postgresql://unused/synthetic", "--schema", schema],
        env={},
        connect=lambda url, name: (rows, now_us),
        stdout=out,
        stderr=err,
    )
    assert code == 0, err.getvalue()
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _report_rows(db: psycopg.Connection[dict[str, Any]], schema: str) -> list[dict[str, Any]]:
    with db.cursor() as cursor:
        cursor.execute(REPORT_SQL)
        return cursor.fetchall()


def _admit(db: psycopg.Connection[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """Run the lookup synchronous TTS admission and the Job path both call."""
    with db.cursor() as cursor:
        return require_builtin_voice(
            cursor,
            voice_id=overrides.pop("voice_id", VOICE),
            revision=overrides.pop("revision", int(REVISION)),
            revision_id=overrides.pop("revision_id", REVISION_ID),
            binding_revision_id=BINDING_REVISION_ID,
            instance_id=INSTANCE,
            instance_generation=GENERATION,
            host_node_id=HOST,
            gateway_id=GATEWAY,
            policy_revision_id=POLICY,
        )


def _admission_answer(db: psycopg.Connection[dict[str, Any]], **overrides: Any) -> str:
    """The refusal code, or ``accepted`` when the lookup returns the facts."""
    try:
        _admit(db, **overrides)
    except AdmissionRejectedError as error:
        return error.code
    return "accepted"


def _refusal_detail(db: psycopg.Connection[dict[str, Any]]) -> str | None:
    """The detail sentence an operator would read for this row's refusal."""
    try:
        _admit(db)
    except AdmissionRejectedError as error:
        return error.detail
    return None


def _job_fallback_selects_the_row(db: psycopg.Connection[dict[str, Any]]) -> bool:
    """Whether the public Job builtin fallback would treat this as a builtin voice."""
    row = db.execute(
        "SELECT 1 FROM speech_builtin_voice_qualifications q "
        "WHERE q.voice_id=%s AND q.revision=%s AND q.revision_id=%s "
        f"AND ({qualification_source_evidenced_sql('q')})",
        (VOICE, REVISION, REVISION_ID),
    ).fetchone()
    return row is not None


def test_attested_qualification_publishes_and_is_read_back_as_operator_sourced(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, role = pg
    ledger = Ledger(db, schema)
    before = ledger.snapshot()
    writer = ledger.writer(role)
    try:
        outcome = ledger.attest(writer)
        assert outcome["outcome"] == "committed"
        row = db.execute(
            "SELECT facts FROM speech_builtin_voice_approvals WHERE approval_id=%s",
            (APPROVAL,),
        ).fetchone()
        assert row is not None
        facts = row["facts"]
        # The two sources are distinguishable by the recorded facts alone.
        assert facts["verifier_id"] == "operator-attested-customvoice-v1"
        assert set(facts["required_checks"]) == OPERATOR_CHECKS
        assert "probe_evidence_digest" not in facts
        assert facts["declaration_digest"] == outcome["declaration_digest"]
        assert facts["attestation"]["instance_id"] == INSTANCE
        assert facts["attestation"]["fencing_token"] == str(FENCING)
        attestation = db.execute(
            "SELECT * FROM speech_builtin_voice_operator_attestations WHERE attestation_id=%s",
            (ATTESTATION,),
        ).fetchone()
        assert attestation is not None
        assert (attestation["instance_generation"], attestation["fencing_token"]) == (
            GENERATION,
            FENCING,
        )
        assert attestation["declaration"]["declared_by"] == "ops-oncall"
        assert attestation["template_id"] == TEMPLATE
        assert attestation["declaration_digest"] == outcome["declaration_digest"]
    finally:
        _close(ledger)
    publisher = ledger.publisher(role)
    try:
        published = ledger.publish(publisher)
        assert published["status"] == "created"
        provenance = db.execute(
            "SELECT source_kind, attestation_id FROM "
            "speech_builtin_voice_qualification_provenance WHERE voice_id=%s AND revision=%s",
            (VOICE, REVISION),
        ).fetchone()
        assert provenance == {"source_kind": "operator_attested", "attestation_id": ATTESTATION}
        with db.cursor() as cursor:
            require_builtin_voice(
                cursor,
                voice_id=VOICE,
                revision=int(REVISION),
                revision_id=REVISION_ID,
                binding_revision_id=BINDING_REVISION_ID,
                instance_id=INSTANCE,
                instance_generation=GENERATION,
                host_node_id=HOST,
                gateway_id=GATEWAY,
                policy_revision_id=POLICY,
            )
        # The read surface names the source from the ledger, not from topology.
        entries = _report(db, schema)
        voice = [entry for entry in entries if entry.get("voice_id") == VOICE]
        assert voice[0]["source_kind"] == "operator_attested"
        assert voice[0]["provenance_recorded"] is True
        assert voice[0]["attestation_id"] == ATTESTATION
        assert voice[0]["declared_by"] == "ops-oncall"
        assert voice[0]["instance_id"] == INSTANCE
        assert voice[0]["attestation_state"] == "live"
        assert entries[-1]["summary"]["operator_attested"] == 1
        # Nothing in the runtime ledger moved: an attestation is not execution.
        assert ledger.snapshot() == before
    finally:
        _close(ledger)


def _close(ledger: Ledger) -> None:
    for pool in ledger.pools:
        pool.close()
    ledger.pools.clear()


@pytest.mark.parametrize(
    "case, code",
    [
        ("declaration_target", "trust_declaration_target_mismatch"),
        ("instance_untrusted", "trust_declaration_instance_not_trusted"),
        ("image", "image_digest_mismatch"),
        ("template", "template_identity_mismatch"),
        ("generation", "instance_generation_mismatch"),
        ("fencing", "stale_fencing_token"),
        ("window_expired", "attestation_window_expired"),
        ("gateway", "gateway_policy_evidence_unavailable"),
        ("model", "model_artifact_digest_mismatch"),
        ("capabilities", "capability_outside_binding_domains"),
        ("release_evidence", "binding_release_evidence_mismatch"),
        ("source_expired", "source_approval_unavailable"),
    ],
)
def test_each_declared_mismatch_is_refused_by_name_and_writes_nothing(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str], case: str, code: str
) -> None:
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    try:
        overrides: dict[str, Any] = {}
        if case == "declaration_target":
            overrides["declaration"] = ledger.declaration(
                installation={**ledger.declaration()["installation"], "schema": "other"}
            )
        elif case == "instance_untrusted":
            overrides["declaration"] = ledger.declaration(trusted_instances=["instance-other"])
        elif case == "source_expired":
            db.execute(
                "UPDATE speech_builtin_voice_source_approvals "
                "SET expires_at=clock_timestamp() - interval '1 second'"
            )
        else:
            request = ledger.request()
            if case == "image":
                request["engine"]["image_digest"] = "sha256:" + "9" * 64
            elif case == "template":
                request["engine"]["template_id"] = "template-other"
            elif case == "generation":
                request["engine"]["instance_generation"] = GENERATION + 1
            elif case == "fencing":
                request["engine"]["fencing_token"] = FENCING - 1
            elif case == "window_expired":
                request["window"] = {
                    "observed_at": _timestamp(now_us - 90_000_000),
                    "expires_at": _timestamp(now_us - 1_000_000),
                }
            elif case == "model":
                request["model"]["model_artifact_digest"] = "sha256:" + "9" * 64
            elif case == "capabilities":
                request["capabilities"] = {**request["capabilities"], "sample_rates_hz": [48000]}
            if case == "gateway":
                overrides["gateway_evidence"] = ledger.gateway_evidence(
                    policy_revision_id="policy-2"
                )
            else:
                overrides["request"] = request
        if case == "release_evidence":
            overrides["report"] = json.dumps({"report_id": "tampered"}).encode()
        with pytest.raises(OperatorAttestationError) as error:
            ledger.attest(writer, **overrides)
        assert error.value.code == code
        assert ledger.count("speech_builtin_voice_approvals") == 0
        assert ledger.count("speech_builtin_voice_operator_attestations") == 0
        assert ledger.count("speech_builtin_voice_qualifications") == 0
    finally:
        _close(ledger)


def test_attestation_is_idempotent_and_a_conflict_is_named(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        first = ledger.attest(writer)
        replayed = ledger.attest(writer)
        assert (first["outcome"], replayed["outcome"]) == ("committed", "replayed")
        assert ledger.count("speech_builtin_voice_approvals") == 1
        assert ledger.count("speech_builtin_voice_operator_attestations") == 1
        now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
        with pytest.raises(OperatorAttestationError) as conflict:
            ledger.attest(
                writer,
                request=ledger.request(
                    window={
                        "observed_at": _timestamp(now_us - 90_000_000),
                        "expires_at": _timestamp(now_us + 1_800_000_000),
                    }
                ),
            )
        assert conflict.value.code == "attestation_conflict"
    finally:
        _close(ledger)


def test_the_writer_identity_cannot_repair_or_erase_its_own_record(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
    finally:
        _close(ledger)
    # A publisher credential holds publication-head authority and is therefore
    # refused as the attestation writer (it can repoint what publication shows).
    publisher = ledger.publisher(role)
    try:
        with pytest.raises(OperatorAttestationError) as forbidden:
            ledger.attest(publisher)
        assert forbidden.value.code == "writer_identity_forbidden"
        assert ledger.count("speech_builtin_voice_approvals") == 1
    finally:
        _close(ledger)
    identity = role + "_oa"
    login = portable_pg_connect(
        make_conninfo(
            os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], user=identity, password=PASSWORD
        ),
        autocommit=True,
        row_factory=dict_row,
    )
    try:
        login.execute(f'SET search_path TO "{schema}", pg_catalog')
        for statement in (
            "UPDATE speech_builtin_voice_approvals SET expires_at=clock_timestamp()",
            "UPDATE speech_builtin_voice_operator_attestations SET fencing_token=1",
            "DELETE FROM speech_builtin_voice_operator_attestations",
            "INSERT INTO speech_builtin_voice_qualifications VALUES "
            "('voice-vivian','1','voice-rev-1','binding-rev-1','approval-att-1','{}'::jsonb,'Vivian')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                login.execute(statement)
    finally:
        login.close()


def test_attestation_opens_no_compute_release_and_no_native_evidence(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, role = pg
    ledger = Ledger(db, schema)
    before = ledger.snapshot()
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
    finally:
        _close(ledger)
    publisher = ledger.publisher(role)
    try:
        ledger.publish(publisher)
    finally:
        _close(ledger)
    assert ledger.snapshot() == before
    # No lease, intent, stop evidence or delivery was produced by the attestation.
    for table in (
        "runtime_instance_intents",
        "runtime_intent_deliveries",
        "admission_leases",
        "execution_stop_evidence",
    ):
        assert ledger.count(table) == 0
    pool = db.execute("SELECT owner_state, version FROM resource_pools").fetchone()
    assert pool == {"owner_state": "active", "version": 1}
    instance = db.execute(
        "SELECT instance_state, base_budget_kind FROM engine_instances"
    ).fetchone()
    assert instance == {"instance_state": "ready", "base_budget_kind": "steady"}
    # The invariant is the grant surface, not the code path: the declaration
    # identity cannot even read the evidence a native qualification needs, so a
    # defect in the writer still cannot manufacture runtime state. `resource_pools`
    # and `workspace_quotas` are the only runtime relations it may read at all.
    with portable_pg_connect(
        make_conninfo(
            os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], user=role + "_oa", password=PASSWORD
        ),
        autocommit=True,
        row_factory=dict_row,
    ) as identity:
        identity.execute(f'SET search_path TO "{schema}", pg_catalog')
        for table in (
            "host_nodes",
            "admission_leases",
            "execution_stop_evidence",
            "runtime_instance_intents",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                identity.execute(f"SELECT 1 FROM {table} LIMIT 0")
        for table in ("resource_pools", "workspace_quotas"):
            identity.execute(f"SELECT 1 FROM {table} LIMIT 0")


def _renewal(ledger: Ledger) -> tuple[dict[str, Any], dict[str, Any]]:
    """A second, independent declaration for revision 2 of the same voice."""
    request = ledger.request(
        attestation_id=ATTESTATION + "-2",
        approval_id=APPROVAL + "-2",
        voice={
            "voice_id": VOICE,
            "revision": "2",
            "revision_id": "voice-rev-2",
            "source_approval_id": SOURCE,
        },
    )
    evidence = ledger.gateway_evidence(voice_revision="2", voice_revision_id="voice-rev-2")
    return request, evidence


def test_a_second_attestation_for_the_same_voice_revision_is_refused_by_name(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """One voice revision carries one attestation; renewal needs a new revision."""
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
        with pytest.raises(OperatorAttestationError) as error:
            ledger.attest(
                writer,
                request=ledger.request(
                    attestation_id=ATTESTATION + "-again",
                    approval_id=APPROVAL + "-again",
                ),
            )
        assert error.value.code == "attestation_revision_already_attested"
        assert ledger.count("speech_builtin_voice_operator_attestations") == 1
        assert ledger.count("speech_builtin_voice_approvals") == 1
    finally:
        _close(ledger)


def test_renewal_publishes_a_second_qualification_under_a_new_voice_revision(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """The documented cure for an expiring attestation, executed end to end."""
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
        publisher = ledger.publisher(role)
        ledger.publish(publisher)
        request, evidence = _renewal(ledger)
        outcome = ledger.attest(writer, request=request, gateway_evidence=evidence)
        assert outcome["outcome"] == "committed"
        assert outcome["revision"] == "2"
        ledger.publish(publisher, approval_id=APPROVAL + "-2", key_digest="sha256:" + "e" * 64)
        qualifications = db.execute(
            "SELECT revision FROM speech_builtin_voice_qualifications "
            "WHERE voice_id=%s ORDER BY revision",
            (VOICE,),
        ).fetchall()
        assert [row["revision"] for row in qualifications] == ["1", "2"]
        provenance = db.execute(
            "SELECT revision, source_kind, evidence_kind FROM "
            "speech_builtin_voice_qualification_provenance WHERE voice_id=%s ORDER BY revision",
            (VOICE,),
        ).fetchall()
        assert provenance == [
            {
                "revision": "1",
                "source_kind": "operator_attested",
                "evidence_kind": "operator_attestation",
            },
            {
                "revision": "2",
                "source_kind": "operator_attested",
                "evidence_kind": "operator_attestation",
            },
        ]
        head = db.execute(
            "SELECT revision FROM speech_publication_heads WHERE kind='voice' AND object_id=%s",
            (VOICE,),
        ).fetchone()
        assert head == {"revision": "2"}
    finally:
        _close(ledger)


def test_a_hand_written_qualification_without_provenance_is_refused(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """The attack the review used: hand-write the two INSERTs, skip the publish command.

    No ``DELETE`` is needed and no privilege is escalated. Writing a qualification row
    is a required permission of the publishing identity, and the provenance row is a
    child table with no database-level requirement that the parent has one, so the
    publishing identity can produce "published, but nothing says where from" directly.
    That shape is refused everywhere admission looks, and the audit report still shows
    it as recorded history rather than pretending the row does not exist.
    """
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
    finally:
        _close(ledger)
    ledger.publisher(role)  # provisions the publishing identity's own grants
    publisher = portable_pg_connect(
        make_conninfo(os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], user=role, password=PASSWORD),
        autocommit=True,
        row_factory=dict_row,
    )
    try:
        publisher.execute("SELECT set_config('search_path', %s, false)", (schema,))
        # Every statement here is one the publishing identity is entitled to run; what
        # it cannot do is make the result look like it came from a source.
        publisher.execute(
            "INSERT INTO speech_publications VALUES ('voice',%s,%s,%s,%s)",
            (VOICE, str(REVISION), REVISION_ID, REVISION),
        )
        publisher.execute(
            "INSERT INTO speech_publications VALUES ('binding',%s,'1',%s,1)",
            (BINDING_REVISION_ID, BINDING_REVISION_ID),
        )
        # Copy the qualification straight out of the approval the identity can read,
        # so the row matches this request in every column -- everything but the source.
        publisher.execute(
            "INSERT INTO speech_builtin_voice_qualifications "
            "(voice_id,revision,revision_id,binding_revision_id,approval_id,document,"
            "native_speaker_id) "
            "SELECT a.voice_id,a.revision,a.revision_id,a.binding_revision_id,a.approval_id,"
            "a.facts->'public_document',a.facts->>'native_speaker_id' "
            "FROM speech_builtin_voice_approvals a WHERE a.approval_id=%s",
            (APPROVAL,),
        )
        assert db.execute(
            "SELECT count(*) AS n FROM speech_builtin_voice_qualification_provenance"
        ).fetchone() == {"n": 0}
        # Published, publishable by hand, and still refused: the missing provenance row
        # is the whole difference.
        assert _admission_answer(db) == SOURCE_UNVERIFIED
        assert _job_fallback_selects_the_row(db) is False
        assert "no qualification source recorded" in cast(str, _refusal_detail(db))
        entries = _report(db, schema)
        assert entries and entries[0]["provenance_recorded"] is False
        assert entries[0]["evidence"] == "legacy_pre_0055"
    finally:
        publisher.close()
        _close(ledger)


def test_the_attested_identity_cannot_publish_a_controller_label(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """The forgery the review found: a probe-shaped approval from the attested identity.

    That identity may insert the eight approval columns but not
    ``probe_authorization_id``, so the row it writes cannot name a probe
    authorization. Publication records it as ``unverified`` -- never as a controller
    observation -- and the ledger itself refuses to store a controller label without
    the evidence.
    """
    db, schema, role = pg
    ledger = Ledger(db, schema)
    ledger.writer(role)  # provisions the minimal identity this test logs in as
    password = PASSWORD
    forged = portable_pg_connect(
        make_conninfo(
            os.environ["SPEECHNEXUS_TEST_DATABASE_URL"],
            user=role + "_oa",
            password=password,
        ),
        autocommit=True,
        row_factory=dict_row,
    )
    try:
        forged.execute("SELECT set_config('search_path', %s, false)", (schema,))
        forged.execute(
            "INSERT INTO speech_builtin_voice_approvals "
            "(approval_id,voice_id,revision,revision_id,binding_revision_id,"
            "source_approval_id,facts,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                APPROVAL,
                VOICE,
                REVISION,
                REVISION_ID,
                BINDING_REVISION_ID,
                SOURCE,
                json.dumps(_probe_facts(ledger)),
                ledger.expires_at,
            ),
        )
        recorded = forged.execute(
            "SELECT probe_authorization_id FROM speech_builtin_voice_approvals "
            "WHERE approval_id=%s",
            (APPROVAL,),
        ).fetchone()
        assert recorded is not None
        assert recorded["probe_authorization_id"] is None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            forged.execute(
                "UPDATE speech_builtin_voice_approvals SET probe_authorization_id=%s "
                "WHERE approval_id=%s",
                ("probe-auth-forged", APPROVAL),
            )
    finally:
        forged.close()
    publisher = ledger.publisher(role)
    try:
        ledger.publish(publisher)
        provenance = db.execute(
            "SELECT source_kind, evidence_kind, attestation_id, probe_authorization_id FROM "
            "speech_builtin_voice_qualification_provenance WHERE voice_id=%s AND revision=%s",
            (VOICE, REVISION),
        ).fetchone()
        assert provenance == {
            "source_kind": "unverified",
            "evidence_kind": "absent",
            "attestation_id": None,
            "probe_authorization_id": None,
        }
        # The forgery is published but admitted nowhere: the lookup synchronous TTS
        # admission calls refuses it by name, and the public Job builtin fallback
        # does not select the row -- so a published, honest-looking "unverified"
        # qualification cannot synthesize.
        assert _admission_answer(db) == SOURCE_UNVERIFIED
        assert _job_fallback_selects_the_row(db) is False
        assert db.execute(
            "SELECT count(*) AS n FROM speech_builtin_voice_qualifications "
            "WHERE voice_id=%s AND revision=%s",
            (VOICE, REVISION),
        ).fetchone() == {"n": 1}
        entries = _report(db, schema)
        assert entries[0]["source_kind"] == "unverified"
        verdicts = {
            "controller_observed"
            for entry in entries
            if entry.get("source_kind") == "controller_observed"
        }
        assert verdicts == set()
        # The ledger backstops the writer: a controller label without its evidence
        # cannot be stored even if a future writer tried.
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "UPDATE speech_builtin_voice_qualification_provenance "
                "SET source_kind='controller_observed', evidence_kind='probe_authorization' "
                "WHERE voice_id=%s AND revision=%s",
                (VOICE, REVISION),
            )
    finally:
        _close(ledger)


def test_an_elapsed_declaration_stops_admitting_work(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """P3-⑧: qualification rows outlive their window, so admission must read it.

    Publication happened while the declaration was live; the ledger row is
    immutable, so the only way an elapsed declaration can stop work is if the
    admission lookup reads the attestation window. Moving the window into the past
    therefore has to change the answer from "accepted" to the named refusal, even
    though the qualification, the approval and the publication are all untouched.
    """
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
    finally:
        _close(ledger)
    publisher = ledger.publisher(role)
    try:
        ledger.publish(publisher)
        assert _admission_answer(db) == "accepted"
        assert _job_fallback_selects_the_row(db) is True
        db.execute(
            "UPDATE speech_builtin_voice_operator_attestations SET expires_at_us=%s "
            "WHERE attestation_id=%s",
            (int(datetime.now(UTC).timestamp() * 1_000_000) - 1, ATTESTATION),
        )
        assert _admission_answer(db) == SOURCE_UNVERIFIED
        assert _job_fallback_selects_the_row(db) is False
        # The sentence names the elapsed declaration, not a generic "no evidence", and
        # the repair it states is a new revision: a published qualification's window
        # cannot be extended in place (republishing the same revision is a
        # `publication_conflict`).
        detail = cast(str, _refusal_detail(db))
        assert "elapsed window" in detail, detail
        assert "a new voice revision" in detail, detail
        # The qualification itself is still published: the refusal is about its
        # source window, not about the row having vanished.
        assert db.execute(
            "SELECT count(*) AS n FROM speech_builtin_voice_qualifications "
            "WHERE voice_id=%s AND revision=%s",
            (VOICE, REVISION),
        ).fetchone() == {"n": 1}
    finally:
        _close(ledger)


def test_a_qualification_without_a_provenance_row_is_refused(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """A qualification row with no provenance row is refused, and the refusal says so.

    This is the forgery an earlier revision of this path let through: the provenance
    row is a child table with no database-level requirement that a qualification has
    one, and writing the qualification row is a required permission of the publishing
    identity. A `DELETE` (or, equivalently, two hand-written `INSERT`s that skip the
    publish command) therefore produces a usable voice while the read surface calls it
    history. "No provenance row" only means "not written by the publish command", so it
    is refused like any other missing evidence; the read surface still labels such a
    row ``legacy_pre_0055`` for audit, and the repair is to publish the voice again.
    """
    db, schema, role = pg
    ledger = Ledger(db, schema)
    probe = ledger.probe_authorization()
    db.execute(
        "INSERT INTO speech_builtin_voice_approvals "
        "(approval_id,voice_id,revision,revision_id,binding_revision_id,source_approval_id,"
        "facts,expires_at,probe_authorization_id,result_intent) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'{}'::jsonb)",
        (
            APPROVAL,
            VOICE,
            REVISION,
            REVISION_ID,
            BINDING_REVISION_ID,
            SOURCE,
            json.dumps(_probe_facts(ledger)),
            ledger.expires_at,
            probe,
        ),
    )
    publisher = ledger.publisher(role)
    try:
        ledger.publish(publisher)
        # Controller regression: a probe-backed publication is admitted.
        assert _admission_answer(db) == "accepted"
        assert _job_fallback_selects_the_row(db) is True
        db.execute(
            "DELETE FROM speech_builtin_voice_qualification_provenance "
            "WHERE voice_id=%s AND revision=%s",
            (VOICE, REVISION),
        )
        # Without its provenance row the same published qualification is refused, and
        # the refusal names the repair (publish again) instead of "nothing matched".
        assert _admission_answer(db) == SOURCE_UNVERIFIED
        assert _job_fallback_selects_the_row(db) is False
        assert _refusal_detail(db) is not None
        absent = cast(str, _refusal_detail(db))
        assert "no qualification source recorded" in absent
        assert "a new voice revision" in absent
        entries = _report(db, schema)
        # Audit fact, not an admission permit: the row is still labelled history.
        assert entries[0]["provenance_recorded"] is False
        assert entries[0]["evidence"] == "legacy_pre_0055"
        # Same row, now carrying an unverified provenance row: still refused.
        db.execute(
            "INSERT INTO speech_builtin_voice_qualification_provenance "
            "(voice_id,revision,source_kind,evidence_kind,attestation_id,"
            "probe_authorization_id) VALUES (%s,%s,'unverified','absent',NULL,NULL)",
            (VOICE, REVISION),
        )
        assert _admission_answer(db) == SOURCE_UNVERIFIED
        assert _job_fallback_selects_the_row(db) is False
    finally:
        _close(ledger)


def test_controller_probe_publication_still_records_its_own_source(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, role = pg
    ledger = Ledger(db, schema)
    probe = ledger.probe_authorization()
    db.execute(
        "INSERT INTO speech_builtin_voice_approvals "
        "(approval_id,voice_id,revision,revision_id,binding_revision_id,source_approval_id,"
        "facts,expires_at,probe_authorization_id,result_intent) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'{}'::jsonb)",
        (
            APPROVAL,
            VOICE,
            REVISION,
            REVISION_ID,
            BINDING_REVISION_ID,
            SOURCE,
            json.dumps(_probe_facts(ledger)),
            ledger.expires_at,
            probe,
        ),
    )
    publisher = ledger.publisher(role)
    try:
        ledger.publish(publisher)
        provenance = db.execute(
            "SELECT source_kind, evidence_kind, attestation_id, probe_authorization_id FROM "
            "speech_builtin_voice_qualification_provenance WHERE voice_id=%s AND revision=%s",
            (VOICE, REVISION),
        ).fetchone()
        assert provenance == {
            "source_kind": "controller_observed",
            "evidence_kind": "probe_authorization",
            "attestation_id": None,
            "probe_authorization_id": probe,
        }
        entries = _report(db, schema)
        assert entries[0]["source_kind"] == "controller_observed"
        assert entries[0]["source_evidence"] == "probe_authorization"
        assert entries[0]["probe_authorization_id"] == probe
        assert entries[0]["provenance_recorded"] is True
        assert "attestation_id" not in entries[0]
    finally:
        _close(ledger)
    # The same approval cannot be republished as operator-sourced: a stray
    # attestation record makes publication refuse instead of trusting the shape.
    db.execute(
        "INSERT INTO speech_builtin_voice_operator_attestations "
        "(attestation_id,approval_id,voice_id,revision,revision_id,binding_revision_id,"
        "source_approval_id,instance_id,instance_generation,fencing_token,host_node_id,"
        "gateway_id,pool_id,policy_revision_id,gateway_policy_digest,template_id,image_digest,"
        "template_digest,declared_lifecycle_digest,model_id,model_revision,"
        "model_artifact_digest,observed_at_us,expires_at_us,declaration,declaration_digest,"
        "attestation_intent,declared_by) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "'{}'::jsonb,%s,'{}'::jsonb,'ops-oncall')",
        (
            ATTESTATION,
            APPROVAL,
            VOICE,
            REVISION,
            REVISION_ID,
            BINDING_REVISION_ID,
            SOURCE,
            INSTANCE,
            GENERATION,
            FENCING,
            HOST,
            GATEWAY,
            POOL,
            POLICY,
            "sha256:" + "e" * 64,
            TEMPLATE,
            ledger.image_digest,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            ledger.pin["model"]["repository"],
            ledger.pin["model"]["revision"],
            ledger.model_digest,
            int(datetime.now(UTC).timestamp() * 1_000_000) - 1_000_000,
            int(datetime.now(UTC).timestamp() * 1_000_000) + 3_600_000_000,
            "sha256:" + "4" * 64,
        ),
    )
    approval = db.execute(
        "SELECT * FROM speech_builtin_voice_approvals WHERE approval_id=%s", (APPROVAL,)
    ).fetchone()
    assert approval is not None
    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    with (
        db.cursor() as cursor,
        pytest.raises(BuiltinVoicePublicationRejectedError, match="attestation_mismatch"),
    ):
        _recorded_source(cursor, APPROVAL, approval, approval["facts"], now_us)


def test_an_operator_approval_without_its_attestation_is_refused(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """The fail-closed branch: operator facts with no attestation row never publish."""
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
        db.execute(
            "DELETE FROM speech_builtin_voice_operator_attestations WHERE approval_id=%s",
            (APPROVAL,),
        )
        publisher = ledger.publisher(role)
        with pytest.raises(BuiltinVoicePublicationRejectedError, match="attestation_missing"):
            ledger.publish(publisher)
        assert ledger.count("speech_builtin_voice_qualification_provenance") == 0
        assert ledger.count("speech_builtin_voice_qualifications") == 0
    finally:
        _close(ledger)


def test_downgrade_refuses_to_discard_attestation_history(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
    finally:
        _close(ledger)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = schema
    engine = create_engine(
        os.environ["SPEECHNEXUS_TEST_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            connection.execute(text(f'DROP TABLE IF EXISTS "{schema}".speech_asr_settlement'))
            connection.execute(
                text(f'DROP INDEX IF EXISTS "{schema}".speech_job_cancellation_fence_age_scan')
            )
            connection.execute(
                text(f'DROP INDEX IF EXISTS "{schema}".speech_job_cancellation_lease_debt_scan')
            )
            connection.execute(
                text(
                    f'UPDATE "{schema}".alembic_version '
                    "SET version_num = '0056_debt_probe_live_state_index'"
                )
            )
            connection.commit()
            with pytest.raises(RuntimeError, match="Operator attestation history"):
                command.downgrade(config, "0054_artifact_deleted_ledger")
        assert ledger.count("speech_builtin_voice_operator_attestations") == 1
    finally:
        engine.dispose()


def test_rows_published_before_the_migration_are_reported_as_recorded_history(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    db, schema, _ = pg
    ledger = Ledger(db, schema)
    db.execute(
        "INSERT INTO speech_builtin_voice_approvals VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
        (
            APPROVAL,
            VOICE,
            REVISION,
            REVISION_ID,
            BINDING_REVISION_ID,
            SOURCE,
            json.dumps(_probe_facts(ledger)),
            ledger.expires_at,
        ),
    )
    db.execute(
        "INSERT INTO speech_builtin_voice_qualifications VALUES "
        "(%s,%s,%s,%s,%s,%s::jsonb,'Vivian')",
        (
            VOICE,
            REVISION,
            REVISION_ID,
            BINDING_REVISION_ID,
            APPROVAL,
            json.dumps(_public_document(ledger, ledger.capabilities)),
        ),
    )
    entries = _report(db, schema)
    # No provenance row and no probe authorization: the source cannot be attributed,
    # so the read surface refuses to call it a controller observation.
    assert entries[0]["source_kind"] == "unverified"
    assert entries[0]["provenance_recorded"] is False
    assert entries[0]["evidence"] == "legacy_pre_0055"
    assert "source unknown" in entries[0]["note"]
    assert entries[-1]["summary"]["legacy_pre_0055"] == 1
    assert entries[-1]["summary"]["controller_observed"] == 0
    # The label records history; it does not admit work. Admission reads the same
    # row as "no source recorded" and refuses it -- `test_a_qualification_without_a_
    # provenance_row_is_refused` covers that end to end through a real publication.
    assert _job_fallback_selects_the_row(db) is False
    _close(ledger)


def test_the_read_surface_separates_both_sources_through_the_reader_identity(
    pg: tuple[psycopg.Connection[dict[str, Any]], str, str],
) -> None:
    """The audit runs as `sn_qualification_reader`, never as the schema owner.

    That identity holds only ``qualification_reader_grants``: it reads the ledger,
    the provenance labels and the evidence rows behind them, and writes nothing.
    Reading an operator-sourced and a controller-sourced qualification through it
    is what a deployment auditor actually does, so the report is proven to fit the
    least-privilege surface instead of the schema owner's.
    """
    db, schema, role = pg
    ledger = Ledger(db, schema)
    writer = ledger.writer(role)
    try:
        ledger.attest(writer)
        publisher = ledger.publisher(role)
        ledger.publish(publisher)
        # A second voice whose controller-source approval names its probe
        # authorization: the pre-0055 shape, attributed by positive evidence.
        controller_voice = "voice-controller"
        db.execute(
            "INSERT INTO speech_builtin_voice_source_approvals VALUES (%s,%s,%s,%s,%s,%s)",
            (
                "source-controller",
                controller_voice,
                "source-rev",
                ledger.digest,
                "Vivian",
                ledger.expires_at,
            ),
        )
        probe = ledger.probe_authorization(
            "probe-auth-2", source_approval_id="source-controller", voice_id=controller_voice
        )
        db.execute(
            "INSERT INTO speech_builtin_voice_approvals "
            "(approval_id,voice_id,revision,revision_id,binding_revision_id,"
            "source_approval_id,facts,expires_at,probe_authorization_id,result_intent) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'{}'::jsonb)",
            (
                "approval-controller",
                controller_voice,
                REVISION,
                "voice-rev-controller",
                BINDING_REVISION_ID,
                "source-controller",
                json.dumps(_probe_facts(ledger)),
                ledger.expires_at,
                probe,
            ),
        )
        db.execute(
            "INSERT INTO speech_builtin_voice_qualifications VALUES "
            "(%s,%s,%s,%s,%s,%s::jsonb,'Vivian')",
            (
                controller_voice,
                REVISION,
                "voice-rev-controller",
                BINDING_REVISION_ID,
                "approval-controller",
                json.dumps(_public_document(ledger, ledger.capabilities)),
            ),
        )
    finally:
        _close(ledger)

    identity = ledger.reader()
    rows = ledger.read_rows_as(identity)
    assert len(rows) == 2
    out, err = io.StringIO(), io.StringIO()
    code = provenance_main(
        ["--database-url", "postgresql://unused/synthetic", "--schema", schema],
        env={},
        connect=lambda url, name: (rows, int(rows[0]["now_us"])),
        stdout=out,
        stderr=err,
    )
    assert code == 0, err.getvalue()
    report = [json.loads(line) for line in out.getvalue().splitlines()]
    by_voice = {entry["voice_id"]: entry for entry in report[:-1]}
    operator_entry = by_voice[VOICE]
    assert operator_entry["source_kind"] == "operator_attested"
    assert operator_entry["source_evidence"] == "operator_attestation"
    assert operator_entry["attestation_id"]
    controller_entry = by_voice["voice-controller"]
    assert controller_entry["source_kind"] == "controller_observed"
    assert controller_entry["source_evidence"] == "probe_authorization"
    assert controller_entry["probe_authorization_id"] == probe
    assert report[-1]["summary"]["operator_attested"] == 1
    assert report[-1]["summary"]["controller_observed"] == 1
    assert report[-1]["summary"]["unverified"] == 0
    # The same identity cannot write: the report surface is read-only by grant,
    # not by the report choosing to issue only SELECT.
    with portable_pg_connect(
        make_conninfo(
            os.environ["SPEECHNEXUS_TEST_DATABASE_URL"], user=identity, password=PASSWORD
        ),
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "INSERT INTO speech_builtin_voice_qualification_provenance "
                "(voice_id,revision,source_kind,evidence_kind) "
                "VALUES ('voice-x','1','unverified','absent')"
            )


def _probe_facts(ledger: Ledger) -> dict[str, Any]:
    """A controller-observed approval: the same shape the probe path has always used."""
    caps = ledger.capabilities
    return {
        "source_approval_id": SOURCE,
        "source_revision": "source-rev",
        "source_digest": ledger.digest,
        "qualification_report_id": "report-1",
        "qualification_report_digest": ledger.report_digest,
        "capacity_manifest_id": "manifest-1",
        "capacity_manifest_digest": ledger.manifest_digest,
        "probe_evidence_digest": ledger.digest,
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
        "model_id": ledger.pin["model"]["repository"],
        "model_revision": ledger.pin["model"]["revision"],
        "model_artifact_digest": ledger.model_digest,
        "image_digest": ledger.image_digest,
        "generation": str(GENERATION),
        "instance_id": INSTANCE,
        "host_node_id": HOST,
        "gateway_id": GATEWAY,
        "gateway_policy_revision_id": POLICY,
        "gateway_policy_digest": "sha256:" + "e" * 64,
        "binding_id": BINDING,
        "binding_revision": BINDING_REVISION,
        "binding_revision_id": BINDING_REVISION_ID,
        "native_speaker_id": "Vivian",
        "checked_at_us": 1_767_225_600_000_000,
        "expires_at_us": int(ledger.expires_at.timestamp()) * 1_000_000,
        "public_document": _public_document(ledger, caps),
        "qualified_capabilities": caps,
    }


def _public_document(ledger: Ledger, caps: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_type": "voice_revision",
        "voice_id": VOICE,
        "revision": REVISION,
        "object": "nexus.voice_revision",
        "status": "published",
        "display_name": "Vivian",
        "source": {
            "type": "builtin",
            "draft_id": None,
            "draft_revision": None,
            "source_asset_digest": None,
        },
        "model_binding": {"id": BINDING, "revision": BINDING_REVISION},
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
