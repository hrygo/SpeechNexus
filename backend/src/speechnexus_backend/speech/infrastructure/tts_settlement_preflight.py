"""Reject startup while asr/tts capacity is held without a settlement owner.

Two kinds of admission exist on the same lease table:

* synchronous TTS and qualification admissions write their own
  ``speech_tts_settlement`` root in the admission transaction; a historical row
  without one must be reconciled before the process starts;
* public asr/tts admissions never write that root. The dispatcher returns their
  capacity through ``LeaseService.release`` on the terminal Job, so such a lease
  needs no root while it is still live. A settled window after its frozen
  deadline is allowed for that settlement; a lease still held after the window
  has leaked capacity and the process must refuse to start until it is
  reconciled.

Public Job operation ids are the public Job ids (``job-…``); synchronous
admissions use ``operation-…``. The public branch keys on that prefix, not on the
stored command kind, because a redacted public command has no kind left to match
and would otherwise hide held capacity. A future identity change fails closed
(startup refused) instead of silently ignoring held capacity.
"""

from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_TTS_KINDS = (
    "o.request_document::jsonb->>'kind' IN ('tts_file','builtin_voice_qualification_probe')"
)
_TTS_COMMANDS = (
    "o.request_document::jsonb#>>'{gateway_request,command,kind}'='tts' "
    "OR o.request_document::jsonb#>>'{gateway_request,command,task_kind}'='tts_file'"
)
# The dispatcher settles a past-deadline Job as soon as its claim is due; allow
# one bounded window beyond the frozen deadline before treating a still-held
# public lease as leaked capacity.
_PUBLIC_SETTLEMENT_GRACE_US = 60_000_000
_ROOT_ABSENT = (
    "NOT EXISTS (SELECT 1 FROM speech_tts_settlement s "
    "WHERE s.lease_id=l.lease_id AND s.operation_id=l.operation_id "
    "AND s.attempt_id=l.attempt_id)"
)


def require_complete_tts_roots(pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
    """Read only; an operator must reconcile the exact original identities separately."""
    if not 0 < timeout_seconds <= 30:
        raise ValueError("TTS preflight requires a bounded timeout")
    query = (
        "SELECT l.operation_id,l.attempt_id,l.lease_id FROM admission_leases l "
        "JOIN admission_operations o ON o.operation_id=l.operation_id "
        "CROSS JOIN (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 "
        "AS BIGINT) AS now_us) AS clock "
        "WHERE l.lease_state<>'released' AND ("
        # Historical synchronous TTS/qualification admissions own a root. A
        # public Job admission has the same kind label but is settled by the
        # dispatcher, so it is exempt while it is live.
        f"(({_TTS_KINDS}) OR ({_TTS_COMMANDS})) "
        f"AND o.operation_id NOT LIKE 'job-%' AND {_ROOT_ABSENT}"
        " OR "
        # A live public Job lease needs no root, but once its frozen deadline has
        # passed an unreleased lease is held capacity that nobody settled. The
        # `job-` prefix is the durable public identity; do not match on the
        # command kind, which a redacted public row no longer has.
        f"l.deadline_at_us<=clock.now_us - {_PUBLIC_SETTLEMENT_GRACE_US} "
        "AND l.operation_id LIKE 'job-%' "
        f"AND {_ROOT_ABSENT}"
        ") LIMIT 1"
    )
    with pool.connection(timeout=timeout_seconds) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(
            "SELECT set_config('statement_timeout',%s,true)",
            (str(max(1, int(timeout_seconds * 1000))),),
        )
        row = conn.execute(query).fetchone()
    if row is not None:
        raise ValueError(
            "Unsettled asr/tts admission lacks a settlement root or release; reconcile first"
        )
