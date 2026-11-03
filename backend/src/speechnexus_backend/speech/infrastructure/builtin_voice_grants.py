"""Provisioning declarations for separate builtin voice trust boundaries."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name


def builtin_source_approval_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    """Only the independent source-decision operator receives these grants."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_publication_installation, '
        f'"{schema}".speech_builtin_voice_source_approvals, '
        f'"{schema}".speech_builtin_voice_source_revocations TO "{role}"',
        f"GRANT INSERT (source_approval_id,voice_id,source_revision,source_digest,"
        f'native_speaker_id,expires_at) ON "{schema}".speech_builtin_voice_source_approvals '
        f'TO "{role}"',
    )


def qualification_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_publication_installation TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_builtin_voice_source_approvals, '
        f'"{schema}".speech_builtin_voice_approvals TO "{role}"',
        f"GRANT INSERT (source_approval_id, voice_id, source_revision, source_digest, "
        f'native_speaker_id, expires_at) ON "{schema}".speech_builtin_voice_source_approvals '
        f'TO "{role}"',
        f"GRANT INSERT (approval_id, voice_id, revision, revision_id, binding_revision_id, "
        f'source_approval_id, facts, expires_at) ON "{schema}".speech_builtin_voice_approvals '
        f'TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_builtin_voice_source_revocations, '
        f'"{schema}".speech_builtin_voice_revocations TO "{role}"',
    )


def qualification_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_builtin_voice_source_approvals, '
        f'"{schema}".speech_builtin_voice_source_revocations, '
        f'"{schema}".speech_builtin_voice_approvals, '
        f'"{schema}".speech_builtin_voice_revocations, '
        f'"{schema}".speech_builtin_voice_qualifications TO "{role}"',
    )


def builtin_publisher_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        *qualification_reader_grants(schema, role),
        f'GRANT SELECT ON "{schema}".speech_binding_revisions, '
        f'"{schema}".speech_binding_publication_evidence TO "{role}"',
        f'GRANT INSERT ON "{schema}".speech_builtin_voice_qualifications TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_builtin_voice_publish_intents TO "{role}"',
    )
