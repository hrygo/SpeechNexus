"""Provisioning declarations for separate builtin voice trust boundaries."""

from speechnexus_backend.speech.infrastructure.coordination_keyed_lock_schema import (
    coordination_keyed_lock_grants,
)
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
        f'"{schema}".speech_builtin_voice_qualifications, '
        # Provenance is read, never inferred: an auditor needs both the label and
        # the attestation row that backs it, and the probe authorization the
        # controller label names.
        f'"{schema}".speech_builtin_voice_qualification_provenance, '
        f'"{schema}".speech_builtin_voice_operator_attestations, '
        f'"{schema}".speech_builtin_voice_probe_authorizations TO "{role}"',
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

    It appends one approval through an explicit eight-column INSERT (so
    ``probe_authorization_id`` and ``result_intent`` are unwritable: a declaration
    can never look like a controller probe) and one table-level INSERT into the
    attestation ledger. Its reads are exactly the facts it cross-checks: the
    installation target, source approvals and revocations, existing approvals and
    attestations, binding revisions with their release evidence, publications and
    retirements (for the revision number this declaration points at), registered
    instances, resource pools and workspace quotas. It cannot read
    ``speech_publication_heads`` and never receives DELETE or TRUNCATE anywhere. The one
    UPDATE it carries is the keyed lock's row-lock allowance
    (``coordination_keyed_locks.version``): the attestation transaction opens by locking
    the binding row, and PostgreSQL refuses ``FOR UPDATE`` on a row whose columns are not
    updatable. That column is never written, so a recorded attestation still cannot be
    rewritten or superseded.
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
        # The attestation transaction opens by locking the binding row (that is what
        # orders the disable check against the attestation), so this identity needs the
        # keyed lock surface. Its own column list comes from the lock's declaration.
        *coordination_keyed_lock_grants(schema, role),
    )
