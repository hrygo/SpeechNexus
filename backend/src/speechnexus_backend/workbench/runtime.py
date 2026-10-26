"""Independent workbench ASGI factory with real PostgreSQL adapters and no runtime DDL."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI
from psycopg import sql
from psycopg.rows import dict_row

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.assistant import WorkbenchAssistant
from speechnexus_backend.workbench.application.books import WorkbenchBooks
from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.application.downloads import WorkbenchDownloads
from speechnexus_backend.workbench.application.export_worker import ExportProducer, ExportWorker
from speechnexus_backend.workbench.application.exports import WorkbenchExports
from speechnexus_backend.workbench.application.meetings import WorkbenchMeetings
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks
from speechnexus_backend.workbench.application.recovery import WorkbenchRecovery
from speechnexus_backend.workbench.application.render_copies import WorkbenchRenderResultCopies
from speechnexus_backend.workbench.application.render_reconcile import RenderReconciler
from speechnexus_backend.workbench.application.render_units import WorkbenchRenderUnits
from speechnexus_backend.workbench.application.saved_assets import WorkbenchSavedAssets
from speechnexus_backend.workbench.application.teleprompter import WorkbenchTeleprompter
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.application.voices import WorkbenchVoices
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.export_media_validation import (
    ExportMediaValidator,
    ExportValidationLimits,
)
from speechnexus_backend.workbench.infrastructure.minio_object_zone import (
    MinioDownloadReader,
    MinioObjectZone,
)
from speechnexus_backend.workbench.infrastructure.openai_text_llm import (
    OpenAiTextLlm,
)
from speechnexus_backend.workbench.infrastructure.postgres_assistant_turns import (
    PostgresAssistantTurns,
)
from speechnexus_backend.workbench.infrastructure.postgres_books import PostgresBooks
from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
    PostgresDeletionCleanup,
)
from speechnexus_backend.workbench.infrastructure.postgres_deletions import (
    PostgresDeletionUnitOfWork,
)
from speechnexus_backend.workbench.infrastructure.postgres_downloads import PostgresDownloads
from speechnexus_backend.workbench.infrastructure.postgres_exports import PostgresExports
from speechnexus_backend.workbench.infrastructure.postgres_meetings import PostgresMeetings
from speechnexus_backend.workbench.infrastructure.postgres_minutes_tasks import (
    PostgresMinutesTasks,
)
from speechnexus_backend.workbench.infrastructure.postgres_owned_deletions import (
    PostgresOwnedDeletionCatalog,
)
from speechnexus_backend.workbench.infrastructure.postgres_render_units import (
    PostgresRenderUnits,
)
from speechnexus_backend.workbench.infrastructure.postgres_saved_assets import PostgresSavedAssets
from speechnexus_backend.workbench.infrastructure.postgres_teleprompter import (
    PostgresTeleprompter,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import PostgresVersionUnitOfWork
from speechnexus_backend.workbench.infrastructure.public_artifact_source import (
    PublicArtifactSource,
)
from speechnexus_backend.workbench.infrastructure.public_speech_render_jobs import (
    PublicSpeechRenderJobs,
)
from speechnexus_backend.workbench.infrastructure.public_voice_catalogue import (
    PublicVoiceCatalogue,
)
from speechnexus_backend.workbench.infrastructure.runtime_schema import SCHEMA_REVISION, TABLES
from speechnexus_backend.workbench.interfaces.runtime_ingress import RuntimeIngress
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

_READ_ONLY = frozenset({"erased_mutation_receipts", "deletion_ledger", "runtime_schema_revision"})
_INSERT = TABLES - _READ_ONLY
_UPDATE_COLUMNS = {
    "content_write_intents": {
        "token",
        "intent",
        "state",
        "object_key",
        "sha256",
        "bytes",
        "updated_at",
    },
    "assistant_turns": {
        "error",
        "output_text",
        "playback_generation",
        "revision",
        "speech_job_id",
        "status",
        "updated_at",
    },
    "export_jobs": {
        "artifact",
        "error",
        "progress_completed",
        "progress_total",
        "status",
        "updated_at",
    },
    "render_units": {
        "dependencies_sha256",
        "model",
        "model_binding_id",
        "model_binding_revision",
        "revision",
        "saved_asset_id",
        "source_artifact_id",
        "source_receipt_id",
        "speech_job_id",
        "status",
        "text",
        "updated_at",
        "voice_id",
        "voice_revision",
    },
    "projects": {"metadata_revision"},
    "transcripts": {"head_revision"},
    "meetings": {
        "version",
        "updated_at",
        "ended_at",
        "sealed_revision",
        "sealed_revision_id",
        "source_sha256",
        "minutes_id",
    },
    "minutes_tasks": {
        "status",
        "attempt",
        "attempt_token",
        "version",
        "updated_at",
        "result_text",
        "error_code",
    },
    "minutes_attempts": {
        "outcome",
        "finished_at",
        "input_tokens",
        "output_tokens",
        "completion_digest",
        "completion_confirmed",
    },
    "teleprompter_states": {
        "script_revision",
        "visible_position",
        "manual_hold",
        "updated_at",
    },
}


_log = logging.getLogger(__name__)

_MINUTES_BATCH = 8
_EXPORT_BATCH = 4
_RENDER_SETTLE_BATCH = 12


class RuntimeStartupError(RuntimeError):
    def __init__(self) -> None:
        # Driver messages can contain connection strings, object names or SQL.
        super().__init__("Workbench runtime database is unavailable or unsafe.")


async def check_database(settings: WorkbenchSettings) -> None:
    """Read-only, bounded preflight; never create roles, grant, migrate or repair."""
    connection: psycopg.AsyncConnection[dict[str, Any]] | None = None
    try:
        async with asyncio.timeout(5):
            connection = await psycopg.AsyncConnection.connect(
                settings.database_url.get_secret_value(),
                autocommit=True,
                connect_timeout=3,
                row_factory=dict_row,
            )
            # Never inherit a role switch or search_path from the DSN, PGOPTIONS or a
            # service file: the checked identity must be the login identity that every
            # business connection also resets to, resolved only inside pg_catalog.
            # RESET ROLE is a no-op for a startup-packet -c role=; SET ROLE NONE
            # always restores session_user.
            await connection.execute("SET ROLE NONE")
            await connection.execute("SET search_path = pg_catalog")
            await connection.execute("SET statement_timeout = 3000")
            await connection.execute("BEGIN READ ONLY")
            identity = await (
                await connection.execute(
                    """
                    SELECT session_user AS login, current_user AS effective,
                        r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
                        OR r.rolbypassrls OR pg_has_role(n.nspowner,'MEMBER')
                        OR pg_has_role(d.datdba,'MEMBER')
                        OR has_schema_privilege(n.oid,'CREATE') AS unsafe,
                        has_schema_privilege(n.oid,'USAGE') AS usable
                    FROM pg_roles r, pg_namespace n, pg_database d
                    WHERE r.rolname=session_user AND n.nspname=%s
                        AND d.datname=current_database()
                    """,
                    (settings.database_schema,),
                )
            ).fetchone()
            if (
                identity is None
                or identity["login"] != identity["effective"]
                or identity["unsafe"]
                or not identity["usable"]
            ):
                raise RuntimeStartupError
            rows = await (
                await connection.execute(
                    """
                    SELECT c.relname AS name,c.relkind AS kind,
                        pg_has_role(c.relowner,'MEMBER') AS owned,
                        has_table_privilege(c.oid,'SELECT') AS readable,
                        has_table_privilege(c.oid,'INSERT') AS insertable,
                        has_any_column_privilege(c.oid,'INSERT') AS any_insert,
                        has_table_privilege(c.oid,'DELETE,TRUNCATE,TRIGGER') AS destructive
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')
                    """,
                    (settings.database_schema,),
                )
            ).fetchall()
            if {row["name"] for row in rows} != TABLES:
                raise RuntimeStartupError
            for row in rows:
                name = row["name"]
                if (
                    row["kind"] != "r"
                    or row["owned"]
                    or row["destructive"]
                    or not row["readable"]
                    or (name in _INSERT and not row["insertable"])
                    or (name in _READ_ONLY and row["any_insert"])
                ):
                    raise RuntimeStartupError
            updates = await (
                await connection.execute(
                    """
                    SELECT c.relname AS name,a.attname AS column
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                        JOIN pg_attribute a ON a.attrelid=c.oid
                    WHERE n.nspname=%s AND a.attnum>0 AND NOT a.attisdropped
                        AND c.relkind='r' AND has_column_privilege(c.oid,a.attnum,'UPDATE')
                    """,
                    (settings.database_schema,),
                )
            ).fetchall()
            if {(row["name"], row["column"]) for row in updates} != {
                (table, column) for table, columns in _UPDATE_COLUMNS.items() for column in columns
            }:
                raise RuntimeStartupError
            marker = await (
                await connection.execute(
                    sql.SQL("SELECT singleton,revision FROM {}.runtime_schema_revision").format(
                        sql.Identifier(settings.database_schema)
                    )
                )
            ).fetchall()
            if marker != [{"singleton": True, "revision": SCHEMA_REVISION}]:
                raise RuntimeStartupError
    except psycopg.Error, TimeoutError, ValueError, TypeError, KeyError:
        raise RuntimeStartupError from None
    finally:
        if connection is not None:
            # Disconnect the read-only transaction without an unbounded rollback.
            connection.pgconn.finish()


async def check_cleanup_identity(settings: WorkbenchSettings) -> None:
    """The ledger replay identity must stay narrow and explicit.

    Recovery erases confirmed content, so it must never run as the schema owner or any
    privileged role: a cleanup DSN pointed at the owner would silently bypass the narrow
    grants a deletion worker is supposed to hold.
    """
    if settings.cleanup_database_url is None:
        return
    connection: psycopg.AsyncConnection[dict[str, Any]] | None = None
    try:
        async with asyncio.timeout(5):
            connection = await psycopg.AsyncConnection.connect(
                settings.cleanup_database_url.get_secret_value(),
                autocommit=True,
                connect_timeout=3,
                row_factory=dict_row,
            )
            # Judge the login identity, not a role a DSN or PGOPTIONS could select.
            await connection.execute("SET ROLE NONE")
            await connection.execute("SET search_path = pg_catalog")
            identity = await (
                await connection.execute(
                    """
                    SELECT session_user AS login, current_user AS effective,
                        r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
                        OR r.rolbypassrls OR pg_has_role(n.nspowner,'MEMBER')
                        OR pg_has_role(d.datdba,'MEMBER')
                        OR pg_has_role(r.oid,'pg_read_all_data','MEMBER')
                        OR pg_has_role(r.oid,'pg_write_all_data','MEMBER') AS unsafe,
                        has_schema_privilege(n.oid,'USAGE') AS usable
                    FROM pg_roles r, pg_namespace n, pg_database d
                    WHERE r.rolname=session_user AND n.nspname=%s
                        AND d.datname=current_database()
                    """,
                    (settings.database_schema,),
                )
            ).fetchone()
            if (
                identity is None
                or identity["login"] != identity["effective"]
                or identity["unsafe"]
                or not identity["usable"]
            ):
                raise RuntimeStartupError
    except psycopg.Error, TimeoutError, ValueError, TypeError, KeyError:
        raise RuntimeStartupError from None
    finally:
        if connection is not None:
            connection.pgconn.finish()


def _object_zone(settings: WorkbenchSettings) -> MinioObjectZone | None:
    if settings.object_endpoint is None:
        return None
    assert settings.object_bucket is not None
    assert settings.object_access_key is not None
    assert settings.object_secret_key is not None
    return MinioObjectZone(
        settings.object_endpoint,
        settings.object_bucket,
        settings.object_access_key.get_secret_value(),
        settings.object_secret_key.get_secret_value(),
        prefix=settings.object_prefix,
    )


def _speech_client(settings: WorkbenchSettings) -> httpx.AsyncClient | None:
    if settings.speech_base_url is None:
        return None
    # A fixed base URL, no redirect following and bounded reads: artifact copies never
    # reach a client-supplied host. A private address is not itself an egress proof.
    return httpx.AsyncClient(
        base_url=settings.speech_base_url,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=3, read=60, write=60, pool=3),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )


async def _sweep_deletions(recovery: WorkbenchRecovery, interval: float) -> None:
    """Keep the deletion promise while serving; cancellation ends the loop.

    A failed pass is logged and retried: the ledger still records what is owed, and the
    next tick or the next start replays it.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await recovery.run()
        except Exception:
            # Adapter exceptions can contain content, SQL or private endpoints.
            _log.error("deletion sweep failed; the ledger still records what is owed")


