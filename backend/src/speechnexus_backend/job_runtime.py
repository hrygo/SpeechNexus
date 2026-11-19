"""Owned public Job writer lifetime; startup verifies but never provisions grants."""

from __future__ import annotations

import logging
import threading
import uuid

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.deletions import SpeechDeletions
from speechnexus_backend.speech.application.job_cancellation import CancellationCapacity
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from speechnexus_backend.speech.application.voice_publications import VoicePublications
from speechnexus_backend.speech.domain.job_prerequisites import ADMISSION_DSN
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactRemoval
from speechnexus_backend.speech.infrastructure.assets_minio import MinioAssetObjects
from speechnexus_backend.speech.infrastructure.deletion_executions_postgres import (
    PostgresDeletionExecutions,
)
from speechnexus_backend.speech.infrastructure.deletion_ledger_schema import (
    ARTIFACT_UPDATE_COLUMNS,
    ASSET_UPDATE_COLUMNS,
    CONTENT_DELETION_UPDATE_COLUMNS,
    DELETION_DELETE_TABLES,
    DELETION_LEDGER_UPDATE_COLUMNS,
    LEGACY_INTENT_UPDATE_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.deletion_plans_postgres import (
    PostgresDeletionPlans,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresLeaseSettler
from speechnexus_backend.speech.infrastructure.job_schema_preflight import (
    JobSchemaNotMigratedError,
    require_migrated_schema,
)
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    JOB_WRITER_INSERT_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.voice_drafts_postgres import PostgresVoiceDrafts
from speechnexus_backend.speech.infrastructure.voice_publications_postgres import (
    PostgresVoicePublications,
)
from speechnexus_backend.startup_privileges import (
    refuse_column_privileges,
    refuse_foreign_relation_privileges,
    refuse_schema_create,
    refuse_table_privileges,
    require_schema_usage,
)

_LOG = logging.getLogger(__name__)

_ADMISSION_RESERVATION_TABLES = ("admission_attempts", "admission_requests", "admission_leases")
_ADMISSION_REQUIRED_SELECT = (
    "workspace_quotas",
    "resource_pools",
    "host_nodes",
    "engine_instances",
    "runtime_instance_heads",
    "runtime_instance_intents",
    "admission_operations",
    "admission_leases",
    "speech_jobs",
    "speech_job_intents",
)

_PUBLICATION_READ_ONLY = (
    "speech_voice_capability_evidence",
    "speech_jobs",
    "speech_artifacts",
    "speech_assets",
    "speech_asset_objects",
    "speech_binding_revisions",
    "speech_voice_draft_revisions",
)
_PUBLICATION_APPEND_ONLY = (
    "speech_publications",
    "speech_voice_revisions",
    "speech_voice_publication_intents",
    "speech_asset_references",
)
_PUBLICATION_UPDATE_COLUMNS = {
    "speech_publication_heads": ("revision", "revision_id", "updated_at_us"),
    "speech_catalog_state": ("revision", "generated_at_us"),
    "speech_voice_drafts": ("status",),
}
_PUBLICATION_REQUIRED_SELECT = (
    *_PUBLICATION_READ_ONLY,
    *_PUBLICATION_APPEND_ONLY,
    *_PUBLICATION_UPDATE_COLUMNS,
)
_PUBLICATION_REQUIRED_INSERT = (*_PUBLICATION_APPEND_ONLY, "speech_publication_heads")

# The deletion routes are two routes; their grants are checked when those routes
# are used, not as a process-wide startup condition. A writer that never serves
# them must still boot, and a deployment that forgets the grants gets a
# diagnosable 503 on the route instead of a silent runtime failure.
DELETION_PREVIEW_TABLES = (
    "speech_asset_objects",
    "speech_asset_references",
    "speech_deletion_plans",
    # The artifact and voice_revision snapshots read the execution evidence and
    # the immutable published revision to decide what still references the target.
    "speech_voice_capability_evidence",
    "speech_voice_revisions",
)
# The execution route (0059) adds its own ledger and reuses the asset content
# deletion machine's two relations. None is a startup requirement.
DELETION_EXECUTION_TABLES = (
    "speech_content_deletions",
    "speech_asset_events",
    "speech_deletion_ledger",
)
# Allowed but not required. A role may hold these grants (the reference
# deployment does) or none at all (a writer that never serves the route).
_JOB_OPTIONAL_TABLES = frozenset((*DELETION_PREVIEW_TABLES, *DELETION_EXECUTION_TABLES))
REQUIRED_SELECT = (
    "speech_workspaces",
    "speech_publications",
    "speech_publication_heads",
    "speech_catalog_state",
    "speech_publication_retirements",
    "speech_binding_revisions",
    "speech_assets",
    "speech_pronunciation_revisions",
    "speech_jobs",
    "speech_job_intents",
    "speech_job_events",
    "speech_job_submissions",
    "speech_job_cancellations",
    "speech_artifacts",
    "speech_voice_drafts",
    "speech_voice_draft_revisions",
    # The owner-privileged projection of a published draft's facts; the evidence
    # ledger and the immutable revision document behind it stay unreadable here.
    "speech_voice_draft_publications",
    "speech_voice_metadata_commands",
)
REQUIRED_INSERT = (
    "speech_voice_drafts",
    "speech_voice_draft_revisions",
    "speech_voice_metadata_commands",
    "speech_jobs",
    "speech_job_intents",
    "speech_job_events",
    "speech_job_submissions",
    "speech_job_cancellations",
)
REQUIRED_INSERT_COLUMNS: dict[str, tuple[str, ...]] = {
    "speech_voice_draft_revisions": (
        "draft_id",
        "revision",
        "kind",
        "generation_job_id",
        "input_json",
        "binding_json",
        "created_at_us",
    ),
}
REQUIRED_UPDATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "speech_voice_drafts": (
        "display_name",
        "metadata_revision",
        "updated_at_us",
        "head_revision",
        "generation_job_id",
    ),
    "speech_jobs": ("state", "status_version", "result_json", "error_code", "updated_at_us"),
}

_JOB_SUBJECT = "Public Job writer"
_LOG = logging.getLogger(__name__)
# Every relation the combined Job writer may touch. A grant outside this set --
# table or column -- is refused by name (#561).
_JOB_DECLARED_TABLES = tuple(
    dict.fromkeys(
        (
            *REQUIRED_SELECT,
            *REQUIRED_INSERT,
            *DELETION_PREVIEW_TABLES,
            *DELETION_EXECUTION_TABLES,
            "coordination_jobs",
            "coordination_outbox",
            "admission_operations",
            "speech_job_cancellation_fences",
            "speech_job_cancellation_reconciliations",
            "speech_job_cancellation_not_submitted",
        )
    )
)
# The only UPDATE columns the writer may hold. Tables not named here are read-only
# for this role, and the two required surfaces must be complete.
_JOB_UPDATE_SURFACE: dict[str, tuple[str, ...]] = {
    **REQUIRED_UPDATE_COLUMNS,
    # Allowed, not required: deletion preview takes these row locks and deletion
    # execution drives the asset content-deletion machine, and the route's own
    # probe names any missing column instead of failing at request time.
    "speech_workspaces": ("metadata_revision",),
    "speech_assets": ASSET_UPDATE_COLUMNS,
    # A confirmed Artifact deletion writes the same `deleted` record 0054's
    # retention path does; it never rewrites the object identity or deadline.
    "speech_artifacts": ARTIFACT_UPDATE_COLUMNS,
    "speech_deletion_ledger": DELETION_LEDGER_UPDATE_COLUMNS,
    "speech_content_deletions": CONTENT_DELETION_UPDATE_COLUMNS,
    # Operation-content deletion tombstones the legacy private command copy
    # through 0007's command-shape CHECK (#540).
    "admission_operations": LEGACY_INTENT_UPDATE_COLUMNS,
    "coordination_jobs": (
        "claim_owner_id",
        "claim_expires_at_us",
        "claim_version",
        "job_state",
    ),
    "speech_job_cancellation_reconciliations": (
        "attempts",
        "next_check_at_us",
        "last_outcome",
        "settled_at_us",
        "updated_at_us",
    ),
}
_JOB_REQUIRED_UPDATE = frozenset(REQUIRED_UPDATE_COLUMNS)
# The deletion executor removes exactly the authoritative public Job intent row of
# the confirmed operation (#540) and one unreferenced voice draft revision. Every
# other declared relation still refuses DELETE/TRUNCATE, and the assertion below
# keeps that surface to the two relations the SQL actually names.
_JOB_DELETE_TABLES = frozenset(DELETION_DELETE_TABLES)
assert set(_JOB_DECLARED_TABLES) >= _JOB_DELETE_TABLES, (
    "The deletion DELETE surface names a relation outside the declared writer surface"
)
# A writer-only deployment keeps reads and cancellations, but a create has no
# admission role to reserve capacity with. The store used to answer the bare
# ``503 job_unavailable``, which read like a database outage; the runtime names the
# blank key instead (never a value) (#877 item 1). The text is the shared table's,
# so the operator map in docs/operations/compose-minimal.md stays one source (#920).
_ADMISSION_DSN_BLANK = ADMISSION_DSN.detail
_PUBLICATION_SUBJECT = "Voice publisher"


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Public Job connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class _SplitJobDatabaseError(ValueError):
    """A configured Job companion identity reaches another database.

    Distinct from the generic startup rejection so ``open()`` can keep this message
    intact: an operator who split the DSNs needs the two keys and the same-database
    rule, not a hint to re-check the migration or the writer role (#660).
    """


class JobRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.job_writer_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API public Job writer identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self._admission_pool: SpeechPool | None = None
        self._publisher_pool: SpeechPool | None = None
        self._capacity_pool: SpeechPool | None = None
        self.jobs: PublicJobs | None = None
        self.deletion_plans: SpeechDeletions | None = None
        self.deletion_objects: MinioAssetObjects | None = None
        self.artifact_deletion_objects: MinioArtifactRemoval | None = None
        self.voice_drafts: VoiceDrafts | None = None
        self.voice_publications: VoicePublications | None = None
        # The single release object the immediate cancellation and the
        # reconciliation settlement port share; None until this runtime opens.
        self.capacity_release: CancellationCapacity | None = None
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.jobs is not None:
                raise ValueError("Public Job writer is already open")
            secret = self._settings.job_writer_database_url
            assert secret is not None
            name = "speech-jobs-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=4,
                    name=name,
                )
                self._pool = pool
                pool.open()
                with pool.connection(timeout=3) as connection:
                    connection.execute("SET LOCAL statement_timeout='3s'")
                    # A database that never ran the migration profile is refused by
                    # name here: the role guard below would otherwise report the
                    # absent relations as missing grants (#920 item 3).
                    require_migrated_schema(
                        connection,
                        self._settings.database_schema,
                        (*REQUIRED_SELECT, *_ADMISSION_REQUIRED_SELECT),
                    )
                    _verify_privileges(connection, self._settings.database_schema)
                admission_pool = self._open_admission_pool()
                self._admission_pool = admission_pool
                if admission_pool is not None and _database_identity(pool) != _database_identity(
                    admission_pool
                ):
                    # Identity mismatch is not a privilege or migration fault, so it
                    # must not be masked as one: the message names both keys and the
                    # rule. Assignment above lets the shared cleanup close the pool.
                    raise _SplitJobDatabaseError(
                        "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL and "
                        "SPEECHNEXUS_JOB_WRITER_DATABASE_URL must connect to the same database: "
                        "one admission transaction reserves the row and writes the Job, so point "
                        "both at one database or unset SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"
                    )
                publisher_pool = self._open_publisher_pool()
                self._publisher_pool = publisher_pool
                if publisher_pool is not None and _database_identity(pool) != _database_identity(
                    publisher_pool
                ):
                    raise _SplitJobDatabaseError(
                        "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL and "
                        "SPEECHNEXUS_JOB_WRITER_DATABASE_URL must connect to the same database: "
                        "the publisher appends the publication rows and revision pointers the Job "
                        "writer's schema verifies, so point both at one database or unset "
                        "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL"
                    )
                adapter = PostgresPublicJobs(
                    pool,
                    admission_pool=admission_pool,
                    require_admission=True,
                    timeout_seconds=self._settings.job_transaction_timeout_seconds,
                    storage_bucket=self._settings.object_storage_bucket,
                    admission_unavailable_detail=(
                        None if admission_pool is not None else _ADMISSION_DSN_BLANK
                    ),
                )
                capacity_pool = self._open_capacity_pool()
                self._capacity_pool = capacity_pool
                self._verify_capacity_pool(pool, capacity_pool)
                self.capacity_release = CancellationCapacity(
                    PostgresLeaseSettler(
                        capacity_pool,
                        timeout_seconds=self._settings.job_transaction_timeout_seconds,
                    ).release_sync
                )
                self.jobs = PublicJobs(adapter, adapter)
                deletion_store = PostgresDeletionPlans(
                    pool, timeout_seconds=self._settings.job_transaction_timeout_seconds
                )
                try:
                    missing_grants = deletion_store.missing_privileges()
                except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as error:
                    # The probe is advisory; a pool or catalog error must not stop boot.
                    _LOG.debug("deletion preview probe skipped: %s", type(error).__name__)
                    missing_grants = ()
                if missing_grants:
                    _LOG.warning(
                        "deletion preview is not provisioned; missing %s. Apply "
                        "deletion_plan_grants to the Job writer; the rest of the API "
                        "starts normally and the route answers 503 "
                        "deletion_plans_not_configured.",
                        ", ".join(missing_grants),
                    )
                deletion_executions = PostgresDeletionExecutions(
                    pool, timeout_seconds=self._settings.job_transaction_timeout_seconds
                )
                try:
                    execution_missing = deletion_executions.missing_privileges()
                except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as error:
                    _LOG.debug("deletion execution probe skipped: %s", type(error).__name__)
                    execution_missing = ()
                if execution_missing:
                    _LOG.warning(
                        "deletion execution is not provisioned; missing %s. Apply "
                        "deletion_execution_grants to the Job writer; the rest of the API "
                        "starts normally and the route answers 503 deletion_not_configured.",
                        ", ".join(execution_missing),
                    )
                self.deletion_objects = self._open_deletion_objects()
                self.artifact_deletion_objects = self._open_artifact_deletion_objects()
                self.deletion_plans = SpeechDeletions(
                    deletion_store,
                    workspace_id=self._settings.capability_default_workspace_id,
                    executions=deletion_executions,
                    purge=self.deletion_objects,
                    artifact_purge=self.artifact_deletion_objects,
                )
                self.voice_drafts = VoiceDrafts(self.jobs, PostgresVoiceDrafts(pool))
                self.voice_publications = (
                    VoicePublications(
                        PostgresVoicePublications(
                            publisher_pool,
                            timeout_seconds=self._settings.job_transaction_timeout_seconds,
                        )
                    )
                    if publisher_pool is not None
                    else None
                )
            except BaseException as error:
                try:
                    self.capacity_release = None
                    if self._capacity_pool is not None:
                        self._capacity_pool.close()
                        self._capacity_pool = None
                    if self._publisher_pool is not None:
                        self._publisher_pool.close()
                        self._publisher_pool = None
                    if self._admission_pool is not None:
                        self._admission_pool.close()
                        self._admission_pool = None
                    if self._pool is not None:
                        self._pool.close()
                        self._pool = None
                finally:
                    logger.removeFilter(diagnostic)
                    self._diagnostics = None
                if not isinstance(error, Exception):
                    raise
                # A split-database configuration is a composition error and an
                # unmigrated database has its own named repair; neither is the
                # migration/role fault the generic message describes, so both keep
                # their own answer (the pool cleanup above already ran).
                if isinstance(error, (_SplitJobDatabaseError, JobSchemaNotMigratedError)):
                    raise
                raise ValueError(
                    "Public Job writer startup rejected; verify migration and writer role"
                ) from None

    def _open_deletion_objects(self) -> MinioAssetObjects | None:
        """The asset object-store identity the deletion route purges with.

        The content of an ``asset`` target is the object itself, so execution
        cannot confirm a deletion without this identity. It is the same private
        bucket the asset runtime writes; a deployment that does not configure
        object storage keeps the preview route and answers
        ``503 deletion_object_storage_unconfigured`` on execution instead of
        claiming a purge that never happened.
        """
        settings = self._settings
        if (
            settings.object_storage_access_key is None
            or settings.object_storage_secret_key is None
            or settings.object_storage_endpoint is None
        ):
            return None
        return MinioAssetObjects(
            str(settings.object_storage_endpoint),
            settings.object_storage_bucket,
            settings.object_storage_access_key.get_secret_value(),
            settings.object_storage_secret_key.get_secret_value(),
        )

    def _open_artifact_deletion_objects(self) -> MinioArtifactRemoval | None:
        """The Artifact bucket identity a confirmed deletion purges with.

        It is the same private bucket the Artifact publisher and the retention
        sweep use; no separate deployment unit or credential is introduced. A
        deployment without object storage keeps preview and answers
        ``503 deletion_object_storage_unconfigured`` when an Artifact execution
        would otherwise claim a purge it cannot perform.
        """
        settings = self._settings
        if (
            settings.object_storage_access_key is None
            or settings.object_storage_secret_key is None
            or settings.object_storage_endpoint is None
        ):
            return None
        return MinioArtifactRemoval(
            str(settings.object_storage_endpoint),
            settings.object_storage_bucket,
            settings.object_storage_access_key.get_secret_value(),
            settings.object_storage_secret_key.get_secret_value(),
        )

    def _open_admission_pool(self) -> SpeechPool | None:
        secret = self._settings.job_admission_database_url
        if secret is None:
            return None
        name = "speech-job-admission-" + uuid.uuid4().hex
        pool = speech_pool(
            secret.get_secret_value(),
            schema=self._settings.database_schema,
            max_size=4,
            name=name,
        )
        pool.open()
        try:
            with pool.connection(timeout=3) as connection:
                connection.execute("SET LOCAL statement_timeout='3s'")
                require_migrated_schema(
                    connection,
                    self._settings.database_schema,
                    _ADMISSION_REQUIRED_SELECT,
                )
                _verify_admission_privileges(connection, self._settings.database_schema)
        except BaseException:
            pool.close()
            raise
        return pool

    def _open_publisher_pool(self) -> SpeechPool | None:
        secret = self._settings.voice_publisher_database_url
        if secret is None:
            return None
        name = "speech-voice-publisher-" + uuid.uuid4().hex
        pool = speech_pool(
            secret.get_secret_value(),
            schema=self._settings.database_schema,
            max_size=4,
            name=name,
        )
        pool.open()
        try:
            with pool.connection(timeout=3) as connection:
                connection.execute("SET LOCAL statement_timeout='3s'")
                _verify_publication_privileges(connection, self._settings.database_schema)
        except BaseException:
            pool.close()
            raise
        return pool

    def _open_capacity_pool(self) -> SpeechPool:
        """Open the identity that returns capacity for an immediate cancellation.

        The API identity holds the coordination-runtime writes in the reference
        deployment (``coordination_runtime_grants`` for ``sn_api``), while the Job
        writer identity does not, so lease release runs on the existing
        ``SPEECHNEXUS_DATABASE_URL`` rather than on the writer pool. Nothing is
        verified here: a refused or unavailable release is an already tolerated,
        retryable answer (the Job stays terminal and the lease stays reserved), so
        a release-only privilege gap must not take the whole API down. A refused
        release leaves the reserved row visible to the documented held-lease
        query, and the already ``cancelled`` Job is what a later pass retries.
        """
        name = "speech-cancel-release-" + uuid.uuid4().hex
        pool = speech_pool(
            self._settings.database_url.get_secret_value(),
            schema=self._settings.database_schema,
            max_size=2,
            name=name,
        )
        pool.open()
        return pool

    def _verify_capacity_pool(self, writer_pool: SpeechPool, capacity_pool: SpeechPool) -> None:
        """Refuse a verified split database; never fail on an unverifiable one.

        A capacity pool on another database makes the lease lookup miss, and
        ``release_sync`` answers True for a Job whose lease stays held: a silent
        fake success, worse than the held lease it was meant to fix. So a *verified*
        mismatch is a composition error. An unreachable or under-privileged capacity
        identity is different: release already answers a retryable False, and that
        must not stop the whole API from booting, so it only warns (the held lease
        stays visible to the documented held-lease query).
        """
        try:
            if _database_identity(capacity_pool) != _database_identity(writer_pool):
                raise _SplitJobDatabaseError(
                    "SPEECHNEXUS_DATABASE_URL and SPEECHNEXUS_JOB_WRITER_DATABASE_URL must "
                    "connect to the same database: cancellation releases the admission lease "
                    "the Job writer's transaction reserved, and a different database would "
                    "answer released without touching that lease"
                )
            with capacity_pool.connection(timeout=3) as connection:
                connection.execute("SELECT 1")
        except _SplitJobDatabaseError:
            raise
        except Exception:
            _LOG.warning(
                "cancellation capacity identity unverified; a release will answer a retryable "
                "failure instead of stopping the API",
                exc_info=True,
            )

    def close(self) -> None:
        with self._lifetime:
            self.jobs = None
            self.deletion_plans = None
            self.voice_drafts = None
            self.voice_publications = None
            self.capacity_release = None
            if self._capacity_pool is not None:
                self._capacity_pool.close()
                self._capacity_pool = None
            if self._publisher_pool is not None:
                self._publisher_pool.close()
                self._publisher_pool = None
            if self._admission_pool is not None:
                self._admission_pool.close()
                self._admission_pool = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None


