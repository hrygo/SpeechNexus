"""Commit qualification facts obtained by the fixed probe execution driver."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.qualification_probe_artifacts import VerifiedLocalArtifacts
from speechnexus_backend.qualification_probe_execution import VerifiedProbeExecution
from speechnexus_backend.speech.domain.builtin_voice_qualification import BuiltinVoiceFacts
from speechnexus_backend.speech.domain.publications import publication_digest
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import revision_is_disabled
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    find_binding_revision_document,
    find_evidence,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import find_publication
from speechnexus_backend.speech.interfaces.binding_release_evidence import (
    verify_binding_release_evidence,
)


class ProbeApprovalError(ValueError):
    """Original probe evidence cannot support a qualification approval."""


def result_request(
    *,
    approval_id: str,
    authorization_id: str,
    artifact_pin: bytes,
    manifest: bytes,
    report: bytes,
    scenario_evidence: bytes,
    original_identity: dict[str, str],
) -> dict[str, str]:
    if set(original_identity) != {
        "operation_id",
        "attempt_id",
        "admission_request_digest",
        "business_request_digest",
        "gateway_request_digest",
        "intent_file_sha256",
        "recipe_file_sha256",
    }:
        raise ProbeApprovalError("probe_approval_unverified")
    return {
        "approval_id": approval_id,
        "authorization_id": authorization_id,
        **original_identity,
        **{
            name + "_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()
            for name, raw in (
                ("artifact_pin", artifact_pin),
                ("manifest", manifest),
                ("report", report),
                ("scenario_evidence", scenario_evidence),
            )
        },
    }


def find_probe_approval(store: PostgresPublications, request: dict[str, str]) -> bool:
    """Recover an exact committed result without calling the gateway or model."""

    def operation(cursor: Any) -> bool:
        cursor.execute(
            "SELECT p.result_intent,a.intent,current_database() AS db,current_schema() AS schema, "
            "i.installation_id::text AS installation_id FROM speech_builtin_voice_approvals p "
            "LEFT JOIN speech_builtin_voice_probe_authorizations a "
            "ON a.authorization_id=p.probe_authorization_id "
            "CROSS JOIN speech_publication_installation i "
            "WHERE p.approval_id=%s AND i.singleton IS TRUE",
            (request["approval_id"],),
        )
        rows = cursor.fetchall()
        if not rows:
            return False
        row = rows[0]
        auth = row["intent"]
        if (
            len(rows) != 1
            or not isinstance(auth, dict)
            or not isinstance(row["result_intent"], dict)
            or (row["db"], row["schema"], row["installation_id"])
            != (auth["database"], auth["schema"], auth["installation_id"])
            or any(row["result_intent"].get(k) != v for k, v in request.items())
        ):
            raise ProbeApprovalError("probe_approval_conflict")
        return True

    return store._run(operation, read_only=True, preserve=(ProbeApprovalError,))


def _commit_verified_probe(
    store: PostgresPublications,
    *,
    authorization_id: str,
    approval_id: str,
    probe: VerifiedProbeExecution,
    artifacts: VerifiedLocalArtifacts,
    artifact_pin: bytes,
    manifest: bytes,
    report: bytes,
    scenario_evidence: bytes,
    original_identity: dict[str, str],
) -> str:
    """Private composition step; input is from the in-process fixed verifiers only."""
    evidence_digest = publication_digest(
        {
            "probe": asdict(probe),
            "artifact_pin_sha256": hashlib.sha256(artifact_pin).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "report_sha256": hashlib.sha256(report).hexdigest(),
            "scenario_sha256": hashlib.sha256(scenario_evidence).hexdigest(),
        }
    )
    request = result_request(
        approval_id=approval_id,
        authorization_id=authorization_id,
        artifact_pin=artifact_pin,
        manifest=manifest,
        report=report,
        scenario_evidence=scenario_evidence,
        original_identity=original_identity,
    )

    def operation(cursor: Any) -> str:
        cursor.execute(
            "SELECT a.intent FROM speech_builtin_voice_probe_authorizations a "
            "JOIN speech_builtin_voice_probe_admissions p USING (authorization_id) "
            "WHERE a.authorization_id=%s",
            (authorization_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise ProbeApprovalError("probe_approval_unverified")
        auth = rows[0]["intent"]
        for key in (
            "operation_id",
            "attempt_id",
            "admission_request_digest",
            "business_request_digest",
            "gateway_request_digest",
        ):
            if original_identity.get(key) != auth[key]:
                raise ProbeApprovalError("probe_approval_conflict")
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:'||current_schema()||':binding:'||%s,0))",
            (auth["binding_id"],),
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:'||current_schema()||':voice:'||%s,0))",
            (auth["voice_id"],),
        )
        cursor.execute(
            "SELECT current_database() AS db,current_schema() AS schema,"
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        )
        target = cursor.fetchall()
        if len(target) != 1 or (
            target[0]["db"],
            target[0]["schema"],
            target[0]["installation_id"],
        ) != (auth["database"], auth["schema"], auth["installation_id"]):
            raise ProbeApprovalError("probe_approval_unverified")
        cursor.execute(
            "SELECT voice_id,revision,revision_id,binding_revision_id,source_approval_id,facts,"
            "probe_authorization_id,result_intent "
            "FROM speech_builtin_voice_approvals WHERE approval_id=%s",
            (approval_id,),
        )
        existing = cursor.fetchall()
        if existing:
            old = existing[0]
            if len(existing) != 1 or (
                old["voice_id"],
                old["revision"],
                old["revision_id"],
                old["binding_revision_id"],
                old["source_approval_id"],
                old["facts"].get("probe_evidence_digest"),
                old["probe_authorization_id"],
            ) != (
                auth["voice_id"],
                auth["voice_revision"],
                auth["voice_revision_id"],
                auth["binding_revision_id"],
                auth["source_approval_id"],
                evidence_digest,
                authorization_id,
            ):
                raise ProbeApprovalError("probe_approval_conflict")
            if any(old["result_intent"].get(k) != v for k, v in request.items()):
                raise ProbeApprovalError("probe_approval_conflict")
            return "replayed"
        cursor.execute(
            "SELECT a.intent,s.source_revision,s.source_digest,s.expires_at "
            "FROM speech_builtin_voice_probe_authorizations a "
            "JOIN speech_builtin_voice_source_approvals s "
            "ON s.source_approval_id=a.source_approval_id AND s.voice_id=a.voice_id "
            "WHERE a.authorization_id=%s AND s.expires_at>clock_timestamp() "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_source_revocations r "
            "WHERE r.source_approval_id=s.source_approval_id)",
            (authorization_id,),
        )
        source = cursor.fetchall()
        if len(source) != 1 or source[0]["intent"] != auth:
            raise ProbeApprovalError("probe_approval_unverified")
        cursor.execute(
            "SELECT s.submit_intent_id,s.execution_state,s.generated_pcm_samples,"
            "s.voice_revision_id,e.evidence_digest,e.evidence_kind,e.execution_stopped,"
            "e.resources_released,e.observed_at_us,o.request_document "
            "FROM speech_builtin_voice_probe_admissions p "
            "JOIN admission_operations o ON o.operation_id=p.operation_id "
            "JOIN admission_leases l ON l.operation_id=p.operation_id "
            "AND l.attempt_id=p.attempt_id "
            "JOIN submit_intents s ON s.lease_id=l.lease_id "
            "JOIN execution_stop_evidence e ON e.submit_intent_id=s.submit_intent_id "
            "WHERE p.authorization_id=%s AND s.submit_intent_id=%s",
            (authorization_id, probe.submit_intent_id),
        )
        terminals = cursor.fetchall()
        if len(terminals) != 1:
            raise ProbeApprovalError("probe_approval_unverified")
        terminal = terminals[0]
        if (
            terminal["execution_state"] != "succeeded"
            or terminal["evidence_kind"] != "engine_terminal_response"
            or terminal["execution_stopped"] != 1
            or terminal["resources_released"] != 1
            or terminal["voice_revision_id"] != auth["voice_revision_id"]
            or terminal["generated_pcm_samples"] != probe.generated_samples
            or terminal["evidence_digest"] != probe.stop_evidence_digest
            or terminal["observed_at_us"] != probe.terminal_observed_at_us
        ):
            raise ProbeApprovalError("probe_approval_unverified")
        if revision_is_disabled(cursor, auth["binding_id"], auth["binding_revision_id"]):
            raise ProbeApprovalError("probe_approval_unverified")
        cursor.execute(
            "SELECT revision FROM speech_binding_revisions WHERE binding_id=%s AND revision_id=%s",
            (auth["binding_id"], auth["binding_revision_id"]),
        )
        revisions = cursor.fetchall()
        if len(revisions) != 1:
            raise ProbeApprovalError("probe_approval_unverified")
        rev = int(revisions[0]["revision"])
        publication = find_publication(cursor, "binding", auth["binding_id"], rev)
        evidence = find_evidence(cursor, auth["binding_id"], rev)
        definition = find_binding_revision_document(cursor, auth["binding_id"], rev)
        if publication is None or evidence is None or definition is None:
            raise ProbeApprovalError("probe_approval_unverified")
        cursor.execute("SELECT clock_timestamp() AS now")
        now = cursor.fetchall()[0]["now"]
        release = verify_binding_release_evidence(
            publication=publication,
            recorded_evidence=evidence,
            binding_definition=definition[1].encode(),
            artifact_pin=artifact_pin,
            manifest=manifest,
            report=report,
            scenario_evidence=scenario_evidence,
            now=now,
        )
        if (
            release.expires_at is None
            or (
                release.manifest_id,
                release.report_id,
                release.manifest_digest,
                release.report_digest,
            )
            != (
                auth["manifest_id"],
                auth["report_id"],
                auth["manifest_digest"],
                auth["report_digest"],
            )
            or artifacts.model_artifact_digest != auth["model_artifact_digest"]
            or artifacts.expected_image_digest != auth["image_digest"]
            or not any(
                p.model_digest == auth["model_artifact_digest"]
                and p.image_digest == auth["image_digest"]
                for p in release.stage_pins
            )
        ):
            raise ProbeApprovalError("probe_approval_unverified")
        expiry = min(source[0]["expires_at"], release.expires_at, now + timedelta(days=1))
        if expiry <= now:
            raise ProbeApprovalError("probe_approval_unverified")
        command = json.loads(terminal["request_document"])["gateway_request"]["command"]
        if (
            command["text"] != "SpeechNexus qualification probe."
            or command["parameters"]["language"] != "en"
        ):
            raise ProbeApprovalError("probe_approval_unverified")
        caps: dict[str, Any] = {
            "languages": ["en"],
            "sample_rates_hz": [24000],
            "audio_formats": ["wav"],
            "modes": ["complete"],
            "max_input_codepoints": len(command["text"]),
        }
        domains = json.loads(definition[1])["parameter_domains"]["tts"]
        for public_key, domain_key in (
            ("languages", "languages"),
            ("audio_formats", "audio_formats"),
            ("sample_rates_hz", "sample_rates_hz"),
            ("modes", "audio_delivery_modes"),
        ):
            if not set(caps[public_key]) <= set(domains[domain_key]):
                raise ProbeApprovalError("probe_approval_unverified")
        if caps["max_input_codepoints"] > domains["max_input_codepoints"]:
            raise ProbeApprovalError("probe_approval_unverified")
        ts = now.isoformat().replace("+00:00", "Z")
        document = {
            "resource_type": "voice_revision",
            "voice_id": auth["voice_id"],
            "revision": auth["voice_revision"],
            "object": "nexus.voice_revision",
            "display_name": "Vivian",
            "capabilities": caps,
            "validation": {
                "validation_id": auth["report_id"],
                "status": "passed",
                "checked_at": ts,
            },
            "published_at": ts,
            "retired_at": None,
            "disabled_reason": None,
            "source": {
                "type": "builtin",
                "draft_id": None,
                "draft_revision": None,
                "source_asset_digest": None,
            },
            "status": "published",
            "model_binding": {"id": auth["binding_id"], "revision": str(rev)},
        }
        checks = [
            "source_approved",
            "artifact_integrity",
            "engine_identity",
            "voice_speaker_binding",
            "wav_complete",
            "output_bounds",
            "native_terminal",
            "stop_release",
        ]
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        checked_delta, expiry_delta = now - epoch, expiry - epoch
        checked_us = (checked_delta.days * 86400 + checked_delta.seconds) * 1_000_000
        checked_us += checked_delta.microseconds
        expiry_us = (expiry_delta.days * 86400 + expiry_delta.seconds) * 1_000_000
        expiry_us += expiry_delta.microseconds
        facts = {
            "source_approval_id": auth["source_approval_id"],
            "source_revision": source[0]["source_revision"],
            "source_digest": source[0]["source_digest"],
            "qualification_report_id": auth["report_id"],
            "qualification_report_digest": auth["report_digest"],
            "capacity_manifest_id": auth["manifest_id"],
            "capacity_manifest_digest": auth["manifest_digest"],
            "probe_evidence_digest": evidence_digest,
            "verifier_id": "builtin-customvoice-v1",
            "verifier_version": "1",
            "required_checks": checks,
            "model_id": artifacts.model_id,
            "model_revision": artifacts.model_revision,
            "model_artifact_digest": artifacts.model_artifact_digest,
            "image_digest": auth["image_digest"],
            "generation": str(auth["instance_generation"]),
            "instance_id": auth["instance_id"],
            "host_node_id": auth["host_node_id"],
            "gateway_id": auth["gateway_id"],
            "gateway_policy_revision_id": auth["policy_revision_id"],
            "gateway_policy_digest": auth["gateway_policy_digest"],
            "binding_id": auth["binding_id"],
            "binding_revision": str(rev),
            "binding_revision_id": auth["binding_revision_id"],
            "native_speaker_id": "Vivian",
            "checked_at_us": checked_us,
            "expires_at_us": expiry_us,
            "public_document": document,
            "qualified_capabilities": caps,
        }
        BuiltinVoiceFacts(facts)
        validate_response("getVoiceRevision", 200, {"body": document})
        cursor.execute(
            "SELECT facts FROM speech_builtin_voice_approvals WHERE approval_id=%s", (approval_id,)
        )
        existing = cursor.fetchall()
        if existing:
            if len(existing) != 1 or existing[0]["facts"] != facts:
                raise ProbeApprovalError("probe_approval_conflict")
            return "replayed"
        result_intent = request | {"probe_evidence_digest": evidence_digest}
        cursor.execute(
            "INSERT INTO speech_builtin_voice_approvals "
            "(approval_id,voice_id,revision,revision_id,binding_revision_id,"
            "source_approval_id,facts,expires_at,probe_authorization_id,result_intent) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                approval_id,
                auth["voice_id"],
                auth["voice_revision"],
                auth["voice_revision_id"],
                auth["binding_revision_id"],
                auth["source_approval_id"],
                Jsonb(facts),
                expiry,
                authorization_id,
                Jsonb(result_intent),
            ),
        )
        return "committed"

    return store._run(operation, preserve=(ProbeApprovalError,))
