"""Bounded durable ASR settlement roots; never read private command content."""

from __future__ import annotations

from typing import Any

from speechnexus_backend.speech.domain.asr_settlement import AsrSettlementRoot
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_NOW = "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)"


class PostgresAsrSettlementStore:
    """Every call owns one short transaction; external calls occur after commit."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("ASR settlement storage deadline must be bounded")
        self._pool, self._timeout = pool, timeout_seconds

    def due(self, gateway_id: str, host_node_id: str, limit: int) -> list[AsrSettlementRoot]:
        if not 1 <= limit <= 128:
            raise ValueError("ASR settlement batch must be bounded")
        with self._pool.connection(timeout=self._timeout) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            self._deadline(conn)
            rows = conn.execute(
                "SELECT lease_id,operation_id,attempt_id,gateway_id,host_node_id "
                "FROM speech_asr_settlement WHERE settled_at_us IS NULL "
                f"AND next_check_at_us <= {_NOW} "
                "AND gateway_id=%s AND host_node_id=%s "
                "ORDER BY next_check_at_us, lease_id LIMIT %s",
                (gateway_id, host_node_id, limit),
            ).fetchall()
        return [
            AsrSettlementRoot(
                str(row["lease_id"]),
                str(row["operation_id"]),
                str(row["attempt_id"]),
                str(row["gateway_id"]),
                str(row["host_node_id"]),
            )
            for row in rows
        ]

    def intent(self, lease_id: str) -> str | None:
        with self._pool.connection(timeout=self._timeout) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            self._deadline(conn)
            rows = conn.execute(
                "SELECT submit_intent_id FROM submit_intents WHERE lease_id=%s "
                "ORDER BY submit_intent_id",
                (lease_id,),
            ).fetchall()
        if len(rows) > 1:
            # The release transaction refuses this shape; never pick one.
            raise ValueError("Lease has more than one submit intent")
        return str(rows[0]["submit_intent_id"]) if rows else None

    def defer(self, lease_id: str, *, seconds: float) -> None:
        if not 0 < seconds <= 300:
            raise ValueError("ASR settlement delay must be bounded")
        with self._pool.connection(timeout=self._timeout) as conn:
            self._deadline(conn)
            conn.execute(
                "UPDATE speech_asr_settlement SET next_check_at_us="
                f"{_NOW}+%s WHERE lease_id=%s AND settled_at_us IS NULL",
                (int(seconds * 1_000_000), lease_id),
            )

    def mark_settled(self, lease_id: str) -> None:
        with self._pool.connection(timeout=self._timeout) as conn:
            self._deadline(conn)
            conn.execute(
                "UPDATE speech_asr_settlement SET settled_at_us="
                f"{_NOW} WHERE lease_id=%s AND settled_at_us IS NULL",
                (lease_id,),
            )

    def _deadline(self, conn: Any) -> None:
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(self._timeout * 1000))),),
        )