def _database_identity(pool: SpeechPool) -> tuple[str, str, int]:
    with pool.connection(timeout=3) as connection:
        row = connection.execute(
            "SELECT current_database() AS database, "
            "COALESCE(host(inet_server_addr()), 'local') AS host, "
            "COALESCE(inet_server_port(), 0) AS port"
        ).fetchone()
    if row is None:
        raise ValueError("Public Job database identity is unavailable")
    return str(row["database"]), str(row["host"]), int(row["port"])


def _verify_privileges(connection: SpeechConnection, schema: str) -> None:
    require_schema_usage(connection, None, schema, subject=_JOB_SUBJECT)
    refuse_schema_create(connection, None, schema, subject=_JOB_SUBJECT)
    # Reservation tables are reserved for the admission role and are deliberately
    # not part of the writer's declared surface. Check them first so the specific
    # "no admission reservations" repair hint wins over the generic stray grant.
    for table in _ADMISSION_RESERVATION_TABLES:
        row = connection.execute(
            "SELECT pg_catalog.has_any_column_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if row is None or row["ok"]:
            raise ValueError("Public Job writer must not insert admission reservations on " + table)
    refuse_foreign_relation_privileges(
        connection,
        None,
        schema,
        _JOB_DECLARED_TABLES,
        subject=_JOB_SUBJECT,
        declared_hint="_JOB_DECLARED_TABLES",
    )
    rows = connection.execute(
        "SELECT c.relname AS name, c.relkind AS kind, "
        "pg_catalog.has_table_privilege(CURRENT_USER, c.oid, 'SELECT') AS can_select "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
        (schema,),
    ).fetchall()
    privileges = {row["name"]: row for row in rows}
    for table in REQUIRED_SELECT:
        row = privileges.get(table)
        if row is None or not row["can_select"]:
            raise ValueError("Public Job writer lacks SELECT on " + table)
    for table, columns in REQUIRED_INSERT_COLUMNS.items():
        for column in columns:
            allowed = connection.execute(
                "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'INSERT') AS ok",
                (f"{schema}.{table}", column),
            ).fetchone()
            if allowed is None or not allowed["ok"]:
                raise ValueError(f"Public Job writer lacks INSERT on {table}.{column}")
    for table, columns in REQUIRED_UPDATE_COLUMNS.items():
        for column in columns:
            allowed = connection.execute(
                "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'UPDATE') AS ok",
                (f"{schema}.{table}", column),
            ).fetchone()
            if allowed is None or not allowed["ok"]:
                raise ValueError(f"Public Job writer lacks UPDATE on {table}.{column}")
    # No declared table -- writable, append-only or coordination -- may confer
    # deletion, truncation or the two whole-table authorities this writer never
    # needs (#561). Judging every declared relation closes the same gap on the
    # coordination/admission tables that the first slice only covered implicitly.
    for table in _JOB_DECLARED_TABLES:
        refused = (
            ("TRUNCATE", "REFERENCES", "TRIGGER")
            if table in _JOB_DELETE_TABLES
            else ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
        )
        refuse_table_privileges(
            connection,
            None,
            f"{schema}.{table}",
            table,
            refused,
            subject=_JOB_SUBJECT,
        )
    # DELETE is permitted but not required on that one relation, exactly like the
    # execution route's other grants: a writer that never serves deletion boots
    # without it, and the route's own probe names the gap before any content moves.
    # INSERT is a per-column authority too: no declared table may carry a
    # table-level grant, and every writable relation must match its declared
    # column set exactly (an added column must not silently widen the surface).
    for table in _JOB_DECLARED_TABLES:
        qualified = f"{schema}.{table}"
        expected_columns = set(JOB_WRITER_INSERT_COLUMNS.get(table, ()))
        repair = (
            "revoke the table-level grant, then re-grant INSERT only on the declared "
            "columns; see deploy/README.md upgrade section"
            if expected_columns
            else None
        )
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("INSERT",),
            subject=_JOB_SUBJECT,
            repair=repair,
        )
        column_rows = connection.execute(
            "SELECT column_name, pg_catalog.has_column_privilege(CURRENT_USER, %s, "
            "column_name, 'INSERT') AS insertable FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (qualified, schema, table),
        ).fetchall()
        granted_columns = {row["column_name"] for row in column_rows if row["insertable"]}
        if expected_columns:
            optional = table in _JOB_OPTIONAL_TABLES
            shapes = (set(), expected_columns) if optional else (expected_columns,)
            if granted_columns not in shapes:
                raise ValueError(
                    f"{_JOB_SUBJECT} INSERT columns do not match the declared surface on "
                    f"{table}; revoke the extra column grant or restore the declared column"
                )
        else:
            refuse_column_privileges(
                connection,
                None,
                qualified,
                table,
                ("INSERT",),
                subject=_JOB_SUBJECT,
            )
        # REFERENCES is never useful to this writer, not even on one column.
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("REFERENCES",),
            subject=_JOB_SUBJECT,
        )
    # UPDATE is a per-column authority too: a table grant would make every column
    # updatable, and an added column would silently widen a declared surface. Each
    # declared table is compared directly; only the four tables with legitimate
    # update columns may hold any, and the two required shapes must be complete.
    for table in _JOB_DECLARED_TABLES:
        allowed_columns = set(_JOB_UPDATE_SURFACE.get(table, ()))
        column_rows = connection.execute(
            "SELECT column_name, pg_catalog.has_column_privilege(CURRENT_USER, %s, "
            "column_name, 'UPDATE') AS updatable FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (f"{schema}.{table}", schema, table),
        ).fetchall()
        granted = {row["column_name"] for row in column_rows if row["updatable"]}
        complete = table in _JOB_REQUIRED_UPDATE
        if (complete and granted != allowed_columns) or (
            not complete and not granted <= allowed_columns
        ):
            raise ValueError(
                f"{_JOB_SUBJECT} UPDATE columns do not match the declared surface on "
                f"{table}; revoke the extra column grant or restore the declared column"
            )