async def _dispatch_minutes_once(minutes: MinutesTasks, workspace_id: str) -> None:
    """Dispatch one bounded page; a failed read or task leaves the queue as it was."""
    try:
        queued = await minutes.queued_ids(workspace_id, limit=_MINUTES_BATCH)
    except Exception:
        _log.error("minutes queue could not be read; the next pass retries")
        return
    for task_id in queued:
        try:
            await minutes.execute(task_id)
        except asyncio.CancelledError:
            raise
        except MinutesTaskConflictError:
            # Another instance holds the claim; the queue still records one outcome.
            _log.info("minutes task %s is claimed elsewhere", task_id)
        except Exception:
            _log.error("minutes task %s did not finish in this pass", task_id)


async def _dispatch_minutes(minutes: MinutesTasks, workspace_id: str, interval: float) -> None:
    """Dispatch queued Minutes work while serving; cancellation ends the loop.

    Each pass reads the durable queue again, so a restart, a failed pass or an operator
    cancel cannot leave work claimed without a recorded outcome.
    """
    while True:
        await asyncio.sleep(interval)
        await _dispatch_minutes_once(minutes, workspace_id)


async def _dispatch_exports(worker: ExportWorker, interval: float) -> None:
    """Produce queued exports while serving; cancellation ends the loop.

    The queue is the durable record: a pass claims a bounded page, publishes object
    bytes before the database reference and records outcomes for completed attempts.
    A failed pass is diagnosed without exception content. This loop only claims
    queued jobs; running claims and unknown completions need separate recovery.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await worker.run_once(limit=_EXPORT_BATCH)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Adapter failures may contain content or private service details.
            _log.error("export pass failed; inspect durable export state before retrying")


async def _settle_render_generations(
    reconciler: RenderReconciler, workspace_id: str, interval: float
) -> None:
    """Apply terminal speech Job outcomes to their generating units.

    A pass reads the durable unit rows, so a restart cannot forget an attempt; an
    unreadable Job leaves the unit generating for the next pass.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await reconciler.settle(workspace_id=workspace_id, limit=_RENDER_SETTLE_BATCH)
            await reconciler.restore_copies(
                workspace_id=workspace_id, limit=_RENDER_SETTLE_BATCH
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.error("render settlement pass failed; units stay generating")


def _deletion_recovery(
    settings: WorkbenchSettings, objects: MinioObjectZone | None
) -> WorkbenchRecovery | None:
    """Startup replay of accepted deletions, when a cleanup identity is configured."""
    if settings.cleanup_database_url is None:
        return None
    worker = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(
            settings.cleanup_database_url.get_secret_value(),
            schema=settings.database_schema,
            max_connections=2,
        ),
        meetings=True,
        objects=objects,
        owned=PostgresOwnedDeletionCatalog(),
    )
    return WorkbenchRecovery(worker, worker)


