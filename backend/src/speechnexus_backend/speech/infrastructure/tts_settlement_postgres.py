"""Bounded durable TTS settlement roots; never read private command content."""

from __future__ import annotations

import json
from typing import Any, cast

from speechnexus_backend.speech.domain.tts_settlement import (
    AbortReason,
    SettlementRoot,
    TtsSettlementTarget,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class PostgresTtsSettlementStore:
    """Every call owns one short transaction; external calls occur after commit."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Settlement storage deadline must be bounded")
        self._pool, self._timeout = pool, timeout_seconds

    def due(self, gateway_id: str, node_id: str, limit: int) -> list[SettlementRoot]:
        if not 1 <= limit <= 128:
            raise ValueError("Settlement batch must be bounded")
        with self._pool.connection(timeout=self._timeout) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            self._deadline(conn)
            rows = conn.execute(
                "SELECT target_document, deadline_at_us, abort_reason, "
                "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us "
                "FROM speech_tts_settlement WHERE settled_at_us IS NULL "
                "AND next_check_at_us <= "
                "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) "
                "AND target_document::jsonb->>'gateway_id'=%s "
                "AND target_document::jsonb->>'host_node_id'=%s "
                "ORDER BY next_check_at_us, lease_id LIMIT %s",
                (gateway_id, node_id, limit),
            ).fetchall()
        result: list[SettlementRoot] = []
        for row in rows:
            target = TtsSettlementTarget(**json.loads(row["target_document"]))
            reason = row["abort_reason"]
            if reason not in (None, "client_cancelled", "deadline_exceeded", "stream_disconnected"):
                raise ValueError("Invalid stored TTS settlement reason")
            result.append(
                SettlementRoot(
                    target,
                    int(row["deadline_at_us"]),
                    int(row["now_us"]),
                    cast(AbortReason | None, reason),
                )
            )
        return result

    def request_cancellation(self, operation_id: str, reason: AbortReason) -> None:
        """Persist the original operation even when admission has not committed yet."""
        from speechnexus_backend.speech.domain.runtime_intents import identifier

        identifier(operation_id)
        if reason not in ("client_cancelled", "stream_disconnected", "deadline_exceeded"):
            raise ValueError("Invalid TTS cancellation reason")
        with self._pool.connection(timeout=self._timeout) as conn:
            self._deadline(conn)
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('speechnexus:tts-cancel:' "
                "|| current_schema() || ':' || %s, 0))",
                (operation_id,),
            )
            conn.execute(
                "INSERT INTO speech_tts_cancellation (operation_id,reason,requested_at_us) "
                "VALUES (%s,%s,CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)) "
                "ON CONFLICT (operation_id) DO NOTHING",
                (operation_id, reason),
            )
            conn.execute(
                "UPDATE speech_tts_settlement SET abort_reason=COALESCE(abort_reason, "
                "(SELECT reason FROM speech_tts_cancellation WHERE operation_id=%s)), "
                "next_check_at_us=0 WHERE operation_id=%s AND settled_at_us IS NULL",
                (operation_id, operation_id),
            )

    def request_abort(self, lease_id: str, reason: AbortReason) -> AbortReason | None:
        if reason not in ("client_cancelled", "deadline_exceeded", "stream_disconnected"):
            raise ValueError("Invalid TTS abort reason")
        with self._pool.connection(timeout=self._timeout) as conn:
            self._deadline(conn)
            row = conn.execute(
                "UPDATE speech_tts_settlement SET abort_reason=COALESCE(abort_reason,%s), "
                "next_check_at_us=0 WHERE lease_id=%s AND settled_at_us IS NULL "
                "RETURNING abort_reason",
                (reason, lease_id),
            ).fetchone()
        return cast(AbortReason, row["abort_reason"]) if row else None

    def defer(self, lease_id: str, *, seconds: float) -> None:
        if not 0 < seconds <= 300:
            raise ValueError("Settlement delay must be bounded")
        with self._pool.connection(timeout=self._timeout) as conn:
            self._deadline(conn)
            conn.execute(
                "UPDATE speech_tts_settlement SET next_check_at_us="
                "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)+%s "
                "WHERE lease_id=%s AND settled_at_us IS NULL",
                (int(seconds * 1_000_000), lease_id),
            )

    def mark_settled(self, lease_id: str) -> None:
        with self._pool.connection(timeout=self._timeout) as conn:
            self._deadline(conn)
            conn.execute(
                "UPDATE speech_tts_settlement SET settled_at_us="
                "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) "
                "WHERE lease_id=%s AND settled_at_us IS NULL",
                (lease_id,),
            )

    def _deadline(self, conn: Any) -> None:
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(self._timeout * 1000))),),
        )
