"""Transactional promotion of an independently approved builtin voice."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.speech.domain.builtin_voice_qualification import BuiltinVoiceFacts
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    find_binding_revision_document,
    find_evidence,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import (
    append_publication,
    find_publication,
)
from speechnexus_backend.speech.interfaces.binding_release_evidence import (
    verify_binding_release_evidence,
)


class BuiltinVoicePublicationRejectedError(Exception):
    """Missing approval, stale target, or conflicting original intent."""


def publish_builtin_voice(
    store: PostgresPublications,
    *,
    key_digest: str,
    approval_id: str,
    database: str,
    schema: str,
    installation_id: str,
    expected_head: str | None,
    artifact_pin: bytes,
    manifest: bytes,
    report: bytes,
    scenario_evidence: bytes,
) -> dict[str, Any]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", key_digest):
        raise ValueError("invalid intent digest")
    request = {
        "approval_id": approval_id,
        "database": database,
        "schema": schema,
        "installation_id": installation_id,
        "expected_head": expected_head,
        "artifact_pin_digest": "sha256:" + hashlib.sha256(artifact_pin).hexdigest(),
        "manifest_digest": "sha256:" + hashlib.sha256(manifest).hexdigest(),
        "report_digest": "sha256:" + hashlib.sha256(report).hexdigest(),
        "scenario_evidence_digest": "sha256:" + hashlib.sha256(scenario_evidence).hexdigest(),
    }

    def operation(cursor: Any) -> dict[str, Any]:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:builtin-voice-intent:' || current_schema() || ':' || %s, 0))",
            (key_digest,),
        )
        cursor.execute(
            "SELECT intent FROM speech_builtin_voice_publish_intents WHERE key_digest=%s",
            (key_digest,),
        )
        existing = cursor.fetchall()
        if existing:
            saved = existing[0]["intent"]
            if saved["request"] != request:
                raise BuiltinVoicePublicationRejectedError("idempotency_conflict")
            return {
                "status": "replay",
                "voice_id": saved["voice_id"],
                "revision": saved["revision"],
            }
        cursor.execute(
            "SELECT current_database() AS db, current_schema() AS schema, "
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        )
        targets = cursor.fetchall()
        if len(targets) != 1 or (
            targets[0]["db"],
            targets[0]["schema"],
            targets[0]["installation_id"],
        ) != (database, schema, installation_id):
            raise BuiltinVoicePublicationRejectedError("target_changed")
        cursor.execute(
            "SELECT a.*, s.source_revision, s.source_digest, s.native_speaker_id AS source_speaker "
            "FROM speech_builtin_voice_approvals a JOIN speech_builtin_voice_source_approvals s "
            "ON s.source_approval_id=a.source_approval_id WHERE a.approval_id=%s "
            "AND a.expires_at > clock_timestamp() AND s.expires_at > clock_timestamp() "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_revocations r "
            "WHERE r.approval_id=a.approval_id) "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_source_revocations r "
            "WHERE r.source_approval_id=s.source_approval_id)",
            (approval_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise BuiltinVoicePublicationRejectedError("not_qualified")
        a = rows[0]
        f = a["facts"]
        BuiltinVoiceFacts(f)
        validate_response("getVoiceRevision", 200, {"body": f["public_document"]})
        d = f["public_document"]
        if (
            (
                f["source_approval_id"],
                f["source_revision"],
                f["source_digest"],
                f["native_speaker_id"],
            )
            != (
                a["source_approval_id"],
                a["source_revision"],
                a["source_digest"],
                a["source_speaker"],
            )
            or f["binding_revision_id"] != a["binding_revision_id"]
            or (d["voice_id"], d["revision"]) != (a["voice_id"], a["revision"])
        ):
            raise BuiltinVoicePublicationRejectedError("evidence_mismatch")
        cursor.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000000)::bigint AS now_us"
        )
        if f["checked_at_us"] > cursor.fetchall()[0]["now_us"]:
            raise BuiltinVoicePublicationRejectedError("qualification_from_future")
        # Match the actual independently stored approval expiry, not caller JSON.
        expiry_delta = a["expires_at"] - datetime(1970, 1, 1, tzinfo=UTC)
        expiry_us = (
            expiry_delta.days * 86400 + expiry_delta.seconds
        ) * 1_000_000 + expiry_delta.microseconds
        if expiry_us != f["expires_at_us"]:
            raise BuiltinVoicePublicationRejectedError("expiry_mismatch")
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':binding:' || %s, 0))",
            (f["binding_id"],),
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':voice:' || %s, 0))",
            (a["voice_id"],),
        )
        cursor.execute(
            "SELECT 1 FROM speech_publication_heads WHERE kind='binding' "
            "AND object_id=%s AND revision=%s AND revision_id=%s",
            (f["binding_id"], f["binding_revision"], f["binding_revision_id"]),
        )
        if len(cursor.fetchall()) != 1:
            raise BuiltinVoicePublicationRejectedError("binding_changed")
        binding = find_publication(cursor, "binding", f["binding_id"], int(f["binding_revision"]))
        evidence = find_evidence(cursor, f["binding_id"], int(f["binding_revision"]))
        definition = find_binding_revision_document(
            cursor, f["binding_id"], int(f["binding_revision"])
        )
        if (
            binding is None
            or evidence is None
            or definition is None
            or definition[0] != f["binding_revision_id"]
        ):
            raise BuiltinVoicePublicationRejectedError("binding_evidence_missing")
        domains = json.loads(definition[1])["parameter_domains"]["tts"]
        caps, qualified = d["capabilities"], f["qualified_capabilities"]
        for public_key, domain_key in (
            ("languages", "languages"),
            ("audio_formats", "audio_formats"),
            ("sample_rates_hz", "sample_rates_hz"),
            ("modes", "audio_delivery_modes"),
        ):
            if not set(caps[public_key]) <= set(domains[domain_key]) or not set(
                caps[public_key]
            ) <= set(qualified[public_key]):
                raise BuiltinVoicePublicationRejectedError("capabilities_mismatch")
        limit = caps.get("max_input_codepoints")
        if (
            type(limit) is not int
            or limit > domains["max_input_codepoints"]
            or limit > qualified["max_input_codepoints"]
        ):
            raise BuiltinVoicePublicationRejectedError("capabilities_mismatch")
        verified = verify_binding_release_evidence(
            publication=binding,
            recorded_evidence=evidence,
            binding_definition=definition[1].encode("utf-8"),
            artifact_pin=artifact_pin,
            manifest=manifest,
            report=report,
            scenario_evidence=scenario_evidence,
            now=datetime.now(UTC),
        )
        if verified.expires_at is None or a["expires_at"] > verified.expires_at:
            raise BuiltinVoicePublicationRejectedError("binding_evidence_expired")
        model = json.loads(artifact_pin)["model"]
        manifest_document = json.loads(manifest)
        if (
            model["repository"] != f["model_id"]
            or model["revision"] != f["model_revision"]
            or "tts_file" not in manifest_document["supported_tasks"]
        ):
            raise BuiltinVoicePublicationRejectedError("binding_evidence_mismatch")
        if (
            verified.manifest_id,
            verified.report_id,
            verified.manifest_digest,
            verified.report_digest,
        ) != (
            f["capacity_manifest_id"],
            f["qualification_report_id"],
            f["capacity_manifest_digest"],
            f["qualification_report_digest"],
        ) or not any(
            pin.model_digest == f["model_artifact_digest"] and pin.image_digest == f["image_digest"]
            for pin in verified.stage_pins
        ):
            raise BuiltinVoicePublicationRejectedError("binding_evidence_mismatch")
        # Recheck revocations after acquiring the same voice lock as revocation writers.
        cursor.execute(
            "SELECT 1 FROM speech_builtin_voice_approvals a "
            "JOIN speech_builtin_voice_source_approvals s "
            "ON s.source_approval_id=a.source_approval_id WHERE a.approval_id=%s "
            "AND a.expires_at > clock_timestamp() AND s.expires_at > clock_timestamp() "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_revocations r "
            "WHERE r.approval_id=a.approval_id) "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_source_revocations r "
            "WHERE r.source_approval_id=s.source_approval_id)",
            (approval_id,),
        )
        if len(cursor.fetchall()) != 1:
            raise BuiltinVoicePublicationRejectedError("not_qualified")
        cursor.execute(
            "SELECT revision FROM speech_publication_heads WHERE kind='voice' AND object_id=%s",
            (a["voice_id"],),
        )
        heads = cursor.fetchall()
        if (heads[0]["revision"] if heads else None) != expected_head or find_publication(
            cursor, "voice", a["voice_id"], int(a["revision"])
        ) is not None:
            raise BuiltinVoicePublicationRejectedError("publication_conflict")
        published_at = datetime.fromisoformat(d["published_at"].replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            raise BuiltinVoicePublicationRejectedError("invalid_publication_time")
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        delta = published_at.astimezone(UTC) - epoch
        published_at_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        pub = Publication(
            "voice", a["voice_id"], int(a["revision"]), a["revision_id"], published_at_us
        )
        if not append_publication(cursor, pub):
            raise BuiltinVoicePublicationRejectedError("publication_conflict")
        cursor.execute(
            "INSERT INTO speech_builtin_voice_qualifications "
            "(voice_id, revision, revision_id, binding_revision_id, approval_id, document, "
            "native_speaker_id) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                a["voice_id"],
                a["revision"],
                a["revision_id"],
                a["binding_revision_id"],
                approval_id,
                json.dumps(d),
                f["native_speaker_id"],
            ),
        )
        saved = {
            "request": request,
            "voice_id": a["voice_id"],
            "revision": a["revision"],
            "revision_id": a["revision_id"],
            "facts": f,
        }
        cursor.execute(
            "INSERT INTO speech_builtin_voice_publish_intents "
            "(key_digest,intent,voice_id,revision) VALUES (%s,%s::jsonb,%s,%s)",
            (key_digest, json.dumps(saved), a["voice_id"], a["revision"]),
        )
        return {"status": "created", "voice_id": a["voice_id"], "revision": a["revision"]}

    return store._run(operation, preserve=(BuiltinVoicePublicationRejectedError,))
