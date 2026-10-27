"""Composition and owned lifetime for read-only committed Artifact reads."""

from __future__ import annotations

import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.artifacts import ArtifactService
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactObjects
from speechnexus_backend.speech.infrastructure.artifacts_postgres import PostgresArtifactMetadata
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, speech_pool


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
            raise ValueError(
                "Artifact reader startup rejected; verify migration and reader role"
            ) from None

    def close(self) -> None:
        self.service = None
        self._objects.close()
        self._pool.close()


def _verify_read_only(connection: SpeechConnection, schema: str) -> None:
    for table in ("speech_artifacts", "speech_workspaces"):
        row = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS can_select, "
            "pg_catalog.has_table_privilege(CURRENT_USER, %s, 'INSERT,UPDATE,DELETE,TRUNCATE') "
            "AS can_mutate",
            (f"{schema}.{table}", f"{schema}.{table}"),
        ).fetchone()
        if row is None or not row["can_select"] or row["can_mutate"]:
            raise ValueError("Artifact reader must be read-only")
    row = connection.execute(
        "SELECT pg_catalog.has_schema_privilege(CURRENT_USER, %s, 'CREATE') AS can_create",
        (schema,),
    ).fetchone()
    if row is None or row["can_create"]:
        raise ValueError("Artifact reader must not create schema objects")


__all__ = ["ArtifactRuntime"]
