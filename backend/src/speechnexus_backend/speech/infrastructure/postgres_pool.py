"""Explicit bounded PostgreSQL pool shared by narrow speech transaction adapters."""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

type SpeechConnection = psycopg.Connection[dict[str, Any]]
type SpeechPool = ConnectionPool[SpeechConnection]


def speech_pool(
    database_url: str, *, schema: str = "speech", max_size: int = 4, max_waiting: int = 16
) -> SpeechPool:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise ValueError("Invalid speech schema")
    if not 1 <= max_size <= 64 or not 1 <= max_waiting <= 1024:
        raise ValueError("Speech pool must be bounded")

    def configure(connection: SpeechConnection) -> None:
        connection.isolation_level = IsolationLevel.READ_COMMITTED
        # PostgreSQL otherwise implicitly searches pg_temp before trusted schemas.
        connection.execute(
            "SELECT set_config('search_path', %s, false)", (schema + ",pg_catalog,pg_temp",)
        )
        connection.execute("SET TIME ZONE 'UTC'")
        connection.commit()

    return ConnectionPool[SpeechConnection](
        database_url,
        kwargs={"row_factory": dict_row, "connect_timeout": 3},
        configure=configure,
        name="speech-coordination",
        min_size=0,
        max_size=max_size,
        max_waiting=max_waiting,
        timeout=3,
        open=False,
    )
