"""Composition and owned lifetime for read-only committed Artifact reads."""

from __future__ import annotations

import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.artifacts import ArtifactService
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactObjects
from speechnexus_backend.speech.infrastructure.artifacts_postgres import PostgresArtifactMetadata
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, speech_pool
from speechnexus_backend.startup_privileges import (
    current_role,
    refuse_column_privileges,
    refuse_foreign_relation_privileges,
    refuse_schema_create,
    refuse_table_privileges,
    require_schema_usage,
)


class ArtifactRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.artifact_reader_database_url is None:
            raise ValueError("An explicit Artifact reader identity is required")
        if settings.object_storage_access_key is None or settings.object_storage_secret_key is None:
            raise ValueError("Explicit object-storage credentials are required")
        self._settings = settings
        self._pool = speech_pool(
            settings.artifact_reader_database_url.get_secret_value(),
            schema=settings.database_schema,
            max_size=2,
            name="speech-artifacts-" + uuid.uuid4().hex,
        )
        self._objects = MinioArtifactObjects(
            str(settings.object_storage_endpoint),
            settings.object_storage_bucket,
            settings.object_storage_access_key.get_secret_value(),
            settings.object_storage_secret_key.get_secret_value(),
        )
        self.service: ArtifactService | None = None

    def open(self) -> None:
        if self.service is not None:
            raise ValueError("Artifact reader is already open")
        try:
            self._pool.open()
            with self._pool.connection(timeout=3) as connection:
                connection.execute("SET LOCAL statement_timeout = '3s'")
                _verify_read_only(connection, self._settings.database_schema)
            self.service = ArtifactService(
                PostgresArtifactMetadata(
                    self._pool, timeout_seconds=self._settings.artifact_read_timeout_seconds
                ),
                self._objects,
                self._settings.capability_default_workspace_id,
            )
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            # The cause type is safe to name and is what distinguishes a bad
            # grant from an unreachable database; the message itself stays fixed.
            raise ValueError(
                "Artifact reader startup rejected "
                f"({type(error).__name__}); verify migration and reader role"
            ) from None

    def close(self) -> None:
        self.service = None
        self._objects.close()
        self._pool.close()


_DECLARED_TABLES = ("speech_artifacts", "speech_workspaces")
_SUBJECT = "Artifact reader"


def _verify_read_only(connection: SpeechConnection, schema: str) -> None:
    """The reader may select two tables and hold nothing else (#561).

    Table and column authority are judged separately: a column grant leaves
    ``has_table_privilege`` false, so each column decision uses the column
    helpers. REFERENCES and TRIGGER are whole-table grants a read-only reader
    never needs, and any grant outside the declared set -- table or column --
    is refused by name.
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
    for table in _DECLARED_TABLES:
        qualified = f"{schema}.{table}"
        row = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS held",
            (qualified,),
        ).fetchone()
        if row is None or not row["held"]:
            raise ValueError(
                f"{_SUBJECT} lacks SELECT on {table}; grant SELECT on that table "
                "or correct the configured schema"
            )
        refuse_table_privileges(
            connection,
            role,
            qualified,
            table,
            ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_SUBJECT,
        )
        refuse_column_privileges(
            connection,
            role,
            qualified,
            table,
            ("INSERT", "UPDATE", "REFERENCES"),
            subject=_SUBJECT,
        )


__all__ = ["ArtifactRuntime"]
