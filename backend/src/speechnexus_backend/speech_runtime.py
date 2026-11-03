"""Production composition for synchronous file TTS with qualified builtin voices."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.speech_synthesis import SpeechLimits, SpeechSynthesis
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
    require_builtin_voice,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    MinioExecutionResultObjects,
)
from speechnexus_backend.speech.infrastructure.gateway_synthesis import GatewaySynthesizer
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.qualified_speech_facts_postgres import (
    QualifiedPostgresSpeechFacts,
)
from speechnexus_backend.speech.infrastructure.tts_admission import (
    PostgresOriginalTtsCommands,
    PostgresTtsAdmission,
    PostgresTtsFacts,
)
from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    require_complete_tts_roots,
)
from speechnexus_backend.tts_settlement_worker import (
    TtsSettlementRuntime,
    TtsSettlementWorkerSettings,
)

_LOGGER = logging.getLogger(__name__)


def build_speech(settings: Settings) -> SpeechSynthesis | None:
    """Compose only explicit deployment inputs and an independently qualified voice."""
    required = (
        ("SPEECHNEXUS_SPEECH_RECIPE_FILE", settings.speech_recipe_file),
        ("SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE", settings.speech_default_language),
        ("SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL", settings.speech_settlement_database_url),
        ("SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL", settings.execution_gateway_base_url),
        (
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
            settings.execution_gateway_component_credential,
        ),
        ("SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY", settings.object_storage_access_key),
        ("SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY", settings.object_storage_secret_key),
    )
    missing = [name for name, value in required if value is None]
    if missing:
        _LOGGER.warning(
            "Synchronous speech unavailable; missing configuration: %s", ", ".join(missing)
        )
        return None
    assert settings.speech_recipe_file is not None
    assert settings.object_storage_access_key is not None
    assert settings.object_storage_secret_key is not None
    recipe = load_tts_recipe(settings.speech_recipe_file)
    if recipe.approved.admission_expires_at_us <= time.time_ns() // 1_000:
        _LOGGER.warning("Synchronous speech unavailable; private recipe expired")
        return None
    pool = speech_pool(
        settings.database_url.get_secret_value(),
        schema=settings.database_schema,
        name="speech-synthesis",
    )
    results: MinioExecutionResultObjects | None = None
    gateway: GatewayExecutionClient | None = None
    synthesizer: GatewaySynthesizer | None = None
    settlement: TtsSettlementRuntime | None = None
    try:
        pool.open()
        require_complete_tts_roots(pool, timeout_seconds=settings.speech_storage_timeout_seconds)
        facts = QualifiedPostgresSpeechFacts(
            pool, recipe, timeout_seconds=settings.speech_storage_timeout_seconds
        )
        try:
            qualified = facts.has_qualified_voice()
        except PublicationUnavailableError:
            qualified = False
        if not qualified:
            _LOGGER.warning("Synchronous speech unavailable; no current qualified voice")
            pool.close()
            return None
        binding = recipe.approved.stages[0].runtime_plan.binding
        worker_settings = TtsSettlementWorkerSettings.model_validate(
            {
                "SPEECHNEXUS_DATABASE_URL": settings.database_url,
                "SPEECHNEXUS_TTS_SETTLEMENT_DATABASE_URL": settings.speech_settlement_database_url,
                "SPEECHNEXUS_DATABASE_SCHEMA": settings.database_schema,
                "SPEECHNEXUS_TTS_GATEWAY_ID": binding.gateway_id,
                "SPEECHNEXUS_TTS_HOST_NODE_ID": binding.target_node_id,
                "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": str(settings.execution_gateway_base_url),
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": (
                    settings.execution_gateway_component_credential
                ),
            }
        )
        settlement = TtsSettlementRuntime(worker_settings)
        cancellation = settlement.open()
        admission = PostgresTtsAdmission(
            PostgresTtsFacts(pool, timeout_seconds=settings.speech_storage_timeout_seconds),
            AdmissionService(
                PostgresAdmissionTransactions(
                    pool,
                    max_command_bytes=settings.speech_max_command_bytes,
                    timeout_seconds=settings.speech_storage_timeout_seconds,
                    enforce_binding_disable=True,
                    require_builtin_voice=require_builtin_voice,
                )
            ),
            PostgresOriginalTtsCommands(
                pool, timeout_seconds=settings.speech_storage_timeout_seconds
            ),
            recipe,
            max_command_bytes=settings.speech_max_command_bytes,
            default_language=settings.speech_default_language,
            close=pool.close,
        )
        results = MinioExecutionResultObjects(
            endpoint=str(settings.object_storage_endpoint),
            bucket=settings.object_storage_bucket,
            access_key=settings.object_storage_access_key.get_secret_value(),
            secret_key=settings.object_storage_secret_key.get_secret_value(),
            max_bytes=settings.speech_max_payload_bytes,
        )
        gateway = GatewayExecutionClient.from_settings(settings)
        synthesizer = GatewaySynthesizer(
            gateway,
            admission,
            results,
            poll_seconds=settings.speech_poll_seconds,
            cancellation=cancellation,
            settlement_runtime=settlement,
        )
        return SpeechSynthesis(
            facts,
            synthesizer,
            SpeechLimits(
                max_output_audio_ms=settings.speech_max_output_audio_ms,
                max_payload_bytes=settings.speech_max_payload_bytes,
                timeout_seconds=settings.speech_timeout_seconds,
            ),
        )
    except BaseException:
        if synthesizer is not None:
            with contextlib.suppress(Exception):
                synthesizer.close()
        else:
            if gateway is not None:
                # A newly created client has not yet been used on the gateway loop.
                with contextlib.suppress(Exception), ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(lambda: asyncio.run(gateway.aclose())).result()
            if results is not None:
                with contextlib.suppress(Exception):
                    results.close()
            if settlement is not None:
                with contextlib.suppress(Exception):
                    settlement.close()
            if not pool.closed:
                pool.close()
        raise


__all__ = ["build_speech"]