def create_runtime_app(
    settings: WorkbenchSettings, *, export_producer: ExportProducer | None = None
) -> FastAPI:
    """Construct without I/O; the server must execute ASGI lifespan before accepting requests."""
    versions = PostgresVersionUnitOfWork(
        settings.database_url.get_secret_value(),
        schema=settings.database_schema,
        max_connections=settings.max_connections,
    )
    objects = _object_zone(settings)
    client = _speech_client(settings)
    # The intranet text LLM stays off until the deployment names an approved loopback or
    # private endpoint, a pinned model and the qualification it relies on. The adapter
    # checks the endpoint, so a public host fails at startup rather than at first use.
    llm = OpenAiTextLlm(settings.llm_configuration())
    # The base owns voice design, cloning, validation and publication; the workbench only
    # reads the published catalogue back, and only when a speech base is configured.
    voices = (
        None
        if client is None
        else WorkbenchVoices(PublicVoiceCatalogue(client, deadline_seconds=30))
    )
    # Minutes generation is the workbench's own LLM call: the queue records the profile
    # before the model is asked, so a deployment change cannot rewrite accepted work.
    minutes = MinutesTasks(PostgresMinutesTasks(versions), llm)
    recovery = _deletion_recovery(settings, objects)
    # Export production stays inert until the deployment both asks for it and supplies a
    # producer: workbench assembly consumes saved audio and never invokes inference.
    exports = PostgresExports(versions)
    export_worker = ExportWorker(
        exports,
        objects,
        export_producer,
        ExportMediaValidator(
            ExportValidationLimits(
                max_output_bytes=settings.export_max_output_bytes,
                max_unpacked_bytes=settings.export_max_unpacked_bytes,
                max_archive_entries=settings.export_max_archive_entries,
                max_audio_seconds=settings.export_max_audio_seconds,
                timeout_seconds=settings.export_validation_timeout_seconds,
            )
        ),
    )
    # Deployment-derived capability states, in the contract's canonical order:
    # "disabled" means this deployment deliberately does not offer it,
    # "unavailable" means it is asked for but a required dependency is missing, and
    # "available" means the serving runtime really can carry it out. Consumers read
    # these instead of guessing readiness, and never fill a capability in themselves.
    capabilities = {
        "transcription": (
            "disabled" if client is None else "available" if objects is not None else "unavailable"
        ),
        "meeting_minutes": "available" if llm.profile is not None else "disabled",
        "assistant": (
            "disabled"
            if llm.profile is None
            else "available"
            if client is not None
            else "unavailable"
        ),
        # Manual reading is local to the workbench; voice following is not part of this
        # capability and stays out of the reported state.
        "teleprompter": "available",
        "audiobook_export": (
            "available"
            if settings.export_interval_seconds > 0 and export_worker.enabled
            else "unavailable"
            if settings.export_interval_seconds > 0
            else "disabled"
        ),
    }
    render_jobs = None if client is None else PublicSpeechRenderJobs(client)
    render_units = WorkbenchRenderUnits(PostgresRenderUnits(versions), render_jobs)
    books = WorkbenchBooks(PostgresBooks(versions))
    saved_assets = (
        None
        if objects is None or client is None
        else WorkbenchSavedAssets(
            PostgresSavedAssets(versions),
            PublicArtifactSource(client, deadline_seconds=settings.saved_asset_deadline_seconds),
            objects,
        )
    )
    render_copies = (
        None if saved_assets is None else WorkbenchRenderResultCopies(books, saved_assets)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Adapters own only short per-request connections; these two owned handles are
        # the only long-lived ones and are released with the application, including when
        # the startup check refuses the database.
        sweeper: asyncio.Task[None] | None = None
        dispatcher: asyncio.Task[None] | None = None
        exports_dispatcher: asyncio.Task[None] | None = None
        render_settler: asyncio.Task[None] | None = None
        try:
            await check_database(settings)
            if llm.profile is not None:
                dispatcher = asyncio.create_task(
                    _dispatch_minutes(
                        minutes, settings.workspace_id, settings.minutes_interval_seconds
                    )
                )
            if settings.export_interval_seconds > 0 and export_worker.enabled:
                exports_dispatcher = asyncio.create_task(
                    _dispatch_exports(export_worker, settings.export_interval_seconds)
                )
            if settings.render_settle_interval_seconds > 0 and render_jobs is not None:
                render_settler = asyncio.create_task(
                    _settle_render_generations(
                        RenderReconciler(render_units, render_jobs, render_copies),
                        settings.workspace_id,
                        settings.render_settle_interval_seconds,
                    )
                )
            if recovery is not None:
                await check_cleanup_identity(settings)
                # ADR 0005: replay the deletion ledger before any content is served, so a
                # restart cannot reopen bytes whose confirmed erasure is still owed.
                await recovery.run()
                sweeper = asyncio.create_task(
                    _sweep_deletions(recovery, settings.cleanup_interval_seconds)
                )
            yield
        finally:
            if dispatcher is not None:
                dispatcher.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatcher
            if exports_dispatcher is not None:
                exports_dispatcher.cancel()
                with suppress(asyncio.CancelledError):
                    await exports_dispatcher
            if render_settler is not None:
                render_settler.cancel()
                with suppress(asyncio.CancelledError):
                    await render_settler
            if sweeper is not None:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
            if client is not None:
                await client.aclose()
            if objects is not None:
                objects.close()

    app = create_app(
        WorkbenchVersions(versions),
        # An accepted deletion is a promise the deployment must be able to keep, so the
        # deletion routes stay unavailable until a cleanup identity is configured.
        None
        if settings.cleanup_database_url is None
        else WorkbenchDeletions(
            PostgresDeletionUnitOfWork(
                versions, meetings=True, owned=PostgresOwnedDeletionCatalog()
            ),
            digest_plan=plan_digest,
        ),
        meetings=WorkbenchMeetings(PostgresMeetings(versions), llm),
        teleprompter=WorkbenchTeleprompter(PostgresTeleprompter(versions)),
        saved_assets=saved_assets,
        downloads=None
        if objects is None
        else WorkbenchDownloads(
            PostgresDownloads(versions),
            MinioDownloadReader(
                objects,
                max_bytes=settings.download_max_bytes,
                timeout_seconds=settings.download_timeout_seconds,
            ),
            max_active=settings.download_max_active,
        ),
        download_timeout_seconds=settings.download_timeout_seconds,
        voices=voices,
        books=books,
        # The same deployment-configured intranet LLM serves the assistant; with none
        # configured it reports a stable disabled error instead of a client-chosen model.
        assistant=WorkbenchAssistant(PostgresAssistantTurns(versions), llm),
        exports=WorkbenchExports(exports),
        capabilities=capabilities,
        render_units=render_units,
        default_workspace_id=settings.workspace_id,
        deployment_id=settings.deployment_id,
        cursor_key=settings.cursor_key.get_secret_value().encode("utf-8"),
        max_json_bytes=settings.max_json_bytes,
        body_timeout_seconds=settings.body_timeout_seconds,
        lifespan=lifespan,
    )
    app.add_middleware(
        RuntimeIngress,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    return app


def create_app_from_env() -> FastAPI:
    """uvicorn speechnexus_backend.workbench.runtime:create_app_from_env --factory"""
    return create_runtime_app(WorkbenchSettings())
