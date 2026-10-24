"""Composition and owned lifetime of asset ports; never install DDL or grants."""

from __future__ import annotations

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.assets import AssetService, TextAssetInspector
from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.domain.assets import AssetError, AssetPolicy
from speechnexus_backend.speech.infrastructure.assets_minio import MinioAssetObjects
from speechnexus_backend.speech.infrastructure.assets_postgres import PostgresAssetMetadata
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.workspaces_postgres import PostgresWorkspaceReads


class AssetRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.object_storage_access_key is None or settings.object_storage_secret_key is None:
            raise ValueError("Explicit object-storage credentials are required")
        self._schema = settings.database_schema
        self._pool = speech_pool(settings.database_url.get_secret_value(), schema=self._schema)
        self.workspaces = WorkspaceService(PostgresWorkspaceReads(self._pool))
        self._objects = MinioAssetObjects(
            str(settings.object_storage_endpoint),
            settings.object_storage_bucket,
            settings.object_storage_access_key.get_secret_value(),
            settings.object_storage_secret_key.get_secret_value(),
        )
        self._metadata = PostgresAssetMetadata(
            self._pool, namespace_digest=self._objects.namespace_digest
        )
        self.service = AssetService(
            self._metadata,
            self._objects,
            TextAssetInspector(),
            AssetPolicy(settings.asset_default_ttl_seconds, settings.asset_max_bytes),
        )

    def open(self) -> None:
        self._pool.open()
        try:
            # Check the configured runtime principal, not a migration fallback.
            # This is an owner/DDL guard, not a full deployment privilege audit.
            with self._pool.connection() as connection:
                connection.execute("SET LOCAL statement_timeout = '3s'")
                row = connection.execute(
                    "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s, 'CREATE') "
                    "OR pg_catalog.pg_has_role(d.datdba, 'USAGE') OR EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = %s "
                    "AND pg_catalog.pg_has_role(c.relowner, 'USAGE')) AS forbidden "
                    "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                    "WHERE r.rolname = CURRENT_USER AND d.datname = CURRENT_DATABASE()",
                    (self._schema, self._schema),
                ).fetchone()
                if row is None or row["forbidden"]:
                    raise AssetError("asset_runtime_role_rejected")
            # S3 checks and original-identity reconciliation occur after the
            # short role-check transaction has released its connection.
            try:
                self._metadata.check_namespace()
            except AssetError as error:
                if error.code != "asset_storage_namespace_unbound":
                    raise
                # Validate an initial empty store before declaring it. Existing
                # declarations are checked before sending even S3 credentials.
                self._objects.check_writes_available()
                self._metadata.check_namespace(initialize=True)
            else:
                self._objects.check_writes_available()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._objects.close()
        self._pool.close()
