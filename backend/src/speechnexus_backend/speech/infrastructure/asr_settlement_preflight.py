"""Refuse startup while a held synchronous ASR lease has no settlement owner."""

from __future__ import annotations

import psycopg

from speechnexus_backend.speech.domain.job_prerequisites import MIGRATION_COMMAND
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    SettlementPreflightUnavailableError,
)

_ROOT_ABSENT = (
    "NOT EXISTS (SELECT 1 FROM speech_asr_settlement s "
    "WHERE s.lease_id=l.lease_id AND s.operation_id=l.operation_id "
    "AND s.attempt_id=l.attempt_id)"
)


def require_complete_asr_roots(pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
    """Read only; the worker's release transaction still re-reads every fact."""
    if not 0 < timeout_seconds <= 30:
        raise ValueError("ASR preflight requires a bounded timeout")
    query = (
        "SELECT l.operation_id,l.attempt_id,l.lease_id FROM admission_leases l "
        "JOIN admission_operations o ON o.operation_id=l.operation_id "
        "WHERE ("
        # A held synchronous ASR admission must own a root. Public jobs are
        # `job-…` and qualification probes are not `asr_file`; neither is ours.
        "l.lease_state<>'released' AND o.operation_id LIKE 'operation-%' "
        "AND (CASE WHEN o.operation_id LIKE 'operation-%' "
        "THEN CAST(o.request_document AS jsonb)->>'kind' END)='asr_file' "
        f"AND {_ROOT_ABSENT}"
        " OR "
        # A settled root still holding a lease means the marker ran without a
        # release; it is a defect, not a completed settlement.
        "l.lease_state<>'released' AND EXISTS (SELECT 1 FROM speech_asr_settlement s "
        "WHERE s.lease_id=l.lease_id AND s.settled_at_us IS NOT NULL)"
        ")"
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
            "the ASR settlement preflight cannot read the admission tables "
            f"({reason or 'no detail'}): this database has not run the migration "
            f"profile; run `{MIGRATION_COMMAND}` before starting the API"
        ) from None
    if row is not None:
        raise ValueError(
            "A held ASR admission lacks a settlement root, or a settled root still "
            "holds its lease; reconcile before starting the worker"
        )
