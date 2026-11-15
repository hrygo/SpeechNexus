"""Composition and owned lifetime for the bounded Artifact retention sweep.

The sweep is a maintenance process, not a request-scoped component: it owns its
database pool and its object client, and it starts only when the runtime role can
actually perform the two transitions it needs. A role that could insert, delete
or rewrite a deadline would widen retention into a content-deletion authority,
so startup refuses each of those grants by name before the first row is read.

That check is also why this process takes its own DSN
(``SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL``, the container's
``SPEECHNEXUS_DATABASE_URL``) instead of the API runtime identity: the reference
deployment's ``sn_api`` legitimately holds schema-wide privileges the sweep must
never have, so sharing it would fail the refusal below at startup.
"""

from __future__ import annotations

import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.artifact_retention import ArtifactRetention
from speechnexus_backend.speech.infrastructure.artifact_schema import (
    ARTIFACT_RETENTION_UPDATE_COLUMNS,
    ARTIFACTS,
)
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactRemoval
from speechnexus_backend.speech.infrastructure.artifacts_postgres import PostgresArtifactRetention
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, speech_pool
from speechnexus_backend.startup_privileges import (
    current_role,
    refuse_column_privileges,
    refuse_foreign_relation_privileges,
    refuse_schema_create,
    refuse_table_privileges,
    require_schema_usage,
)

_DECLARED_TABLES = (ARTIFACTS,)
_SUBJECT = "Artifact retention"


class ArtifactRetentionRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.object_storage_access_key is None or settings.object_storage_secret_key is None:
            raise ValueError("Explicit object-storage credentials are required")
        self._settings = settings
        self._schema = settings.database_schema
        self._pool = speech_pool(
            settings.database_url.get_secret_value(),
            schema=self._schema,
            max_size=2,
            name="speech-artifact-retention-" + uuid.uuid4().hex,
        )
        self._objects = MinioArtifactRemoval(
            str(settings.object_storage_endpoint),
            settings.object_storage_bucket,
            settings.object_storage_access_key.get_secret_value(),
            settings.object_storage_secret_key.get_secret_value(),
        )
        self.retention: ArtifactRetention | None = None

    def open(self) -> None:
        if self.retention is not None:
            raise ValueError("Artifact retention is already open")
        try:
            self._pool.open()
            with self._pool.connection(timeout=3) as connection:
                connection.execute("SET LOCAL statement_timeout = '3s'")
                _verify_retention_authority(connection, self._schema)
            self.retention = ArtifactRetention(
                PostgresArtifactRetention(
                    self._pool, timeout_seconds=self._settings.artifact_read_timeout_seconds
                ),
                self._objects,
                batch_size=self._settings.artifact_retention_batch_size,
            )
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            # Name the cause type only: a grant problem and an unreachable
            # database are different repairs, and neither message may carry a
            # connection string.
            raise ValueError(
                "Artifact retention startup rejected "
                f"({type(error).__name__}); verify artifact_retention_grants and the role"
            ) from None

    def close(self) -> None:
        self.retention = None
        self._objects.close()
        self._pool.close()


def _verify_retention_authority(connection: SpeechConnection, schema: str) -> None:
    """The sweep may select rows and change exactly the declared columns.

    The declared set is the state column plus the removal ledger; anything else,
    including the recorded deadline, is a wider authority than a retention pass
    is allowed to hold.
    """
    role = current_role(connection)
    require_schema_usage(connection, role, schema, subject=_SUBJECT)
    refuse_schema_create(connection, role, schema, subject=_SUBJECT)
    refuse_foreign_relation_privileges(
        connection,
        role,
        schema,
        _DECLARED_TABLES,
        subject=_SUBJECT,
        declared_hint="_DECLARED_TABLES",
    )
    qualified = f"{schema}.{ARTIFACTS}"
    row = connection.execute(
        "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS held",
        (qualified,),
    ).fetchone()
    if row is None or not row["held"]:
        raise ValueError(
            f"{_SUBJECT} lacks SELECT on {ARTIFACTS}; install artifact_retention_grants "
            "for this role"
        )
    refuse_table_privileges(
        connection,
        role,
        qualified,
        ARTIFACTS,
        ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
        subject=_SUBJECT,
        repair="grant the declared column UPDATE instead of a whole-table write",
    )
    refuse_column_privileges(
        connection,
        role,
        qualified,
        ARTIFACTS,
        ("INSERT", "REFERENCES"),
        subject=_SUBJECT,
    )
    for column in ARTIFACT_RETENTION_UPDATE_COLUMNS:
        held = connection.execute(
            "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'UPDATE') AS held",
            (qualified, column),
        ).fetchone()
        if held is None or not held["held"]:
            raise ValueError(
                f"{_SUBJECT} lacks UPDATE on {ARTIFACTS}.{column}; install "
                "artifact_retention_grants for this role"
            )
    stray = connection.execute(
        "SELECT count(*) AS total FROM pg_catalog.pg_attribute a "
        "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
        "AND NOT (a.attname = ANY(%s)) "
        "AND pg_catalog.has_column_privilege(CURRENT_USER, a.attrelid, a.attnum, 'UPDATE')",
        (qualified, list(ARTIFACT_RETENTION_UPDATE_COLUMNS)),
    ).fetchone()
    if stray is None or int(stray["total"]):
        raise ValueError(
            f"{_SUBJECT} must hold UPDATE only on the declared {ARTIFACTS} columns; "
            "revoke the extra column grants"
        )


__all__ = ["ArtifactRetentionRuntime"]
