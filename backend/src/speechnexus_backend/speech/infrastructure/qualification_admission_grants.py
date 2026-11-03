"""Separate database roles for approving and executing bounded qualification probes."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.tts_settlement_schema import (
    tts_settlement_writer_grants,
)


def qualification_authorizer_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT SELECT, INSERT (authorization_id, source_approval_id, voice_id, intent) "
        f'ON "{schema}".speech_builtin_voice_probe_authorizations TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_builtin_voice_source_approvals, '
        f'"{schema}".speech_builtin_voice_source_revocations, '
        f'"{schema}".speech_publication_installation, '
        f'"{schema}".speech_binding_revisions, '
        f'"{schema}".speech_binding_publication_evidence, '
        f'"{schema}".speech_publications, '
        f'"{schema}".speech_publication_retirements, '
        f'"{schema}".runtime_instance_heads, '
        f'"{schema}".runtime_instance_intents, '
        f'"{schema}".engine_instances TO "{role}"',
        # PostgreSQL requires UPDATE privilege even for SELECT FOR UPDATE.
        f'GRANT UPDATE (version) ON "{schema}".runtime_instance_heads TO "{role}"',
        f'GRANT UPDATE (version) ON "{schema}".engine_instances TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_builtin_voice_probe_admissions, '
        f'"{schema}".admission_operations, "{schema}".admission_leases TO "{role}"',
        f"GRANT INSERT (probe_authorization_id, result_intent) "
        f'ON "{schema}".speech_builtin_voice_approvals TO "{role}"',
        f"GRANT SELECT (submit_intent_id,lease_id,voice_revision_id,execution_state,"
        f'generated_pcm_samples) ON "{schema}".submit_intents TO "{role}"',
        f"GRANT SELECT (submit_intent_id,evidence_kind,evidence_digest,execution_stopped,"
        f'resources_released,observed_at_us) ON "{schema}".execution_stop_evidence TO "{role}"',
    )


def qualification_admission_grants(schema: str, role: str) -> tuple[str, ...]:
    """Supplement normal admission grants; never grant issuance privileges here."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        *tts_settlement_writer_grants(schema, role),
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_builtin_voice_probe_authorizations TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_builtin_voice_probe_admissions TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_builtin_voice_source_approvals, '
        f'"{schema}".speech_builtin_voice_source_revocations, '
        f'"{schema}".speech_binding_publication_evidence, '
        f'"{schema}".speech_binding_revisions TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_publications TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_publication_installation TO "{role}"',
        f"GRANT SELECT (submit_intent_id,lease_id,voice_revision_id,execution_state,"
        f"sample_rate_hz,generated_pcm_samples,hard_output_ceiling_samples) "
        f'ON "{schema}".submit_intents TO "{role}"',
        f"GRANT SELECT (submit_intent_id,evidence_kind,evidence_digest,execution_stopped,"
        f'resources_released,observed_at_us) ON "{schema}".execution_stop_evidence TO "{role}"',
    )
