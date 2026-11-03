"""Recover one original synchronous TTS operation without current catalog authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.domain.speech_synthesis import SpeechAudio
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    MinioExecutionResultObjects,
)
from speechnexus_backend.speech.infrastructure.gateway_synthesis import GatewaySynthesizer
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.tts_admission import (
    OriginalTtsRecovery,
    PostgresOriginalTtsCommands,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    require_complete_tts_roots,
)


class TtsRecoverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = Field(validation_alias="SPEECHNEXUS_DATABASE_URL")
    database_schema: str = Field(
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA", pattern=r"^[a-z][a-z0-9_]{2,62}$"
    )
    gateway_url: AnyHttpUrl = Field(validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL")
    gateway_credential: SecretStr = Field(
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"
    )
    object_endpoint: AnyHttpUrl = Field(validation_alias="SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT")
    object_bucket: str = Field(validation_alias="SPEECHNEXUS_OBJECT_STORAGE_BUCKET", min_length=3)
    object_access_key: SecretStr = Field(validation_alias="SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY")
    object_secret_key: SecretStr = Field(validation_alias="SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY")


class _PoolDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "tts-original-recovery" in record.getMessage():
            record.msg, record.args = "TTS recovery database unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def recover(
    settings: TtsRecoverySettings, request_id: str, *, timeout_seconds: float
) -> SpeechAudio:
    """Only replay an original persisted command; no payload leaves this process."""
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError("Recovery requires a bounded timeout")
    deadline = time.monotonic() + timeout_seconds
    parsed = urlsplit(str(settings.gateway_url))
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Recovery gateway requires TLS outside loopback")
    pool = speech_pool(
        settings.database_url.get_secret_value(),
        schema=settings.database_schema,
        name="tts-original-recovery",
    )
    objects: MinioExecutionResultObjects | None = None
    gateway: GatewayExecutionClient | None = None
    synthesizer: GatewaySynthesizer | None = None
    pool_logger = logging.getLogger("psycopg.pool")
    diagnostic = _PoolDiagnostics()
    pool_logger.addFilter(diagnostic)
    try:
        pool.open()
        require_complete_tts_roots(pool, timeout_seconds=min(3, deadline - time.monotonic()))
        admission = OriginalTtsRecovery(
            PostgresOriginalTtsCommands(pool),
            AdmissionService(PostgresAdmissionTransactions(pool, max_command_bytes=1_048_576)),
        )
        objects = MinioExecutionResultObjects(
            endpoint=str(settings.object_endpoint),
            bucket=settings.object_bucket,
            access_key=settings.object_access_key.get_secret_value(),
            secret_key=settings.object_secret_key.get_secret_value(),
            max_bytes=64 * 1024 * 1024,
        )
        gateway = GatewayExecutionClient(
            base_url=str(settings.gateway_url),
            credential=settings.gateway_credential,
            timeout_seconds=5,
            max_retries=0,
            backoff_seconds=0,
        )
        synthesizer = GatewaySynthesizer(gateway, admission, objects)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("TTS recovery budget expired")
        return synthesizer.recover(request_id, timeout_seconds=remaining)
    finally:
        try:
            if synthesizer is not None:
                synthesizer.close()
            else:
                try:
                    if objects is not None:
                        objects.close()
                finally:
                    if gateway is not None:
                        import asyncio

                        asyncio.run(gateway.aclose())
        finally:
            pool.close()
            pool_logger.removeFilter(diagnostic)


def write_private_audio(path: Path, audio: SpeechAudio) -> dict[str, object]:
    """Create a private result exactly once and remove our own partial file on failure."""
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise ValueError("Output must be an absolute file path")
    parent = path.parent
    if parent.resolve(strict=True) != parent:
        raise ValueError("Output directory must not contain symlinks")
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Output directory must be private and owned by the caller")
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        identity = os.fstat(descriptor)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                descriptor = -1
                file.write(audio.payload)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(path.name, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(directory)
    return {
        "result": "complete",
        "bytes": len(audio.payload),
        "sha256": hashlib.sha256(audio.payload).hexdigest(),
        "format": audio.response_format.value,
        "sample_rate_hz": audio.sample_rate_hz,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover one persisted TTS operation")
    parser.add_argument("request_id", help="server-generated original speech request identity")
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        audio = recover(
            TtsRecoverySettings(), args.request_id, timeout_seconds=args.timeout_seconds
        )
        summary = write_private_audio(args.output_file, audio)
    except Exception:
        raise SystemExit(
            "TTS recovery unavailable; original identity needs reconciliation"
        ) from None
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
