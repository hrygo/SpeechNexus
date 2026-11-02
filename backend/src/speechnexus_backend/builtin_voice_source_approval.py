"""Record an independent operator's already-made builtin source approval."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import PublicationCursor

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+\-]{0,511}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}\Z")
_SCHEMA = re.compile(r"[a-z][a-z0-9_]{2,62}\Z")


class BuiltinSourceApprovalError(ValueError):
    """Stable, non-content source approval error."""


def approve_builtin_voice_source(
    store: PostgresPublications,
    *,
    database: str,
    schema: str,
    installation_id: str,
    source_approval_id: str,
    voice_id: str,
    source_revision: str,
    source_digest: str,
    native_speaker_id: str,
    expires_at: datetime,
) -> dict[str, str]:
    """Record an actual authorized operator decision; never infer one from evidence."""
    try:
        if (
            not _DATABASE.fullmatch(database)
            or not _SCHEMA.fullmatch(schema)
            or not _IDENTIFIER.fullmatch(source_approval_id)
            or not _IDENTIFIER.fullmatch(voice_id)
            or not _REVISION.fullmatch(source_revision)
            or not _DIGEST.fullmatch(source_digest)
            or native_speaker_id != "Vivian"
            or not isinstance(expires_at, datetime)
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or str(UUID(installation_id)) != installation_id
        ):
            raise ValueError
        expiry = expires_at.astimezone(UTC)
    except ValueError, TypeError, AttributeError, OverflowError:
        raise BuiltinSourceApprovalError("invalid_request") from None

    def operation(cursor: PublicationCursor) -> dict[str, str]:
        cursor.execute(
            "SELECT current_database() AS database,current_schema() AS schema,"
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        )
        targets = cursor.fetchall()
        if len(targets) != 1 or targets[0] != {
            "database": database,
            "schema": schema,
            "installation_id": installation_id,
        }:
            raise BuiltinSourceApprovalError("target_changed")
        # Check every reachable role, including NOINHERIT roles reachable by SET ROLE.
        # The source table permits only the six named INSERT columns.
        cursor.execute(
            "SELECT SESSION_USER<>CURRENT_USER OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole "
            "OR r.rolreplication OR r.rolbypassrls OR "
            "pg_catalog.pg_has_role(SESSION_USER,d.datdba,'MEMBER') OR "
            "pg_catalog.pg_has_role(SESSION_USER,n.nspowner,'MEMBER') OR "
            "pg_catalog.has_schema_privilege(SESSION_USER,n.oid,'CREATE') OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace=n.oid "
            "AND c.relname IN ('speech_publication_installation',"
            "'speech_builtin_voice_source_approvals','speech_builtin_voice_source_revocations',"
            "'speech_builtin_voice_approvals','speech_builtin_voice_revocations',"
            "'speech_builtin_voice_qualifications','speech_builtin_voice_publish_intents',"
            "'speech_builtin_voice_probe_authorizations','speech_builtin_voice_probe_admissions',"
            "'speech_publications','speech_publication_heads','speech_publication_retirements') "
            "AND pg_catalog.pg_has_role(SESSION_USER,c.relowner,'MEMBER')) OR "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
            "WHERE pg_catalog.pg_has_role(SESSION_USER,reachable.oid,'MEMBER') AND ("
            "pg_catalog.left(reachable.rolname,3)='pg_' OR "
            "pg_catalog.has_schema_privilege(reachable.oid,n.oid,'CREATE') OR "
            "reachable.rolsuper OR reachable.rolcreatedb OR reachable.rolcreaterole OR "
            "reachable.rolreplication OR reachable.rolbypassrls OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace=n.oid AND "
            "c.relname IN ('speech_publication_installation',"
            "'speech_builtin_voice_source_approvals',"
            "'speech_builtin_voice_source_revocations','speech_builtin_voice_approvals',"
            "'speech_builtin_voice_revocations','speech_builtin_voice_qualifications',"
            "'speech_builtin_voice_publish_intents','speech_builtin_voice_probe_authorizations',"
            "'speech_builtin_voice_probe_admissions','speech_publications',"
            "'speech_publication_heads','speech_publication_retirements') AND ("
            "pg_catalog.has_table_privilege(reachable.oid,c.oid,"
            "'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
            "pg_catalog.has_any_column_privilege(reachable.oid,c.oid,'UPDATE,REFERENCES') OR "
            "(c.relname<>'speech_builtin_voice_source_approvals' AND ("
            "pg_catalog.has_table_privilege(reachable.oid,c.oid,'INSERT') OR "
            "pg_catalog.has_any_column_privilege(reachable.oid,c.oid,'INSERT'))) OR "
            "(c.relname='speech_builtin_voice_source_approvals' AND ("
            "pg_catalog.has_table_privilege(reachable.oid,c.oid,'INSERT') OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 "
            "AND NOT a.attisdropped AND a.attname NOT IN ('source_approval_id','voice_id',"
            "'source_revision','source_digest','native_speaker_id','expires_at') AND "
            "pg_catalog.has_column_privilege(reachable.oid,c.oid,a.attnum,'INSERT'))))"
            ")))) "
            "AS forbidden FROM pg_catalog.pg_roles r "
            "JOIN pg_catalog.pg_database d ON d.datname=current_database() "
            "JOIN pg_catalog.pg_namespace n ON n.nspname=current_schema() "
            "WHERE r.rolname=SESSION_USER"
        )
        roles = cursor.fetchall()
        if len(roles) != 1 or roles[0]["forbidden"]:
            raise BuiltinSourceApprovalError("writer_identity_forbidden")
        # ID lock makes cross-voice collisions serialize; revocation takes only voice.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:source-approval:'||current_schema()||':'||%s,0))",
            (source_approval_id,),
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:'||current_schema()||':voice:'||%s,0))",
            (voice_id,),
        )
        cursor.execute(
            "SELECT voice_id,source_revision,source_digest,native_speaker_id,expires_at "
            "FROM speech_builtin_voice_source_approvals WHERE source_approval_id=%s",
            (source_approval_id,),
        )
        existing = cursor.fetchall()
        if existing:
            row = existing[0]
            if len(existing) != 1 or (
                row["voice_id"],
                row["source_revision"],
                row["source_digest"],
                row["native_speaker_id"],
                row["expires_at"],
            ) != (voice_id, source_revision, source_digest, native_speaker_id, expiry):
                raise BuiltinSourceApprovalError("intent_conflict")
            return {
                "outcome": "replayed",
                "source_approval_id": source_approval_id,
                "database": database,
                "schema": schema,
                "installation_id": installation_id,
            }
        cursor.execute("SELECT %s::timestamptz > clock_timestamp() AS live", (expiry,))
        if not cursor.fetchall()[0]["live"]:
            raise BuiltinSourceApprovalError("approval_expired")
        cursor.execute(
            "INSERT INTO speech_builtin_voice_source_approvals "
            "(source_approval_id,voice_id,source_revision,source_digest,"
            "native_speaker_id,expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                source_approval_id,
                voice_id,
                source_revision,
                source_digest,
                native_speaker_id,
                expiry,
            ),
        )
        # INSERT may wait for a relation lock after the first expiry check.
        # Reject inside the transaction so the new row is rolled back.
        cursor.execute("SELECT %s::timestamptz > clock_timestamp() AS live", (expiry,))
        if not cursor.fetchall()[0]["live"]:
            raise BuiltinSourceApprovalError("approval_expired")
        return {
            "outcome": "recorded",
            "source_approval_id": source_approval_id,
            "database": database,
            "schema": schema,
            "installation_id": installation_id,
        }

    return store._run(operation, preserve=(BuiltinSourceApprovalError,))
