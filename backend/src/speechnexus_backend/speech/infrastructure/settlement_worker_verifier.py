"""Shared least-privilege check for the settlement role.

Both settlement workers use the same deployment role. The role therefore holds
the union of their table privileges; each worker still verifies the complete
boundary so a missing grant fails startup instead of a scan failing silently.
"""

from __future__ import annotations

from speechnexus_backend.binding_disable_worker import (
    _SETTLEMENT_FULL_READ,
    _SETTLEMENT_READ,
    _SETTLEMENT_UPDATE,
    _verify_no_role_switch,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_schema import (
    asr_settlement_worker_grants,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.tts_settlement_schema import (
    tts_settlement_worker_grants,
)

OWNED_TABLES = frozenset(
    {"speech_tts_settlement", "speech_tts_cancellation", "speech_asr_settlement"}
)
UPDATE_COLUMNS = {
    "speech_tts_settlement": frozenset({"abort_reason", "next_check_at_us", "settled_at_us"}),
    "speech_asr_settlement": frozenset({"next_check_at_us", "settled_at_us"}),
}
INSERT_TABLES = frozenset({"speech_tts_cancellation"})


def settlement_worker_grants(schema: str, role: str) -> tuple[str, ...]:
    """The one grant set both settlement workers verify at startup.

    The lease-settlement reads/writes, the TTS root/cancellation surface and the
    ASR root cursor belong to one role. Keeping the composition next to
    ``OWNED_TABLES`` means a new owned table changes this declaration and the
    verification together, rather than only one of them.
    """
    return (
        *disable_settlement_grants(schema, role),
        *tts_settlement_worker_grants(schema, role),
        *asr_settlement_worker_grants(schema, role),
    )


def verify_settlement_worker(pool: SpeechPool, schema: str, *, label: str) -> None:
    """Require a non-owner principal and the complete shared worker boundary."""
    with pool.connection(timeout=3) as conn:
        conn.execute("SET LOCAL statement_timeout='3s'")
        _verify_no_role_switch(conn, label)
        row = conn.execute(
            "SELECT CURRENT_USER AS role, current_schema() AS schema, "
            "pg_catalog.has_schema_privilege(CURRENT_USER,?,'CREATE') AS creates, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
            "ON c.relnamespace=n.oid WHERE n.nspname=? "
            "AND pg_catalog.pg_has_role(CURRENT_USER,c.relowner,'MEMBER')) AS owns",
            (schema, schema),
        ).fetchone()
        if row is None or row["schema"] != schema or row["creates"] or row["owns"]:
            raise ValueError(f"{label} requires a restricted non-owner identity")
        rows = conn.execute(
            "SELECT c.relname, a.attname, "
            "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'SELECT') AS rd, "
            "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'UPDATE') AS upd, "
            "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'INSERT') AS ins, "
            "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'REFERENCES') AS ref, "
            "pg_catalog.has_table_privilege(CURRENT_USER,c.oid,'DELETE,TRUNCATE,TRIGGER') "
            "AS destructive "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 "
            "AND NOT a.attisdropped WHERE n.nspname=? AND c.relkind IN ('r','p','v','m')",
            (schema,),
        ).fetchall()
        for column in rows:
            table, name = column["relname"], column["attname"]
            can_read = (
                table in _SETTLEMENT_FULL_READ
                or name in _SETTLEMENT_READ.get(table, set())
                or table in OWNED_TABLES
            )
            can_update = name in _SETTLEMENT_UPDATE.get(table, set()) or name in (
                UPDATE_COLUMNS.get(table, frozenset())
            )
            can_insert = table in INSERT_TABLES
            if (
                bool(column["rd"]) != can_read
                or bool(column["upd"]) != can_update
                or bool(column["ins"]) != can_insert
                or column["ref"]
                or column["destructive"]
            ):
                raise ValueError(f"{label} privileges exceed or miss the worker boundary")
        for table, privilege in (
            ("speech_tts_settlement", "SELECT"),
            ("speech_tts_cancellation", "SELECT,INSERT"),
            ("speech_asr_settlement", "SELECT"),
        ):
            result = conn.execute(
                "SELECT pg_catalog.has_table_privilege(CURRENT_USER,?,?) AS allowed",
                (f"{schema}.{table}", privilege),
            ).fetchone()
            if result is None or not result["allowed"]:
                raise ValueError(f"{label} privileges are incomplete")
        for table, columns in UPDATE_COLUMNS.items():
            for update_column in columns:
                result = conn.execute(
                    "SELECT pg_catalog.has_column_privilege(CURRENT_USER,?,?,'UPDATE') AS allowed",
                    (f"{schema}.{table}", update_column),
                ).fetchone()
                if result is None or not result["allowed"]:
                    raise ValueError(f"{label} privileges are incomplete")
