"""Record one operator-attested builtin voice qualification from declared evidence.

The caller is a separate, minimally granted operator identity. Nothing here
produces runtime evidence: the transaction's write targets are exactly
``speech_builtin_voice_approvals`` (one row, probe columns NULL) and
``speech_builtin_voice_operator_attestations``. Budget release, lease settlement,
``execution_cleanup_pending`` and instance lifecycle convergence keep reading the
runtime tables this module never touches.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from speechnexus_backend.speech.domain.operator_attestation import (
    AttestationRequest,
    OperatorAttestationError,
    RegistrationFacts,
    TrustDeclaration,
    build_operator_facts,
    declaration_digest,
    require_capabilities,
    require_gateway_evidence,
    require_lifecycle_identity,
    require_model_artifact,
    require_registration,
    require_template_identity,
)
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import (
    lock_binding,
    revision_is_disabled,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    find_binding_revision_document,
    find_evidence,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import find_publication
from speechnexus_backend.speech.interfaces.binding_release_evidence import (
    verify_binding_release_evidence,
)

_LEDGER_RELATIONS = (
    "speech_builtin_voice_source_approvals",
    "speech_builtin_voice_source_revocations",
    "speech_builtin_voice_approvals",
    "speech_builtin_voice_revocations",
    "speech_builtin_voice_qualifications",
    "speech_builtin_voice_publish_intents",
    "speech_builtin_voice_probe_authorizations",
    "speech_builtin_voice_probe_admissions",
    "speech_builtin_voice_operator_attestations",
    "speech_builtin_voice_qualification_provenance",
    "speech_publications",
    "speech_publication_heads",
    "speech_publication_retirements",
)
_RELATIONS = ",".join("'" + name + "'" for name in _LEDGER_RELATIONS)

# A write identity may append its two rows and read what it needs; it must not be
# able to rewrite, delete or truncate any part of the qualification ledger later.
_FORBIDDEN_IDENTITY = (
    "SELECT SESSION_USER<>CURRENT_USER OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole "
    "OR r.rolreplication OR r.rolbypassrls "
    "OR pg_catalog.pg_has_role(SESSION_USER,d.datdba,'MEMBER') "
    "OR pg_catalog.pg_has_role(SESSION_USER,n.nspowner,'MEMBER') "
    "OR pg_catalog.has_schema_privilege(SESSION_USER,n.oid,'CREATE') "
    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace=n.oid "
    f"AND c.relname IN ({_RELATIONS}) AND ("
    "pg_catalog.has_table_privilege(SESSION_USER,c.oid,"
    "'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
    "pg_catalog.has_any_column_privilege(SESSION_USER,c.oid,'UPDATE,REFERENCES'))) "
    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable WHERE "
    "pg_catalog.pg_has_role(SESSION_USER,reachable.oid,'MEMBER') AND ("
    "pg_catalog.left(reachable.rolname,3)='pg_' OR reachable.rolsuper "
    "OR reachable.rolcreatedb OR reachable.rolcreaterole OR reachable.rolreplication "
    "OR reachable.rolbypassrls OR pg_catalog.has_schema_privilege(reachable.oid,n.oid,'CREATE') "
    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace=n.oid "
    f"AND c.relname IN ({_RELATIONS}) AND pg_catalog.has_table_privilege("
    "reachable.oid,c.oid,'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')))) "
    "AS forbidden FROM pg_catalog.pg_roles r "
    "JOIN pg_catalog.pg_database d ON d.datname=current_database() "
    "JOIN pg_catalog.pg_namespace n ON n.nspname=current_schema() "
    "WHERE r.rolname=SESSION_USER"
)

_REGISTRATION = (
    "SELECT i.instance_id,i.instance_generation,i.instance_state,i.host_node_id,"
    "i.gateway_id,i.pool_id,i.binding_revision_id,i.manifest_id,i.approved_template_id,"
    "i.config_digest,i.last_fencing_token,h.current_generation AS head_generation,"
    "p.fencing_token AS owner_fencing_token,p.owner_expires_at_us,p.owner_state,"
    "w.policy_revision "
    "FROM engine_instances i "
    "JOIN runtime_instance_heads h ON h.instance_id=i.instance_id "
    "AND h.current_generation=i.instance_generation "
    "JOIN resource_pools p ON p.pool_id=i.pool_id "
    "JOIN workspace_quotas w ON w.workspace_id=%s "
    "WHERE i.instance_id=%s AND i.instance_generation=%s"
)


def _registration_facts(row: dict[str, Any]) -> RegistrationFacts:
    return RegistrationFacts(
        instance_id=row["instance_id"],
        instance_generation=int(row["instance_generation"]),
        instance_state=row["instance_state"],
        host_node_id=row["host_node_id"],
        gateway_id=row["gateway_id"],
        pool_id=row["pool_id"],
        binding_revision_id=row["binding_revision_id"],
        manifest_id=row["manifest_id"],
        approved_template_id=row["approved_template_id"],
        config_digest=row["config_digest"],
        last_fencing_token=int(row["last_fencing_token"]),
        head_generation=int(row["head_generation"]),
        owner_fencing_token=int(row["owner_fencing_token"]),
        owner_expires_at_us=int(row["owner_expires_at_us"]),
        policy_revision_id=row["policy_revision"],
    )


def _stage_pins(verified: Any) -> tuple[tuple[str, str], ...]:
    return tuple((pin.model_digest, pin.image_digest) for pin in verified.stage_pins)


def parse_operator_documents(
    *, declaration_document: bytes, request_document: bytes
) -> tuple[TrustDeclaration, AttestationRequest]:
    """Parse both private originals; a malformed one never reaches a transaction."""
    try:
        declaration_raw = json.loads(declaration_document)
        request_raw = json.loads(request_document)
    except ValueError, TypeError, UnicodeDecodeError:
        raise OperatorAttestationError("attestation_request_invalid") from None
    declaration = TrustDeclaration.parse(declaration_raw)
    request = AttestationRequest.parse(request_raw)
    if (request.database, request.schema, request.installation_id) != (
        declaration.database,
        declaration.schema,
        declaration.installation_id,
    ):
        raise OperatorAttestationError("trust_declaration_target_mismatch")
    declaration.require_instance(request.instance_id)
    return declaration, request


def record_operator_attestation(
    store: PostgresPublications,
    *,
    declaration: TrustDeclaration,
    request: AttestationRequest,
    artifact_pin: bytes,
    manifest: bytes,
    report: bytes,
    scenario_evidence: bytes,
    gateway_evidence: dict[str, Any],
    workspace_id: str,
) -> dict[str, Any]:
    """Insert the attested approval and its declaration record in one transaction."""
    if (request.database, request.schema, request.installation_id) != (
        declaration.database,
        declaration.schema,
        declaration.installation_id,
    ):
        raise OperatorAttestationError("trust_declaration_target_mismatch")
    declaration.require_instance(request.instance_id)
    evidence_digest = declaration_digest(declaration, request)

    def operation(cursor: Any) -> dict[str, Any]:
        cursor.execute(_FORBIDDEN_IDENTITY)
        identities = cursor.fetchall()
        if len(identities) != 1 or identities[0]["forbidden"]:
            raise OperatorAttestationError("writer_identity_forbidden")
        lock_binding(cursor, request.binding_id)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:'||current_schema()||':voice:'||%s,0))",
            (request.voice_id,),
        )
        cursor.execute(
            "SELECT current_database() AS database,current_schema() AS schema,"
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        )
        targets = cursor.fetchall()
        if len(targets) != 1 or (
            targets[0]["database"],
            targets[0]["schema"],
            targets[0]["installation_id"],
        ) != (request.database, request.schema, request.installation_id):
            raise OperatorAttestationError("trust_declaration_target_mismatch")
        existing = _replay(cursor, request, evidence_digest)
        if existing is not None:
            return existing
        cursor.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000000)::bigint AS now_us"
        )
        now_us = int(cursor.fetchall()[0]["now_us"])
        declaration.require_live(now_us)
        cursor.execute(
            "SELECT s.source_revision,s.source_digest,"
            "floor(extract(epoch FROM s.expires_at)*1000000)::bigint AS expires_us "
            "FROM speech_builtin_voice_source_approvals s "
            "WHERE s.source_approval_id=%s AND s.voice_id=%s AND s.native_speaker_id='Vivian' "
            "AND s.expires_at>clock_timestamp() "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_source_revocations r "
            "WHERE r.source_approval_id=s.source_approval_id)",
            (request.source_approval_id, request.voice_id),
        )
        sources = cursor.fetchall()
        if len(sources) != 1:
            raise OperatorAttestationError("source_approval_unavailable")
        source = sources[0]
        cursor.execute(
            "SELECT revision FROM speech_binding_revisions WHERE binding_id=%s AND revision_id=%s",
            (request.binding_id, request.binding_revision_id),
        )
        revisions = cursor.fetchall()
        if len(revisions) != 1:
            raise OperatorAttestationError("binding_revision_unavailable")
        revision = int(revisions[0]["revision"])
        if revision_is_disabled(cursor, request.binding_id, request.binding_revision_id):
            raise OperatorAttestationError("binding_revision_unavailable")
        publication = find_publication(cursor, "binding", request.binding_id, revision)
        recorded = find_evidence(cursor, request.binding_id, revision)
        definition = find_binding_revision_document(cursor, request.binding_id, revision)
        if publication is None or recorded is None or definition is None:
            raise OperatorAttestationError("binding_revision_unavailable")
        try:
            verified = verify_binding_release_evidence(
                publication=publication,
                recorded_evidence=recorded,
                binding_definition=definition[1].encode(),
                artifact_pin=artifact_pin,
                manifest=manifest,
                report=report,
                scenario_evidence=scenario_evidence,
                now=datetime.now(UTC),
            )
        except Exception:
            raise OperatorAttestationError("binding_release_evidence_mismatch") from None
        if verified.expires_at is None:
            raise OperatorAttestationError("binding_release_evidence_mismatch")
        release_expires_us = int(verified.expires_at.timestamp() * 1_000_000)
        cursor.execute(
            _REGISTRATION,
            (workspace_id, request.instance_id, request.instance_generation),
        )
        registrations = cursor.fetchall()
        if len(registrations) != 1:
            cursor.execute(
                "SELECT current_generation FROM runtime_instance_heads WHERE instance_id=%s",
                (request.instance_id,),
            )
            if cursor.fetchall():
                raise OperatorAttestationError("instance_generation_mismatch")
            raise OperatorAttestationError("instance_not_registered")
        registration = _registration_facts(registrations[0])
        require_registration(request, facts=registration, now_us=now_us)
        pins = _stage_pins(verified)
        expected_image = next(
            (image for model, image in pins if image == request.image_digest),
            None,
        )
        if expected_image is None:
            raise OperatorAttestationError("image_digest_mismatch")
        require_template_identity(
            request,
            approved_template_id=registration.approved_template_id,
            image_digest=expected_image,
        )
        require_lifecycle_identity(request, declared_by=declaration.declared_by)
        pin_document = json.loads(artifact_pin)
        require_model_artifact(
            request,
            pin_model=pin_document["model"],
            stage_pins=pins,
        )
        domains = json.loads(definition[1])["parameter_domains"]["tts"]
        capabilities = require_capabilities(request.capabilities, domains)
        gateway_policy_digest = require_gateway_evidence(
            gateway_evidence,
            expected={
                "policy_revision_id": registration.policy_revision_id,
                "voice_id": request.voice_id,
                "voice_revision": request.revision,
                "voice_revision_id": request.revision_id,
                "native_speaker_id": "Vivian",
                "binding_revision_id": request.binding_revision_id,
                "gateway_id": registration.gateway_id,
                "instance_id": request.instance_id,
                "instance_generation": request.instance_generation,
                "node_id": registration.host_node_id,
            },
            now_us=now_us,
        )
        request.window.require_live(
            now_us=now_us,
            max_validity_seconds=declaration.max_validity_seconds,
            source_expires_us=int(source["expires_us"]),
            release_expires_us=release_expires_us,
        )
        facts = build_operator_facts(
            request=request,
            declared_by=declaration.declared_by,
            declaration_digest_value=evidence_digest,
            source_revision=source["source_revision"],
            source_digest=source["source_digest"],
            report_id=verified.report_id,
            report_digest=verified.report_digest,
            manifest_id=verified.manifest_id,
            manifest_digest=verified.manifest_digest,
            binding_id=request.binding_id,
            binding_revision=str(revision),
            host_node_id=registration.host_node_id,
            gateway_id=registration.gateway_id,
            policy_revision_id=registration.policy_revision_id,
            gateway_policy_digest=gateway_policy_digest,
            capabilities=capabilities,
        )
        if facts["capacity_manifest_id"] != registration.manifest_id:
            raise OperatorAttestationError("binding_registration_mismatch")
        expires_at = datetime.fromtimestamp(request.window.expires_at_us / 1_000_000, tz=UTC)
        cursor.execute(
            "INSERT INTO speech_builtin_voice_approvals "
            "(approval_id,voice_id,revision,revision_id,binding_revision_id,"
            "source_approval_id,facts,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                request.approval_id,
                request.voice_id,
                request.revision,
                request.revision_id,
                request.binding_revision_id,
                request.source_approval_id,
                json.dumps(facts),
                expires_at,
            ),
        )
        cursor.execute(
            "INSERT INTO speech_builtin_voice_operator_attestations "
            "(attestation_id,approval_id,voice_id,revision,revision_id,binding_revision_id,"
            "source_approval_id,instance_id,instance_generation,fencing_token,host_node_id,"
            "gateway_id,pool_id,policy_revision_id,gateway_policy_digest,template_id,image_digest,"
            "template_digest,declared_lifecycle_digest,model_id,model_revision,"
            "model_artifact_digest,observed_at_us,expires_at_us,declaration,declaration_digest,"
            "attestation_intent,declared_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s::jsonb,%s,%s::jsonb,%s)",
            (
                request.attestation_id,
                request.approval_id,
                request.voice_id,
                request.revision,
                request.revision_id,
                request.binding_revision_id,
                request.source_approval_id,
                request.instance_id,
                request.instance_generation,
                request.fencing_token,
                registration.host_node_id,
                registration.gateway_id,
                registration.pool_id,
                registration.policy_revision_id,
                gateway_policy_digest,
                request.template_id,
                request.image_digest,
                request.template_digest,
                request.lifecycle_digest,
                request.model_id,
                request.model_revision,
                request.model_artifact_digest,
                request.window.observed_at_us,
                request.window.expires_at_us,
                json.dumps(declaration.document),
                evidence_digest,
                json.dumps(request.document),
                declaration.declared_by,
            ),
        )
        return {
            "outcome": "committed",
            "attestation_id": request.attestation_id,
            "approval_id": request.approval_id,
            "voice_id": request.voice_id,
            "revision": request.revision,
            "declaration_digest": evidence_digest,
            "expires_at_us": request.window.expires_at_us,
        }

    return store._run(operation, preserve=(OperatorAttestationError,))


def _replay(
    cursor: Any, request: AttestationRequest, evidence_digest: str
) -> dict[str, Any] | None:
    """Return the original result for the identical attestation, or refuse a conflict."""
    cursor.execute(
        "SELECT attestation_id,approval_id,declaration_digest,"
        "floor(extract(epoch FROM clock_timestamp())*1000000)::bigint AS now_us "
        "FROM speech_builtin_voice_operator_attestations WHERE attestation_id=%s",
        (request.attestation_id,),
    )
    rows = cursor.fetchall()
    if rows:
        row = rows[0]
        if (row["attestation_id"], row["approval_id"], row["declaration_digest"]) != (
            request.attestation_id,
            request.approval_id,
            evidence_digest,
        ):
            raise OperatorAttestationError("attestation_conflict")
        return {
            "outcome": "replayed",
            "attestation_id": request.attestation_id,
            "approval_id": request.approval_id,
            "voice_id": request.voice_id,
            "revision": request.revision,
            "declaration_digest": evidence_digest,
            "expires_at_us": request.window.expires_at_us,
        }
    cursor.execute(
        "SELECT attestation_id FROM speech_builtin_voice_operator_attestations "
        "WHERE approval_id=%s",
        (request.approval_id,),
    )
    if cursor.fetchall():
        raise OperatorAttestationError("attestation_conflict")
    cursor.execute(
        "SELECT approval_id FROM speech_builtin_voice_approvals WHERE approval_id=%s",
        (request.approval_id,),
    )
    if cursor.fetchall():
        raise OperatorAttestationError("attestation_conflict")
    return None
