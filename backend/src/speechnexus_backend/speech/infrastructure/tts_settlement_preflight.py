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

import psycopg

from speechnexus_backend.speech.domain.job_prerequisites import MIGRATION_COMMAND
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class SettlementPreflightUnavailableError(RuntimeError):
    """The preflight could not read its admission tables.

    A missing schema or relation means this database never ran the migration
    profile. That is a deployment defect with one repair, so it is raised as this
    named refusal instead of letting the driver's ``UndefinedTable`` escape as an
    uncaught traceback that restart-loops the API container (#877 item 3). The
    command is the shared one from ``domain.job_prerequisites``, so every surface
    that names the repair prints the same line (#920).
    """


_TTS_KINDS = (
    "CAST(o.request_document AS jsonb)->>'kind' IN ('tts_file','builtin_voice_qualification_probe')"
)
_TTS_COMMANDS = (
    "CAST(o.request_document AS jsonb)#>>'{gateway_request,command,kind}'='tts' "
    "OR CAST(o.request_document AS jsonb)#>>'{gateway_request,command,task_kind}'='tts_file'"
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
    try:
        with pool.connection(timeout=timeout_seconds) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(
                "SELECT set_config('statement_timeout',?,true)",
                (str(max(1, int(timeout_seconds * 1000))),),
            )
            row = conn.execute(query).fetchone()
    except (psycopg.errors.InvalidSchemaName, psycopg.errors.UndefinedTable) as missing:
        reason = str(missing).strip().splitlines()[0][:300]
        raise SettlementPreflightUnavailableError(
            "the settled-capacity preflight cannot read the admission tables "
            f"({reason or 'no detail'}): this database has not run the migration "
            f"profile; run `{MIGRATION_COMMAND}` before starting the API"
        ) from None
    if row is not None:
        raise ValueError(
            "Unsettled asr/tts admission lacks a settlement root or release; reconcile first"
        )
