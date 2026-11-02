"""Explicit, append-only revocation using the admission voice barrier."""

from __future__ import annotations

import re

from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import PublicationCursor


class BuiltinVoiceRevocationRejectedError(ValueError):
    """Fixed safe codes only; never include private source or qualification data."""


def revoke_builtin_voice(
    store: PostgresPublications,
    *,
    kind: str,
    approval_id: str,
    reason: str,
    database: str,
    schema: str,
    installation_id: str,
) -> dict[str, str]:
    """Revoke the fixed approval once; changed reasons cannot rewrite history.

    Revocation does not assert that an existing execution stopped or release its
    budget. Source approvals are scoped to one immutable voice identity.
    """
    if (
        kind not in ("source", "qualification")
        or re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", approval_id) is None
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 2000
    ):
        raise BuiltinVoiceRevocationRejectedError("invalid_request")
    source = kind == "source"
    approvals = (
        "speech_builtin_voice_source_approvals" if source else "speech_builtin_voice_approvals"
    )
    revocations = (
        "speech_builtin_voice_source_revocations" if source else "speech_builtin_voice_revocations"
    )
    identity_column = "source_approval_id" if source else "approval_id"

    def operation(cursor: PublicationCursor) -> dict[str, str]:
        cursor.execute(
            "SELECT current_database() AS database, current_schema() AS schema, "
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        )
        targets = cursor.fetchall()
        if len(targets) != 1 or targets[0] != {
            "database": database,
            "schema": schema,
            "installation_id": installation_id,
        }:
            raise BuiltinVoiceRevocationRejectedError("target_changed")
        cursor.execute(
            f"SELECT voice_id FROM {approvals} WHERE {identity_column}=%s", (approval_id,)
        )
        approvals_found = cursor.fetchall()
        if len(approvals_found) != 1:
            raise BuiltinVoiceRevocationRejectedError("approval_not_found")
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':voice:' || %s, 0))",
            (approvals_found[0]["voice_id"],),
        )
        cursor.execute(
            f"SELECT reason FROM {revocations} WHERE {identity_column}=%s", (approval_id,)
        )
        existing = cursor.fetchall()
        if existing:
            if len(existing) != 1 or existing[0]["reason"] != reason:
                raise BuiltinVoiceRevocationRejectedError("intent_conflict")
            return {"outcome": "replayed", "kind": kind, "approval_id": approval_id}
        cursor.execute(
            f"INSERT INTO {revocations} ({identity_column}, reason) VALUES (%s,%s)",
            (approval_id, reason),
        )
        return {"outcome": "revoked", "kind": kind, "approval_id": approval_id}

    return store._run(operation, preserve=(BuiltinVoiceRevocationRejectedError,))
