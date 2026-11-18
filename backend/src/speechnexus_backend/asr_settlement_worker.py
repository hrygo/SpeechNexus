"""Dedicated synchronous ASR settlement entry point and shared worker factory."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

from pydantic import SecretStr

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.asr_settlement import (
    AsrExecutionQuery,
    AsrSettlementWorker,
)
from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.infrastructure.asr_settlement_postgres import (
    PostgresAsrSettlementStore,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    TranscriptionRecipe,
    load_transcription_recipe,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AsrSettlementWorkerSettings:
    database_url: str
    schema: str
    gateway_id: str
    host_node_id: str
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
    return AsrSettlementWorkerSettings(
        database_url=settings.speech_settlement_database_url.get_secret_value(),
        schema=settings.database_schema,
        gateway_id=binding.gateway_id,
        host_node_id=binding.target_node_id,
        poll_seconds=settings.asr_settlement_poll_seconds,
        batch_size=settings.asr_settlement_batch_size,
        storage_timeout_seconds=settings.asr_settlement_storage_timeout_seconds,
        gateway_url=str(settings.execution_gateway_base_url),
        gateway_credential=settings.execution_gateway_component_credential,
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
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single settlement pass; the periodic runtime is started by the API",
    )
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("this entry point only supports --once")
    logging.basicConfig(level=logging.INFO)
    try:
        resolved = worker_settings(Settings())
        scanned, released, pending = asyncio.run(run_once(resolved))
    except ValueError as error:
        _LOG.error("ASR settlement is unavailable: %s", error)
        return 2
    print(f"asr settlement: scanned={scanned} released={released} pending={pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
