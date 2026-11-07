"""Standalone recorder worker that turns committed validation reports into evidence.

The operator CLI ``speechnexus-record-voice-evidence`` remains the explicit,
single-Job path. This process is its periodic counterpart: it scans durable
succeeded validation Jobs that carry a ``validation_report_artifact_id`` but have
no evidence row yet, reads the committed report object, and records it through
the same recorder transaction. That makes the ``artifact -> result -> evidence``
interruption window self-healing instead of a manual repair.

The identity is the documented ``voice_evidence_recorder_grants`` role: it reads
the Job, its report Artifact, the draft revision and the reference Asset, and it
may append evidence rows only. Startup verifies that exact column-level shape
before scanning, so a deployment that reuses a broader identity fails closed
instead of silently granting itself the ability to rewrite measurements.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
from collections.abc import Callable, Sequence
from threading import Event

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.binding_disable_worker import _verify_no_role_switch
from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceUnavailableError,
)
from speechnexus_backend.speech.application.voice_evidence_recording import (
    CommittedReportArtifacts,
    EvidenceRecordingBatch,
    VoiceEvidenceAutoRecorder,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import MAX_MEASURED_BY
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactObjects
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
    PostgresVoiceEvidenceRecorder,
)
from speechnexus_backend.speech.infrastructure.voice_publication_schema import EVIDENCE_TABLE

_LOG = logging.getLogger(__name__)

# Exactly the SELECT targets of ``voice_evidence_recorder_grants``. The worker
# reads the Job and its committed report, then replays the measured draft
# revision, its binding revision and the reference Asset of a clone draft.
_RECORDER_READ_TABLES = frozenset(
    {
        "speech_jobs",
        "speech_artifacts",
        "speech_voice_drafts",
        "speech_voice_draft_revisions",
        "speech_binding_revisions",
        "speech_assets",
        "speech_asset_objects",
    }
)
_RECORDER_TABLES = _RECORDER_READ_TABLES | {EVIDENCE_TABLE}


class _PoolDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "voice-evidence-" in record.getMessage():
            record.msg, record.args = "Voice evidence database unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class VoiceEvidenceWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL", min_length=1
    )
    database_schema: str = Field(
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA", pattern=r"^[a-z][a-z0-9_]{2,62}$"
    )
    object_storage_endpoint: AnyHttpUrl = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT", repr=False
    )
    artifacts_bucket: str = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_BUCKET",
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    )
    object_storage_access_key: SecretStr = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY", min_length=1, repr=False
    )
    object_storage_secret_key: SecretStr = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY", min_length=1, repr=False
    )
    transaction_timeout_seconds: float = Field(
        default=3,
        gt=0,
        le=30,
        allow_inf_nan=False,
        validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_TRANSACTION_TIMEOUT_SECONDS",
    )
    poll_seconds: float = Field(
        default=5,
        ge=0.1,
        le=300,
        allow_inf_nan=False,
        validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_POLL_SECONDS",
    )
    batch_size: int = Field(
        default=16, ge=1, le=128, validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_BATCH_SIZE"
    )
    max_attempts: int = Field(
        default=5, ge=1, le=32, validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_MAX_ATTEMPTS"
    )
    measured_by: str = Field(
        default="voice-evidence-recorder-worker/v1",
        min_length=1,
        max_length=MAX_MEASURED_BY,
        validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_MEASURED_BY",
    )


def _verify_recorder_worker(pool: SpeechPool, schema: str) -> None:
    """Refuse any identity wider than the documented evidence recorder role.

    Column privileges are compared one column at a time. A table-level grant
    makes every column true, so a per-column comparison still catches it, while
    ``has_table_privilege`` alone would not see a column-level INSERT grant and
    would leave that door open (#549).
    """
    with pool.connection(timeout=3) as connection:
        connection.execute("SET LOCAL statement_timeout='3s'")
        _verify_no_role_switch(connection, "Voice evidence worker")
        identities = connection.execute(
            "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
        ).fetchone()
        if identities is None:
            raise ValueError("Voice evidence worker identity unavailable")
        for role in dict.fromkeys((identities["login"], identities["effective"])):
            _verify_restricted_role(connection, role, schema)
            _verify_columns(connection, role, schema)


def _verify_restricted_role(connection: SpeechConnection, role: str, schema: str) -> None:
    unsafe = connection.execute(
        "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
        "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') OR EXISTS ("
        "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname=%s "
        "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
        "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
        "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
        (role, schema, role, schema, role, role),
    ).fetchone()
    if unsafe is None or unsafe["forbidden"]:
        raise ValueError("Voice evidence worker requires a non-owner identity")


def _verify_columns(connection: SpeechConnection, role: str, schema: str) -> None:
    rows = connection.execute(
        "SELECT c.relname AS table_name, a.attname AS column_name, "
        "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'SELECT') AS can_read, "
        "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'INSERT') AS can_insert, "
        "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'UPDATE') AS can_update, "
        "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'REFERENCES') AS can_reference, "
        "pg_catalog.has_any_column_privilege(%s::name,c.oid,"
        "'SELECT,INSERT,UPDATE,REFERENCES') AS any_column, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,'DELETE,TRUNCATE,TRIGGER') "
        "AS destructive "
        "FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
        "AND a.attnum>0 AND NOT a.attisdropped "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m')",
        (role, role, role, role, role, role, schema),
    ).fetchall()
    seen: set[str] = set()
    for row in rows:
        table = row["table_name"]
        if table not in _RECORDER_TABLES:
            # An unrelated table must stay completely out of reach; this is the
            # one place a table-wide "any column" question is the right one.
            if row["any_column"] or row["destructive"]:
                raise ValueError("Voice evidence worker privileges do not match the recorder")
            continue
        expects_insert = table == EVIDENCE_TABLE
        if (
            bool(row["can_read"]) is not True
            or bool(row["can_insert"]) is not expects_insert
            or row["can_update"]
            or row["can_reference"]
            or row["destructive"]
        ):
            raise ValueError("Voice evidence worker privileges do not match the recorder")
        seen.add(table)
    if seen != _RECORDER_TABLES:
        raise ValueError("Voice evidence worker schema is incomplete")


class _WorkerRuntime:
    """Owned pool, object client and recorder for one process."""

    def __init__(self, settings: VoiceEvidenceWorkerSettings) -> None:
        self._settings = settings
        self._pool = speech_pool(
            settings.database_url.get_secret_value(),
            schema=settings.database_schema,
            max_size=2,
            name="voice-evidence-worker",
        )
        try:
            self._objects = MinioArtifactObjects(
                str(settings.object_storage_endpoint),
                settings.artifacts_bucket,
                settings.object_storage_access_key.get_secret_value(),
                settings.object_storage_secret_key.get_secret_value(),
            )
        except BaseException:
            self._pool.close()
            raise
        self.worker: VoiceEvidenceAutoRecorder | None = None

    def open(self) -> None:
        if self.worker is not None:
            raise ValueError("Voice evidence worker is already open")
        try:
            self._pool.open()
            _verify_recorder_worker(self._pool, self._settings.database_schema)
            store = PostgresVoiceEvidenceRecorder(
                self._pool, timeout_seconds=self._settings.transaction_timeout_seconds
            )
            self.worker = VoiceEvidenceAutoRecorder(
                pending=store,
                reports=CommittedReportArtifacts(store, self._objects),
                recorder=store,
                measured_by=self._settings.measured_by,
                max_attempts=self._settings.max_attempts,
                batch_size=self._settings.batch_size,
            )
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            raise ValueError(
                "Voice evidence worker startup rejected "
                f"({type(error).__name__}); verify grants and object storage"
            ) from None

    def close(self) -> None:
        self.worker = None
        self._objects.close()
        self._pool.close()


class _StopRequested(BaseException):
    """Unwind a bounded pass without taking a non-reentrant event lock."""


def run_worker(
    worker: VoiceEvidenceAutoRecorder,
    stop: Event,
    *,
    interval_seconds: float,
    once: bool = False,
    on_batch: Callable[[EvidenceRecordingBatch], None] | None = None,
    on_unavailable: Callable[[], None] | None = None,
) -> None:
    if type(interval_seconds) not in (int, float) or not 0 < interval_seconds <= 300:
        raise ValueError("Evidence recording requires a bounded interval")
    while not stop.is_set():
        try:
            batch = worker.run_once()
        except VoiceEvidenceUnavailableError:
            if once:
                raise
            if on_unavailable is not None:
                on_unavailable()
            if stop.wait(interval_seconds):
                return
            continue
        if on_batch is not None:
            on_batch(batch)
        if once or stop.wait(interval_seconds):
            return


def _log_batch(batch: EvidenceRecordingBatch) -> None:
    print(json.dumps(batch.as_log(), separators=(",", ":")), flush=True)
    if batch.rejected or batch.retried or batch.exhausted:
        for outcome in batch.outcomes:
            if outcome.outcome != "recorded" and outcome.outcome != "existing":
                print(json.dumps(outcome.as_log(), separators=(",", ":")), flush=True)


def _build(settings: VoiceEvidenceWorkerSettings) -> _WorkerRuntime:
    runtime = _WorkerRuntime(settings)
    runtime.open()
    return runtime


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one bounded pass, then exit")
    options = parser.parse_args(argv)
    try:
        settings = VoiceEvidenceWorkerSettings()
    except ValidationError:
        parser.exit(2, "invalid voice evidence worker configuration\n")
    pool_logger = logging.getLogger("psycopg.pool")
    previous_disabled = pool_logger.disabled
    pool_logger.disabled = True
    pool_logger.addFilter(_PoolDiagnostics())
    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        # Raising instead of Event.set() avoids a deadlock when the signal
        # interrupts Event.wait() while it holds its own lock.
        raise _StopRequested

    runtime: _WorkerRuntime | None = None
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        runtime = _build(settings)
        assert runtime.worker is not None
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        run_worker(
            runtime.worker,
            stop,
            interval_seconds=settings.poll_seconds,
            once=options.once,
            on_batch=_log_batch,
            on_unavailable=lambda: _LOG.warning("Voice evidence storage unavailable"),
        )
    except _StopRequested:
        pass
    except ValueError:
        parser.exit(2, "invalid voice evidence worker configuration or grants\n")
    except VoiceEvidenceUnavailableError:
        parser.exit(1, "voice evidence storage unavailable\n")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if runtime is not None:
            runtime.close()
        pool_logger.disabled = previous_disabled


if __name__ == "__main__":
    main()
