"""Readiness probes for authoritative and materialized storage."""

from __future__ import annotations

import httpx
import psycopg
from pydantic import AnyHttpUrl


class PostgresReadinessProbe:
    """Verify that the authoritative PostgreSQL endpoint accepts a query."""

    def __init__(self, dsn: str, *, timeout_seconds: float) -> None:
        self._dsn = dsn
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "postgresql"

    async def check(self) -> None:
        connection = await psycopg.AsyncConnection.connect(
            self._dsn,
            connect_timeout=max(1, round(self._timeout_seconds)),
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                row = await cursor.fetchone()
                if row != (1,):
                    raise RuntimeError("PostgreSQL readiness query returned an unexpected result")
        finally:
            await connection.close()


class ObjectStorageReadinessProbe:
    """Verify an explicitly configured object-storage health endpoint."""

    def __init__(self, healthcheck_url: AnyHttpUrl, *, timeout_seconds: float) -> None:
        self._healthcheck_url = healthcheck_url
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "object_storage"

    async def check(self) -> None:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.get(str(self._healthcheck_url))
            response.raise_for_status()
