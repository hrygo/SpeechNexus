"""Independently configured durable TTS settlement worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.binding_disable_worker import (
    _verify_pool_pair,
)
from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.application.tts_settlement import TtsSettlementWorker
from speechnexus_backend.speech.domain.gateway_origin import require_safe_gateway_origin
from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.settlement_worker_verifier import (
    verify_settlement_worker,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_postgres import (
    PostgresTtsSettlementStore,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    require_complete_tts_roots,
)

_LOG = logging.getLogger(__name__)


class _PoolDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "tts-settlement-" in record.getMessage():
            record.msg, record.args = "TTS settlement database unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class TtsSettlementWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    admission_database_url: SecretStr = Field(validation_alias="SPEECHNEXUS_DATABASE_URL")
    settlement_database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_TTS_SETTLEMENT_DATABASE_URL"
    )
    database_schema: str = Field(
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA", pattern=r"^[a-z][a-z0-9_]{2,62}$"
    )
    gateway_id: str = Field(validation_alias="SPEECHNEXUS_TTS_GATEWAY_ID")
    node_id: str = Field(validation_alias="SPEECHNEXUS_TTS_HOST_NODE_ID")
    gateway_url: str = Field(validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL")
    allow_plaintext_origin: str = Field(
        default="", validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN"
    )
    credential: SecretStr = Field(
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"
    )
    storage_timeout: float = Field(
        default=3,
        gt=0,
        le=30,
        allow_inf_nan=False,
        validation_alias="SPEECHNEXUS_TTS_SETTLEMENT_STORAGE_TIMEOUT_SECONDS",
    )
    gateway_timeout: float = Field(
        default=5,
        gt=0,
        le=60,
        allow_inf_nan=False,
        validation_alias="SPEECHNEXUS_TTS_SETTLEMENT_GATEWAY_TIMEOUT_SECONDS",
    )
    poll_seconds: float = Field(
        default=1,
        ge=0.1,
        le=300,
        allow_inf_nan=False,
        validation_alias="SPEECHNEXUS_TTS_SETTLEMENT_POLL_SECONDS",
    )
    batch_size: int = Field(
        default=32, ge=1, le=128, validation_alias="SPEECHNEXUS_TTS_SETTLEMENT_BATCH_SIZE"
    )

    @model_validator(mode="after")
    def safe_gateway_origin(self) -> Self:
        require_safe_gateway_origin(
            self.gateway_url,
            allow_plaintext_origin=self.allow_plaintext_origin,
            label="TTS gateway",
        )
        return self


def _verify_worker(pool: SpeechPool, schema: str) -> None:
    """Require a non-owner principal and the shared settlement boundary."""
    verify_settlement_worker(pool, schema, label="TTS settlement worker")


async def run(
    settings: TtsSettlementWorkerSettings,
    *,
    on_ready: Callable[[PostgresTtsSettlementStore], None] | None = None,
    stop: threading.Event | None = None,
) -> None:
    identifier(settings.gateway_id)
    identifier(settings.node_id)
    token = settings.credential.get_secret_value()
    if not 16 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("Invalid gateway credential")
    admission = speech_pool(
        settings.admission_database_url.get_secret_value(),
        schema=settings.database_schema,
        name="tts-settlement-admission-check",
    )
    pool = speech_pool(
        settings.settlement_database_url.get_secret_value(),
        schema=settings.database_schema,
        name="tts-settlement-worker",
    )
    pool_logger = logging.getLogger("psycopg.pool")
    diagnostic = _PoolDiagnostics()
    pool_logger.addFilter(diagnostic)
    try:
        admission.open()
        pool.open()
        await asyncio.to_thread(_verify_pool_pair, admission, pool)
        await asyncio.to_thread(
            require_complete_tts_roots, admission, timeout_seconds=settings.storage_timeout
        )
        await asyncio.to_thread(_verify_worker, pool, settings.database_schema)
        async with GatewayExecutionClient(
            base_url=settings.gateway_url,
            credential=settings.credential,
            timeout_seconds=settings.gateway_timeout,
            max_retries=0,
            backoff_seconds=0,
        ) as gateway:
            store = PostgresTtsSettlementStore(pool, timeout_seconds=settings.storage_timeout)
            worker = TtsSettlementWorker(
                store,
                gateway,
                LeaseService(
                    PostgresLeaseTransactions(pool, timeout_seconds=settings.storage_timeout)
                ),
                gateway_id=settings.gateway_id,
                node_id=settings.node_id,
                batch_size=settings.batch_size,
                poll_seconds=settings.poll_seconds,
            )
            if on_ready is not None:
                on_ready(store)
            while stop is None or not stop.is_set():
                try:
                    await worker.run_once()
                except Exception:
                    _LOG.warning("TTS settlement scan unavailable")
                await asyncio.sleep(settings.poll_seconds)
    finally:
        pool.close()
        admission.close()
        pool_logger.removeFilter(diagnostic)


class TtsSettlementRuntime:
    """Own one worker loop and its isolated connection pool for the API lifespan."""

    def __init__(self, settings: TtsSettlementWorkerSettings) -> None:
        self._settings = settings
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._store: PostgresTtsSettlementStore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread = threading.Thread(
            target=self._thread_main, name="tts-settlement", daemon=True
        )
        self._failed = False

    def _thread_main(self) -> None:
        async def execute() -> None:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            await run(self._settings, on_ready=self._set_ready, stop=self._stop)

        try:
            asyncio.run(execute())
        except BaseException:
            self._failed = True
            self._ready.set()

    def _set_ready(self, store: PostgresTtsSettlementStore) -> None:
        self._store = store
        self._ready.set()

    def open(self) -> PostgresTtsSettlementStore:
        self._thread.start()
        if not self._ready.wait(15) or self._failed or self._store is None:
            self.close()
            raise ValueError("TTS settlement worker failed to start")
        return self._store

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._loop is not None and self._task is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread.is_alive():
            self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise RuntimeError("TTS settlement worker failed to stop")


if __name__ == "__main__":
    try:
        asyncio.run(run(TtsSettlementWorkerSettings()))
    except Exception:
        raise SystemExit(
            "TTS settlement worker unavailable; verify configuration and permissions"
        ) from None
