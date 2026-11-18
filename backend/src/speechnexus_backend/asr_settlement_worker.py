"""Dedicated synchronous ASR settlement entry point and shared worker factory."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from speechnexus_backend.binding_disable_worker import _verify_pool_pair
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.asr_settlement import (
    AsrExecutionQuery,
    AsrSettlementWorker,
)
from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.infrastructure.asr_settlement_postgres import (
    PostgresAsrSettlementStore,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_preflight import (
    require_complete_asr_roots,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.settlement_worker_verifier import (
    verify_settlement_worker,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    TranscriptionRecipe,
    load_transcription_recipe,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    SettlementPreflightUnavailableError,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AsrSettlementWorkerSettings:
    database_url: str
    schema: str
    gateway_id: str
    host_node_id: str
    admission_database_url: str = ""
    poll_seconds: float = 1
    batch_size: int = 32
    storage_timeout_seconds: float = 3
    gateway_url: str = ""
    gateway_credential: SecretStr | None = None
    gateway_timeout_seconds: float = 10
    gateway_retries: int = 2


def worker_settings(settings: Settings) -> AsrSettlementWorkerSettings:
    """Derive the route from the same private recipe the admission path uses."""
    if settings.speech_settlement_database_url is None:
        raise ValueError("ASR settlement requires SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL")
    if settings.transcription_recipe_file is None:
        raise ValueError("ASR settlement requires SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE")
    recipe: TranscriptionRecipe = load_transcription_recipe(settings.transcription_recipe_file)
    binding = recipe.approved.stages[0].runtime_plan.binding
    if settings.execution_gateway_base_url is None:
        raise ValueError("ASR settlement requires SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL")
    if settings.execution_gateway_component_credential is None:
        raise ValueError(
            "ASR settlement requires SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"
        )
    credential = settings.execution_gateway_component_credential
    return AsrSettlementWorkerSettings(
        database_url=settings.speech_settlement_database_url.get_secret_value(),
        admission_database_url=settings.database_url.get_secret_value(),
        schema=settings.database_schema,
        gateway_id=binding.gateway_id,
        host_node_id=binding.target_node_id,
        poll_seconds=settings.asr_settlement_poll_seconds,
        batch_size=settings.asr_settlement_batch_size,
        storage_timeout_seconds=settings.asr_settlement_storage_timeout_seconds,
        gateway_url=str(settings.execution_gateway_base_url),
        gateway_credential=credential,
        gateway_timeout_seconds=settings.execution_gateway_timeout_seconds,
        gateway_retries=settings.execution_gateway_max_retries,
    )


def settlement_pool(settings: AsrSettlementWorkerSettings) -> SpeechPool:
    return speech_pool(
        settings.database_url,
        schema=settings.schema,
        name="asr-settlement",
    )


def build_worker(
    settings: AsrSettlementWorkerSettings,
    pool: SpeechPool,
    gateway: AsrExecutionQuery,
) -> AsrSettlementWorker:
    """One factory for ``--once`` and the in-process runtime: one release path."""
    return AsrSettlementWorker(
        LeaseService(PostgresLeaseTransactions(pool)),
        PostgresAsrSettlementStore(pool, timeout_seconds=settings.storage_timeout_seconds),
        gateway,
        gateway_id=settings.gateway_id,
        host_node_id=settings.host_node_id,
        poll_seconds=settings.poll_seconds,
        batch_size=settings.batch_size,
    )


async def run_once(
    resolved: AsrSettlementWorkerSettings,
    *,
    gateway: AsrExecutionQuery | None = None,
) -> tuple[int, int, int]:
    """One ``--once`` body; tests inject only the external gateway transport."""
    pool = settlement_pool(resolved)
    pool.open()
    client = gateway
    if client is None:
        if resolved.gateway_credential is None or not resolved.gateway_url:
            pool.close()
            raise ValueError("ASR settlement requires the execution-gateway URL and credential")
        client = GatewayExecutionClient(
            base_url=resolved.gateway_url,
            credential=resolved.gateway_credential,
            timeout_seconds=resolved.gateway_timeout_seconds,
            max_retries=resolved.gateway_retries,
            backoff_seconds=0,
        )
    try:
        worker = build_worker(resolved, pool, client)
        result = await worker.run_once()
    finally:
        if gateway is None and isinstance(client, GatewayExecutionClient):
            await client.aclose()
        pool.close()
    return result.scanned, result.released, result.pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Settle synchronous ASR admission leases")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--once",
        action="store_true",
        help="run a single settlement pass; the periodic runtime is started by the API",
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="verify the startup boundary (pools, preflight, privileges) without settling",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        resolved = worker_settings(Settings())
        if args.check:
            asyncio.run(check(resolved))
            print("asr settlement: check ok")
            return 0
        scanned, released, pending = asyncio.run(run_once(resolved))
    except (SettlementPreflightUnavailableError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"asr settlement: scanned={scanned} released={released} pending={pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


async def check(settings: AsrSettlementWorkerSettings) -> None:
    """Open both pools and prove the boundary without reading a lease."""
    if not settings.admission_database_url:
        raise ValueError("ASR settlement requires the admission database URL")
    admission = speech_pool(
        settings.admission_database_url,
        schema=settings.schema,
        name="asr-settlement-admission-check",
    )
    pool = settlement_pool(settings)
    try:
        admission.open()
        await asyncio.to_thread(
            require_complete_asr_roots, admission, timeout_seconds=settings.storage_timeout_seconds
        )
        pool.open()
        await asyncio.to_thread(
            verify_settlement_worker, pool, settings.schema, label="ASR settlement worker"
        )
        await asyncio.to_thread(_verify_pool_pair, admission, pool)
    finally:
        pool.close()
        admission.close()


async def run(
    settings: AsrSettlementWorkerSettings,
    *,
    on_ready: Callable[[], None] | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Start both restricted pools, prove the boundary, then settle in a loop."""
    if not settings.admission_database_url:
        raise ValueError("ASR settlement requires the admission database URL")
    admission = speech_pool(
        settings.admission_database_url,
        schema=settings.schema,
        name="asr-settlement-admission-check",
    )
    pool = settlement_pool(settings)
    try:
        admission.open()
        await asyncio.to_thread(
            require_complete_asr_roots, admission, timeout_seconds=settings.storage_timeout_seconds
        )
        pool.open()
        await asyncio.to_thread(
            verify_settlement_worker, pool, settings.schema, label="ASR settlement worker"
        )
        await asyncio.to_thread(_verify_pool_pair, admission, pool)
        if settings.gateway_credential is None:
            raise ValueError("ASR settlement requires an execution-gateway credential")
        async with GatewayExecutionClient(
            base_url=settings.gateway_url,
            credential=settings.gateway_credential,
            timeout_seconds=settings.gateway_timeout_seconds,
            max_retries=0,
            backoff_seconds=0,
        ) as gateway:
            worker = build_worker(settings, pool, gateway)
            if on_ready is not None:
                on_ready()
            while stop is None or not stop.is_set():
                try:
                    await worker.run_once()
                except Exception:
                    _LOG.warning("ASR settlement scan unavailable")
                await asyncio.sleep(settings.poll_seconds)
    finally:
        pool.close()
        admission.close()


class AsrSettlementRuntime:
    """Own one ASR settlement loop and its isolated pool for the API lifespan."""

    def __init__(self, settings: AsrSettlementWorkerSettings) -> None:
        self._settings = settings
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread = threading.Thread(
            target=self._thread_main, name="asr-settlement", daemon=True
        )
        self._failed: BaseException | None = None

    def _thread_main(self) -> None:
        async def execute() -> None:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            await run(self._settings, on_ready=self._ready.set, stop=self._stop)

        try:
            asyncio.run(execute())
        except BaseException as error:
            self._failed = error
            self._ready.set()

    def open(self) -> None:
        self._thread.start()
        if not self._ready.wait(15) or self._failed is not None:
            failure = self._failed
            self.close()
            if failure is not None:
                raise failure
            raise ValueError("ASR settlement worker failed to start")

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._loop is not None and self._task is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread.is_alive():
            self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise RuntimeError("ASR settlement worker failed to stop")
