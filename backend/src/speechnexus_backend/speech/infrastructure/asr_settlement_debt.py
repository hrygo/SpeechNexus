"""Read-only ASR settlement debt for the admin diagnostics surface.

This is an operations view, not a readiness criterion: a deployment can be
healthy while capacity is held without a settlement owner. Every category is a
concrete fact an operator can act on; nothing here authorizes a release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row

from speechnexus_portable_sql.psycopg_driver import connect as portable_connect

_NOW = "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)"
_LIMIT = 200
_SAMPLES = 3
_DETAIL_LIMIT = 512
_TERMINAL = ("succeeded", "failed", "aborted")


@dataclass(frozen=True, slots=True)
class AsrSettlementDebt:
    unknown: int
    cleanup_pending: int
    route_mismatch: int
    settled_but_held: int
    redacted_held: int
    samples: tuple[str, ...]

    @property
    def total(self) -> int:
        return (
            self.unknown
            + self.cleanup_pending
            + self.route_mismatch
            + self.settled_but_held
            + self.redacted_held
        )

    def detail(self) -> str:
        counts = (
            f"unknown={self.unknown} cleanup_pending={self.cleanup_pending} "
            f"route_mismatch={self.route_mismatch} settled_but_held={self.settled_but_held} "
            f"redacted_held={self.redacted_held}"
        )
        if self.total == 0:
            return counts
        samples = "; ".join(self.samples) if self.samples else "none"
        return (counts + f"; samples={samples}")[:_DETAIL_LIMIT]


def summarize(rows: list[dict[str, Any]], route: tuple[str, str] | None) -> AsrSettlementDebt:
    unknown = cleanup = mismatch = settled = redacted = 0
    samples: list[str] = []
    for row in rows:
        category: str | None = None
        if row.get("source") == "redacted":
            redacted += 1
            category = "redacted_held"
        elif row.get("settled_at_us") is not None:
            settled += 1
            category = "settled_but_held"
        elif route is not None and (row["gateway_id"], row["host_node_id"]) != route:
            mismatch += 1
            category = "route_mismatch"
        else:
            state = row.get("execution_state")
            if isinstance(state, str) and state.startswith("unknown"):
                unknown += 1
                category = "unknown"
            elif state in _TERMINAL:
                # `succeeded` only authorizes release with the engine's terminal
                # response. Cleanup can be "complete" while that proof is an abort
                # acknowledgement, and release then rejects forever: that shape is
                # capacity debt, not a healthy terminal.
                terminal_proof = (
                    row.get("evidence_kind") == "engine_terminal_response"
                    and row.get("execution_stopped") == 1
                    and row.get("resources_released") == 1
                )
                if row.get("cleanup_state") != "complete" or (
                    state == "succeeded" and not terminal_proof
                ):
                    cleanup += 1
                    category = "cleanup_pending"
        if category is not None and len(samples) < _SAMPLES:
            samples.append(f"{category}:{row['operation_id']}:overdue_us={row.get('overdue_us')}")
    return AsrSettlementDebt(unknown, cleanup, mismatch, settled, redacted, tuple(samples))


def read_asr_settlement_debt(
    dsn: str,
    schema: str,
    route: tuple[str, str] | None,
    *,
    timeout_seconds: float = 3,
) -> AsrSettlementDebt:
    if not 0 < timeout_seconds <= 30:
        raise ValueError("ASR debt query requires a bounded timeout")
    rooted = (
        "SELECT s.operation_id, s.lease_id, s.gateway_id, s.host_node_id, s.settled_at_us, "
        "i.execution_state, i.cleanup_state, e.evidence_kind, e.execution_stopped, "
        "e.resources_released, "
        f"({_NOW}) - l.deadline_at_us AS overdue_us, 'rooted' AS source "
        "FROM speech_asr_settlement s "
        "JOIN admission_leases l ON l.lease_id=s.lease_id "
        "LEFT JOIN submit_intents i ON i.lease_id=s.lease_id "
        "LEFT JOIN LATERAL (SELECT evidence_kind, execution_stopped, resources_released "
        "FROM execution_stop_evidence WHERE submit_intent_id=i.submit_intent_id "
        "ORDER BY observed_at_us DESC LIMIT 1) e ON TRUE "
        "WHERE l.lease_state<>'released' "
        f"AND (s.settled_at_us IS NOT NULL OR s.next_check_at_us<=({_NOW})) "
        "ORDER BY s.next_check_at_us, s.lease_id"
    )
    # A redacted document has no readable kind, so neither the backfill nor the
    # preflight can attribute it. It has no root and would otherwise be silent.
    redacted = (
        "SELECT l.operation_id, l.lease_id, NULL::text AS gateway_id, "
        "NULL::text AS host_node_id, NULL::bigint AS settled_at_us, "
        "NULL::text AS execution_state, NULL::text AS cleanup_state, "
        "NULL::text AS evidence_kind, NULL::int AS execution_stopped, "
        "NULL::int AS resources_released, "
        f"({_NOW}) - l.deadline_at_us AS overdue_us, 'redacted' AS source "
        "FROM admission_leases l "
        "JOIN admission_operations o ON o.operation_id=l.operation_id "
        "WHERE l.lease_state<>'released' AND o.operation_id LIKE 'operation-%%' "
        "AND o.request_document IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM speech_asr_settlement s WHERE s.lease_id=l.lease_id) "
        "ORDER BY l.lease_id"
    )
    with portable_connect(
        dsn, connect_timeout=int(timeout_seconds), row_factory=dict_row
    ) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SELECT set_config('search_path', ?, true)", (schema + ",pg_catalog",))
        connection.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(timeout_seconds * 1000))),),
        )
        # 受限方言没有 LIMIT:按 keyset 排序后由 fetchmany 取上界,行数在客户端截断。
        rows = [
            *connection.execute(rooted).fetchmany(_LIMIT),
            *connection.execute(redacted).fetchmany(_LIMIT),
        ]
    return summarize(rows, route)