def _verify_publication_surface(connection: SpeechConnection, schema: str) -> None:
    """The publisher's authority is exactly its declared tables (#549).

    A stray table grant, a column grant on a foreign relation or the ability to
    create schema objects is refused by name. This closes the gap where a table
    outside the declared set -- the retirement ledger, for one -- could be written
    while every declared-table assertion still held.
    """
    require_schema_usage(connection, None, schema, subject=_PUBLICATION_SUBJECT)
    refuse_schema_create(connection, None, schema, subject=_PUBLICATION_SUBJECT)
    refuse_foreign_relation_privileges(
        connection,
        None,
        schema,
        _PUBLICATION_REQUIRED_SELECT,
        subject=_PUBLICATION_SUBJECT,
        declared_hint="_PUBLICATION_REQUIRED_SELECT",
    )


def _verify_publication_privileges(connection: SpeechConnection, schema: str) -> None:
    """The publisher may measure nothing and append only its own projection.

    The tables it may write still need REFERENCES and TRIGGER refused (#549): both
    are whole-table grants even when the row itself is appended or narrowed. Table
    and column authority are judged separately -- ``has_table_privilege`` stays
    false under a column-only grant, so every column decision uses the column
    helpers. Relations outside the declared set are refused as well.
    """
    _verify_publication_surface(connection, schema)
    for table in _PUBLICATION_REQUIRED_SELECT:
        allowed = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Voice publisher lacks SELECT on {table}")
    for table in _PUBLICATION_REQUIRED_INSERT:
        allowed = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Voice publisher lacks INSERT on {table}")
    for table in ("speech_voice_drafts", "speech_catalog_state"):
        append = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if append is None or append["ok"]:
            raise ValueError(f"Voice publisher can append to {table}")
    for table, columns in _PUBLICATION_UPDATE_COLUMNS.items():
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_PUBLICATION_SUBJECT,
        )
        if table not in _PUBLICATION_REQUIRED_INSERT:
            refuse_column_privileges(
                connection,
                None,
                qualified,
                table,
                ("INSERT",),
                subject=_PUBLICATION_SUBJECT,
            )
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("REFERENCES",),
            subject=_PUBLICATION_SUBJECT,
        )
        column_rows = connection.execute(
            "SELECT column_name, pg_catalog.has_column_privilege(CURRENT_USER, %s, "
            "column_name, 'UPDATE') AS updatable FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (qualified, schema, table),
        ).fetchall()
        if {row["column_name"] for row in column_rows if row["updatable"]} != set(columns):
            raise ValueError(f"Voice publisher UPDATE columns do not match {table}")
    for table in _PUBLICATION_READ_ONLY:
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_PUBLICATION_SUBJECT,
        )
        append = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (qualified,),
        ).fetchone()
        if append is None or append["ok"]:
            raise ValueError(f"Voice publisher can append to read-only table {table}")
        # A column INSERT is enough to forge a measured fact row even though the
        # table-level INSERT stays false (#549).
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("INSERT", "UPDATE", "REFERENCES"),
            subject=_PUBLICATION_SUBJECT,
        )
    for table in _PUBLICATION_APPEND_ONLY:
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_PUBLICATION_SUBJECT,
        )
        rewrite = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'UPDATE') AS ok",
            (qualified,),
        ).fetchone()
        if rewrite is None or rewrite["ok"]:
            raise ValueError(f"Voice publisher can rewrite append-only table {table}")
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("UPDATE", "REFERENCES"),
            subject=_PUBLICATION_SUBJECT,
        )


def _verify_admission_privileges(connection: SpeechConnection, schema: str) -> None:
    """The admission role must read every frozen fact and write every reservation."""
    for table in _ADMISSION_REQUIRED_SELECT:
        allowed = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError("Public Job admission role lacks SELECT on " + table)
    for table, column in (
        ("admission_operations", "request_document"),
        ("admission_attempts", "operation_id"),
        ("admission_leases", "lease_id"),
        ("coordination_jobs", "job_id"),
        ("speech_jobs", "job_id"),
        ("speech_job_intents", "request_document"),
        ("speech_job_submissions", "job_id"),
        ("speech_job_events", "job_id"),
    ):
        allowed = connection.execute(
            "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'INSERT') AS ok",
            (f"{schema}.{table}", column),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Public Job admission role lacks INSERT on {table}.{column}")
    for table, column in (
        ("workspace_quotas", "held_attempts"),
        ("host_nodes", "held_unified_physical_memory_bytes"),
        ("engine_instances", "held_slots"),
    ):
        allowed = connection.execute(
            "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'UPDATE') AS ok",
            (f"{schema}.{table}", column),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Public Job admission role lacks UPDATE on {table}.{column}")
