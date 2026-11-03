"""Fail-closed lookup of independently approved builtin voice publications.

The caller holds the voice catalogue advisory lock in its admission transaction.
This module never creates or updates qualification evidence.
"""

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.domain.builtin_voice_qualification import BuiltinVoiceFacts
from speechnexus_backend.speech.infrastructure.publications_sql import PublicationCursor


def require_builtin_voice(
    cursor: PublicationCursor,
    *,
    voice_id: str,
    revision: int,
    revision_id: str,
    binding_revision_id: str,
    instance_id: str,
    instance_generation: int,
    host_node_id: str,
    gateway_id: str,
    policy_revision_id: str,
) -> None:
    cursor.execute(
        "SELECT a.facts, floor(extract(epoch FROM a.expires_at)*1000000)::bigint AS expiry_us, "
        "floor(extract(epoch FROM clock_timestamp())*1000000)::bigint AS now_us "
        "FROM speech_builtin_voice_qualifications q "
        "JOIN speech_builtin_voice_approvals a ON a.approval_id=q.approval_id "
        "AND a.voice_id=q.voice_id AND a.revision=q.revision "
        "AND a.revision_id=q.revision_id AND a.binding_revision_id=q.binding_revision_id "
        "JOIN speech_builtin_voice_source_approvals s ON s.source_approval_id=a.source_approval_id "
        "WHERE q.voice_id=%s AND q.revision=%s AND q.revision_id=%s "
        "AND q.binding_revision_id=%s AND a.expires_at > clock_timestamp() "
        "AND q.document=a.facts->'public_document' "
        "AND q.document->>'voice_id'=q.voice_id AND q.document->>'revision'=q.revision "
        "AND a.facts->>'source_approval_id'=s.source_approval_id "
        "AND a.facts->>'binding_revision_id'=q.binding_revision_id "
        "AND q.native_speaker_id=a.facts->>'native_speaker_id' "
        "AND s.source_revision=a.facts->>'source_revision' "
        "AND s.source_digest=a.facts->>'source_digest' "
        "AND s.native_speaker_id=a.facts->>'native_speaker_id' "
        "AND a.facts->>'instance_id'=%s AND a.facts->>'generation'=%s "
        "AND a.facts->>'host_node_id'=%s AND a.facts->>'gateway_id'=%s "
        "AND a.facts->>'gateway_policy_revision_id'=%s "
        "AND s.expires_at > clock_timestamp() "
        "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_source_revocations sr "
        "WHERE sr.source_approval_id=s.source_approval_id) "
        "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_revocations r "
        "WHERE r.approval_id=a.approval_id) "
        "AND EXISTS (SELECT 1 FROM speech_publications p "
        "WHERE p.kind='voice' AND p.object_id=q.voice_id AND p.revision=q.revision "
        "AND p.revision_id=q.revision_id) "
        "AND NOT EXISTS (SELECT 1 FROM speech_publication_retirements r "
        "WHERE r.kind='voice' AND r.object_id=q.voice_id AND r.revision=q.revision "
        "AND r.revision_id=q.revision_id)",
        (
            voice_id,
            str(revision),
            revision_id,
            binding_revision_id,
            instance_id,
            str(instance_generation),
            host_node_id,
            gateway_id,
            policy_revision_id,
        ),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise AdmissionRejectedError("policy_or_resource_disabled")
    try:
        BuiltinVoiceFacts(rows[0]["facts"])
        validate_response("getVoiceRevision", 200, {"body": rows[0]["facts"]["public_document"]})
        if (
            rows[0]["facts"]["expires_at_us"] != rows[0]["expiry_us"]
            or rows[0]["facts"]["checked_at_us"] > rows[0]["now_us"]
        ):
            raise ValueError("invalid qualification time")
    except ValueError, TypeError, KeyError, ContractValidationError:
        raise AdmissionRejectedError("policy_or_resource_disabled") from None
