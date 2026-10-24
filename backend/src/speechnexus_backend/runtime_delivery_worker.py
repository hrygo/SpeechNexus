"""Explicit per-node runtime transport worker; no inference or budget authority."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import ssl
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal, Self

from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.runtime_delivery import RuntimeDeliveryService
from speechnexus_backend.speech.domain.runtime_delivery import DeliveryBatch, DeliveryClaimRequest
from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.runtime_controller_http import (
    HttpRuntimeController,
    RuntimeControllerEndpoint,
)
from speechnexus_backend.speech.infrastructure.runtime_delivery_postgres import (
    PostgresRuntimeDelivery,
)


class RuntimeDeliverySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPEECHNEXUS_RUNTIME_DELIVERY_",
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        frozen=True,
    )

    process_role: Literal["job-worker"] = Field(validation_alias="SPEECHNEXUS_PROCESS_ROLE")
    node_id: str = Field(validation_alias="SPEECHNEXUS_NODE_ID")
    instance_id: str = Field(validation_alias="SPEECHNEXUS_INSTANCE_ID")
    database_url: SecretStr
    database_schema: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    target_node_id: str
    controller_address: str = Field(repr=False)
    controller_port: int = Field(ge=1, le=65535)
    controller_token: SecretStr
    ca_file: Path | None = Field(default=None, repr=False)
    lease_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    call_timeout_seconds: float = Field(default=3, gt=0, le=1800, allow_inf_nan=False)
    storage_timeout_seconds: float = Field(default=3, gt=0, le=30, allow_inf_nan=False)
    interval_seconds: float = Field(default=1, gt=0, le=300, allow_inf_nan=False)
    retry_seconds: float = Field(default=1, ge=0.1, le=300, allow_inf_nan=False)
    batch_size: int = Field(default=8, ge=1, le=32)

    @property
    def holder_id(self) -> str:
        return self.node_id + ":" + self.instance_id

    @model_validator(mode="after")
    def explicit_bounded_configuration(self) -> Self:
        for value in (self.node_id, self.instance_id, self.holder_id, self.target_node_id):
            identifier(value)
        try:
            info = conninfo_to_dict(self.database_url.get_secret_value())
        except Exception:
            raise ValueError("Delivery requires an explicit PostgreSQL connection") from None
        if not all(info.get(key) for key in ("user", "dbname")) or not (
            info.get("host") or info.get("hostaddr")
        ):
            raise ValueError(
                "Delivery PostgreSQL host, database and runtime principal must be explicit"
            )
        if self.lease_seconds <= self.call_timeout_seconds + 3 * self.storage_timeout_seconds + 1:
            raise ValueError("Delivery lease must cover storage, call and finalization deadlines")
        # Reuse the transport's IP, scope, token and port rules; never maintain a
        # second endpoint validator or accept addresses from queued commands.
        self.endpoint()
        return self

    def endpoint(self) -> RuntimeControllerEndpoint:
        return RuntimeControllerEndpoint(
            self.target_node_id,
            self.controller_address,
            self.controller_port,
            self.controller_token.get_secret_value(),
        )


class RuntimeDeliveryRuntime:
    def __init__(self, settings: RuntimeDeliverySettings) -> None:
        self.settings = settings
        self._pool = speech_pool(
            settings.database_url.get_secret_value(), schema=settings.database_schema
        )
        self._stack = AsyncExitStack()
        self.service: RuntimeDeliveryService | None = None
        self.claim = DeliveryClaimRequest(
            settings.holder_id, (settings.target_node_id,), int(settings.lease_seconds * 1000000)
        )

    def _check_role(self) -> None:
        # A startup owner/DDL guard, not a claim to audit every inherited/global
        # privilege. Provision the documented dedicated DML role separately.
        with self._pool.connection(timeout=self.settings.storage_timeout_seconds) as connection:
            connection.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (str(max(1, int(self.settings.storage_timeout_seconds * 1000))),),
            )
            row = connection.execute(
                "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s,'CREATE') "
                "OR pg_catalog.pg_has_role(d.datdba,'USAGE') OR EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname=%s "
                "AND pg_catalog.pg_has_role(c.relowner,'USAGE')) AS forbidden "
                "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                "WHERE r.rolname=CURRENT_USER AND d.datname=CURRENT_DATABASE()",
                (self.settings.database_schema, self.settings.database_schema),
            ).fetchone()
            if row is None or row["forbidden"]:
                raise ValueError("Delivery runtime requires a non-owner role without DDL")
            connection.execute("SELECT event_id FROM runtime_intent_deliveries LIMIT 0")

    async def __aenter__(self) -> Self:
        self._pool.open()
        try:
            await asyncio.to_thread(self._check_role)
            context = ssl.create_default_context(
                cafile=str(self.settings.ca_file) if self.settings.ca_file else None
            )
            controller = await self._stack.enter_async_context(
                HttpRuntimeController(
                    self.settings.endpoint(),
                    timeout_seconds=self.settings.call_timeout_seconds,
                    ssl_context=context,
                    max_in_flight=1,
                    max_waiting=1,
                )
            )
            self.service = RuntimeDeliveryService(
                PostgresRuntimeDelivery(
                    self._pool, timeout_seconds=self.settings.storage_timeout_seconds
                ),
                {self.settings.target_node_id: controller},
                retry_delay_us=int(self.settings.retry_seconds * 1000000),
            )
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        try:
            await self._stack.aclose()
        finally:
            await asyncio.to_thread(self._pool.close)
            self.service = None


async def run_delivery(
    service: RuntimeDeliveryService,
    claim: DeliveryClaimRequest,
    stop: asyncio.Event,
    *,
    interval_seconds: float,
    batch_size: int,
    once: bool = False,
    on_result: Callable[[DeliveryBatch], None] | None = None,
) -> None:
    if not 0 < interval_seconds <= 300:
        raise ValueError("Delivery polling requires an explicit bounded interval")
    while not stop.is_set():
        result = await service.run_once(claim, limit=batch_size, should_stop=stop.is_set)
        if on_result is not None:
            on_result(result)
        if once:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _main(settings: RuntimeDeliverySettings, *, once: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # Pool background retries otherwise print host/principal details before the
    # CLI can replace its final exception with a stable unavailable message.
    pool_logger = logging.getLogger("psycopg.pool")
    previous_disabled = pool_logger.disabled
    pool_logger.disabled = True
    try:
        async with RuntimeDeliveryRuntime(settings) as runtime:
            assert runtime.service is not None

            def report(result: DeliveryBatch) -> None:
                # Only counters. No originals, replies, credentials or endpoint
                # metadata are printed; unresolved work remains persisted.
                print(
                    json.dumps(
                        {
                            "claimed": result.claimed,
                            "acknowledged": result.acknowledged,
                            "unknown": result.unknown,
                            "stale": result.stale,
                            "blocked": result.blocked,
                        }
                    ),
                    flush=True,
                )

            await run_delivery(
                runtime.service,
                runtime.claim,
                stop,
                interval_seconds=settings.interval_seconds,
                batch_size=settings.batch_size,
                once=once,
                on_result=report,
            )
    finally:
        pool_logger.disabled = previous_disabled
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deliver original runtime intents with persistent unknown recovery"
    )
    parser.add_argument("--once", action="store_true", help="Run one bounded pass, then exit")
    options = parser.parse_args()
    try:
        settings = RuntimeDeliverySettings()
        asyncio.run(_main(settings, once=options.once))
    except Exception:
        # Framework/SQL/TLS diagnostics can contain private deployment metadata.
        parser.exit(1, "Runtime delivery unavailable; original records retained.\n")


if __name__ == "__main__":
    main()
