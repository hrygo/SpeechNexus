"""Reject startup if old unfinished TTS admissions lack durable settlement roots."""

from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


def require_complete_tts_roots(pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
    """Read only; an operator must reconcile the exact original identities separately."""
    if not 0 < timeout_seconds <= 30:
        raise ValueError("TTS preflight requires a bounded timeout")
    with pool.connection(timeout=timeout_seconds) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(
            "SELECT set_config('statement_timeout',%s,true)",
            (str(max(1, int(timeout_seconds * 1000))),),
        )
        row = conn.execute(
            "SELECT l.operation_id,l.attempt_id,l.lease_id FROM admission_leases l "
            "JOIN admission_operations o ON o.operation_id=l.operation_id "
            "WHERE l.lease_state<>'released' "
            "AND (o.request_document::jsonb->>'kind' IN "
            "('tts_file','builtin_voice_qualification_probe') "
            "OR o.request_document::jsonb#>>'{gateway_request,command,kind}'='tts' "
            "OR o.request_document::jsonb#>>'{gateway_request,command,task_kind}'='tts_file') "
            "AND NOT EXISTS (SELECT 1 FROM speech_tts_settlement s "
            "WHERE s.lease_id=l.lease_id AND s.operation_id=l.operation_id "
            "AND s.attempt_id=l.attempt_id) LIMIT 1"
        ).fetchone()
    if row is not None:
        raise ValueError(
            "Unsettled original TTS admission lacks a settlement root; reconcile first"
        )
