"""Production composition for synchronous file ASR, outside ``create_app``.

The route accepts one injected ``Transcription`` service. This module owns the
process-wide clients and private admission recipe used by that service. Missing
operator inputs leave the route unavailable, with the exact environment setting
named in the startup log; malformed private files fail startup instead.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable

from fastapi import FastAPI

from speechnexus_backend.app import create_app
from speechnexus_backend.asr_settlement_worker import (
    AsrSettlementRuntime,
    worker_settings,
)
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.transcription import (
    Transcription,
    TranscriptionLimits,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_debt import (
    read_asr_settlement_debt,
)
from speechnexus_backend.speech.infrastructure.audio_duration_probe import PyAvAudioProbe
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    MinioExecutionResultObjects,
)
from speechnexus_backend.speech.infrastructure.gateway_transcription import (
    GatewayTranscriber,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    PostgresPublicJobs,
)
from speechnexus_backend.speech.infrastructure.transcription_admission import (
    PostgresTranscriptionAdmission,
    PostgresTranscriptionAdmissionFacts,
)
from speechnexus_backend.speech.infrastructure.transcription_admission_postgres import (
    PostgresTranscriptionAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.transcription_objects import (
    MinioTranscriptionInputs,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    load_transcription_recipe,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import (
    TranscriptionCommandPlanner,
)
from speechnexus_backend.speech_runtime import build_speech

_LOGGER = logging.getLogger(__name__)


def build_transcription(settings: Settings) -> Transcription | None:
    """Build the explicit production chain, or return ``None`` when unconfigured."""
    missing = [
        name
        for name, configured in (
            (
                "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE",
                settings.transcription_recipe_file is not None,
            ),
            (
                "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
                settings.execution_gateway_base_url is not None,
            ),
            (
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
                settings.execution_gateway_component_credential is not None,
            ),
            (
                "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET",
                settings.transcription_input_bucket is not None,
            ),
            (
                "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY",
                settings.object_storage_access_key is not None,
            ),
            (
                "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY",
                settings.object_storage_secret_key is not None,
            ),
            (
                "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL",
                settings.speech_settlement_database_url is not None,
            ),
        )
        if not configured
    ]
    if missing:
        _LOGGER.warning(
            "Synchronous transcription is unavailable; missing configuration: %s",
            ", ".join(missing),
        )
        return None

    assert settings.transcription_recipe_file is not None
    assert settings.transcription_input_bucket is not None
    assert settings.object_storage_access_key is not None
    assert settings.object_storage_secret_key is not None
    recipe = load_transcription_recipe(settings.transcription_recipe_file)
    pool = speech_pool(
        settings.database_url.get_secret_value(),
        schema=settings.database_schema,
        name="speech-transcription",
    )
    pool.open()
    created: list[object] = []
    try:
        gateway = GatewayExecutionClient.from_settings(settings)
        inputs = MinioTranscriptionInputs(
            endpoint=str(settings.object_storage_endpoint),
            bucket=settings.transcription_input_bucket,
            access_key=settings.object_storage_access_key.get_secret_value(),
            secret_key=settings.object_storage_secret_key.get_secret_value(),
        )
        created.append(inputs)
        results = MinioExecutionResultObjects(
            endpoint=str(settings.object_storage_endpoint),
            bucket=settings.object_storage_bucket,
            access_key=settings.object_storage_access_key.get_secret_value(),
            secret_key=settings.object_storage_secret_key.get_secret_value(),
        )
        created.append(results)
        admission = PostgresTranscriptionAdmission(
            PostgresTranscriptionAdmissionFacts(
                pool, timeout_seconds=settings.transcription_storage_timeout_seconds
            ),
            AdmissionService(
                PostgresTranscriptionAdmissionTransactions(
                    pool,
                    max_command_bytes=settings.transcription_max_command_bytes,
                    enforce_binding_disable=True,
                )
            ),
            recipe,
            max_command_bytes=settings.transcription_max_command_bytes,
            close=pool.close,
        )
        created.append(admission)
        transcriber = GatewayTranscriber(
            gateway,
            admission,
            inputs,
            results,
            TranscriptionCommandPlanner(recipe.asr),
            poll_seconds=settings.transcription_poll_seconds,
        )
        created.append(transcriber)
        settlement = AsrSettlementRuntime(worker_settings(settings))
        created.append(settlement)
        settlement.open()
        return Transcription(
            PostgresPublicJobs(
                pool, timeout_seconds=settings.transcription_storage_timeout_seconds
            ),
            PyAvAudioProbe(),
            transcriber,
            TranscriptionLimits(
                max_audio_ms=settings.transcription_max_audio_ms,
                max_upload_bytes=settings.transcription_max_upload_bytes,
                timeout_seconds=settings.transcription_timeout_seconds,
            ),
            close=settlement.close,
        )
    except BaseException:
        for resource in reversed(created):
            closer = getattr(resource, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    closer()
        if not pool.closed:
            pool.close()
        raise


def _debt_checks(settings: Settings) -> Callable[[], tuple[dict[str, object], ...]]:
    categories = "unknown/cleanup_pending/route_mismatch/settled_but_held/redacted_held"
    try:
        resolved = worker_settings(settings)
    except Exception:
        detail = f"unavailable ({categories}; not configured)"
        return lambda: ({"name": "asr_settlement_debt", "status": "ok", "detail": detail},)
    route = (resolved.gateway_id, resolved.host_node_id)

    def describe() -> tuple[dict[str, object], ...]:
        try:
            debt = read_asr_settlement_debt(resolved.database_url, resolved.schema, route)
            detail = debt.detail()
        except Exception as error:
            detail = (f"unavailable ({categories}; reason={type(error).__name__})")[:512]
        return ({"name": "asr_settlement_debt", "status": "ok", "detail": detail},)

    return describe


def create_app_from_env() -> FastAPI:
    """``uvicorn speechnexus_backend.transcription_runtime:create_app_from_env --factory``."""
    settings = Settings()
    transcriptions = build_transcription(settings)
    speech = None
    try:
        speech = build_speech(settings)
        return create_app(
            settings=settings,
            transcriptions=transcriptions,
            speech=speech,
            diagnostics_checks=_debt_checks(settings),
        )
    except BaseException:
        for service in (speech, transcriptions):
            if service is not None:
                with contextlib.suppress(Exception):
                    service.close()
        raise


__all__ = ["build_transcription", "create_app_from_env"]
