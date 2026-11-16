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
        f'"{schema}".speech_binding_publication_evidence, '
        f'"{schema}".speech_builtin_voice_operator_attestations TO "{role}"',
        f'GRANT INSERT ON "{schema}".speech_builtin_voice_qualifications, '
        f'"{schema}".speech_builtin_voice_qualification_provenance TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_builtin_voice_publish_intents TO "{role}"',
    )


def operator_attestation_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    """Only the independent operator identity that records an attestation.

    It may append one approval and its attestation record and read the durable
    facts it must cross-check. It must not read qualifications, publications or
    publication heads, and it never receives UPDATE, DELETE or TRUNCATE: a
    recorded attestation cannot be rewritten afterwards.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_publication_installation, '
        f'"{schema}".speech_builtin_voice_source_approvals, '
        f'"{schema}".speech_builtin_voice_source_revocations, '
        f'"{schema}".speech_builtin_voice_approvals, '
        f'"{schema}".speech_builtin_voice_revocations, '
        f'"{schema}".speech_builtin_voice_operator_attestations, '
        f'"{schema}".speech_binding_revisions, '
        f'"{schema}".speech_binding_publication_evidence, '
        f'"{schema}".speech_publications, '
        f'"{schema}".speech_publication_retirements, '
        f'"{schema}".engine_instances, '
        f'"{schema}".runtime_instance_heads, '
        f'"{schema}".resource_pools, '
        f'"{schema}".workspace_quotas TO "{role}"',
        f"GRANT INSERT (approval_id, voice_id, revision, revision_id, binding_revision_id, "
        f'source_approval_id, facts, expires_at) ON "{schema}".speech_builtin_voice_approvals '
        f'TO "{role}"',
        f'GRANT INSERT ON "{schema}".speech_builtin_voice_operator_attestations TO "{role}"',
    )
