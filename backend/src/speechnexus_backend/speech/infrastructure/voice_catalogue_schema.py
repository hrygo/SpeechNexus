"""A projection of published documents, with no new catalogue state."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

VOICE_READER_VIEW = "speech_published_voice_revisions"
VOICE_READER_VIEW_DDL = f"""CREATE VIEW {VOICE_READER_VIEW}
WITH (security_barrier = true, security_invoker = false) AS
SELECT p.object_id AS voice_id, p.revision, p.revision_id, p.published_at_us,
       q.document,
       EXISTS (SELECT 1 FROM speech_publication_heads h
               WHERE h.kind=p.kind AND h.object_id=p.object_id
                 AND h.revision=p.revision AND h.revision_id=p.revision_id) AS selected,
       r.mode AS retirement_mode, r.reason AS retirement_reason, r.retired_at_us
FROM speech_publications p
JOIN speech_builtin_voice_qualifications q
  ON q.voice_id=p.object_id AND q.revision=p.revision AND q.revision_id=p.revision_id
LEFT JOIN speech_publication_retirements r
  ON r.kind=p.kind AND r.object_id=p.object_id
 AND r.revision=p.revision AND r.revision_id=p.revision_id
WHERE p.kind='voice'"""


def voice_catalogue_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """Optional addition to the existing model catalogue reader identity."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (f'GRANT SELECT ON "{schema}".{VOICE_READER_VIEW} TO "{role}"',)
