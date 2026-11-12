"""Bounded evidence statements; reads never repair and writes never rewrite."""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.binding_evidence import RecordedEvidenceIdentity
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
)


class EvidenceCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _evidence(row: dict[str, Any]) -> BindingPublicationEvidence:
    return BindingPublicationEvidence(
        object_id=row["object_id"],
        revision=int(row["revision"]),
        revision_id=row["revision_id"],
        capacity_manifest_id=row["capacity_manifest_id"],
        qualification_report_id=row["qualification_report_id"],
        evidence_source=row["evidence_source"],
        recorded_at_us=int(row["recorded_at_us"]),
        capacity_manifest_digest=row["capacity_manifest_digest"],
        qualification_report_digest=row["qualification_report_digest"],
    )


def find_evidence(
    cursor: EvidenceCursor, object_id: str, revision: int
) -> BindingPublicationEvidence | None:
    cursor.execute(
        "SELECT object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us, "
        "capacity_manifest_digest, qualification_report_digest "
        "FROM speech_binding_publication_evidence "
        "WHERE kind = 'binding' AND object_id = %s AND revision = %s",
        (object_id, str(revision)),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Publication evidence identity is not unique")
    return _evidence(rows[0]) if rows else None


# Document kind -> the two immutable evidence columns naming it. The mapping is
# fixed here so a caller cannot select another relation or expression.
_RECORDED_IDENTITY_COLUMNS = {
    "capacity_manifest": ("capacity_manifest_id", "capacity_manifest_digest"),
    "qualification_report": ("qualification_report_id", "qualification_report_digest"),
}


def find_recorded_identity(
    cursor: EvidenceCursor, kind: str, identity: str
) -> RecordedEvidenceIdentity | None:
    """Return the facts every publication row naming this document agrees on.

    A capacity manifest or qualification report identifier is not unique in the
    evidence table: several immutable binding revisions may name the same
    document. The earliest recorded time is therefore a derived fact, and two
    conflicting recorded digests make the identity ambiguous. That conflict is
    reported as an unreadable store instead of selecting one value silently.
    """
    columns = _RECORDED_IDENTITY_COLUMNS.get(kind)
    if columns is None:
        raise ValueError("Unknown release evidence kind")
    identity_column, digest_column = columns
    cursor.execute(
        "SELECT min(recorded_at_us) AS recorded_at_us, min(" + digest_column + ") AS digest, "
        "count(DISTINCT " + digest_column + ") AS digest_count "
        "FROM speech_binding_publication_evidence "
        "WHERE kind = 'binding' AND " + identity_column + " = %s",
        (identity,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError("Recorded evidence identity is ambiguous")
    row = rows[0]
    if int(row["digest_count"]) > 1:
        raise ValueError("Recorded evidence identity carries conflicting digests")
    if row["recorded_at_us"] is None:
        return None
    return RecordedEvidenceIdentity(
        identity=identity,
        recorded_at_us=int(row["recorded_at_us"]),
        digest=row["digest"],
    )


def insert_evidence(cursor: EvidenceCursor, evidence: BindingPublicationEvidence) -> bool:
    """Return whether this call inserted the row; a race resolves to no insert."""
    cursor.execute(
        "INSERT INTO speech_binding_publication_evidence "
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us, "
        "capacity_manifest_digest, qualification_report_digest) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (kind, object_id, revision) DO NOTHING",
        (
            evidence.kind,
            evidence.object_id,
            str(evidence.revision),
            evidence.revision_id,
            evidence.capacity_manifest_id,
            evidence.qualification_report_id,
            evidence.evidence_source,
            evidence.recorded_at_us,
            evidence.capacity_manifest_digest,
            evidence.qualification_report_digest,
        ),
    )
    return cursor.rowcount == 1


def find_binding_revision_document(
    cursor: EvidenceCursor, binding_id: str, revision: int
) -> tuple[str, str] | None:
    """Return the stored revision identity and its immutable definition JSON."""
    cursor.execute(
        "SELECT revision_id, definition_json FROM speech_binding_revisions "
        "WHERE binding_id = %s AND revision = %s",
        (binding_id, str(revision)),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Binding revision identity is not unique")
    if not rows:
        return None
    return rows[0]["revision_id"], rows[0]["definition_json"]


def list_evidence(cursor: EvidenceCursor) -> list[BindingPublicationEvidence]:
    cursor.execute(
        "SELECT object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us, "
        "capacity_manifest_digest, qualification_report_digest "
        "FROM speech_binding_publication_evidence "
        "WHERE kind = 'binding' ORDER BY object_id, revision"
    )
    return [_evidence(row) for row in cursor.fetchall()]


def evidence_snapshot(cursor: EvidenceCursor) -> tuple[int, int, int]:
    """Return binding publication, evidence and unresolved counts."""
    cursor.execute(
        "SELECT "
        "(SELECT count(*) FROM speech_publications WHERE kind = 'binding') AS publications, "
        "(SELECT count(*) FROM speech_binding_publication_evidence) AS evidence, "
        "(SELECT count(*) FROM speech_publications AS p "
        " LEFT JOIN speech_binding_publication_evidence AS e "
        "   ON e.kind = p.kind AND e.object_id = p.object_id AND e.revision = p.revision "
        " WHERE p.kind = 'binding' AND e.kind IS NULL) AS unresolved"
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError("Evidence snapshot is ambiguous")
    return (
        int(rows[0]["publications"]),
        int(rows[0]["evidence"]),
        int(rows[0]["unresolved"]),
    )


def list_unresolved_publications(cursor: EvidenceCursor) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT p.object_id, p.revision, p.revision_id "
        "FROM speech_publications AS p "
        "LEFT JOIN speech_binding_publication_evidence AS e "
        "  ON e.kind = p.kind AND e.object_id = p.object_id AND e.revision = p.revision "
        "WHERE p.kind = 'binding' AND e.kind IS NULL "
        "ORDER BY p.object_id, p.revision"
    )
    return cursor.fetchall()
