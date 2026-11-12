"""Composition and owned lifetime of asset ports; never install DDL or grants."""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import SecretStr

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.assets import (
    AssetInspector,
    AssetService,
    TextAssetInspector,
)
from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.domain.assets import AssetError, AssetPolicy
from speechnexus_backend.speech.infrastructure.assets_minio import MinioAssetObjects
from speechnexus_backend.speech.infrastructure.assets_postgres import PostgresAssetMetadata
from speechnexus_backend.speech.infrastructure.audio_inspection_http import (
    HttpAudioAssetInspector,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.workspaces_postgres import PostgresWorkspaceReads

_LOG = logging.getLogger(__name__)

# Narrow shutdown handle for an inspector that owns resources. The AssetInspector
# protocol stays domain-only: no transport type leaks into it, and "no closer" is
# stated by the composition root instead of guessed with isinstance at shutdown.
type InspectorCloser = Callable[[], bool]

_INFO = (
    "audio inspection runs in the asset-inspector deployment unit; configure "
    "SPEECHNEXUS_ASSET_INSPECTION_URL and SPEECHNEXUS_ASSET_INSPECTION_TOKEN"
)
# Settings the inspector container owns now. They stay declared for one release
# so a stale environment is diagnosed instead of silently ignored: the old values
# no longer change any limit here, and an empty value (Compose forwards one for
# every unset variable) counts as unconfigured.
_RETIRED_PREREQUISITES: tuple[tuple[str, str], ...] = (
    ("audio_inspection_runtime_root", "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"),
    ("audio_inspection_command", "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"),
    ("audio_inspection_launcher_python", "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON"),
    ("audio_inspection_bubblewrap", "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP"),
    ("audio_inspection_prlimit", "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT"),
)
# Resource limits that moved to the inspector unchanged. Compose forwards their
# publish-time defaults, so equality with the retired default is the normal case;
# any other value is an operator intent this process can no longer honour.
_MIGRATED_LIMITS: tuple[tuple[str, str, int, str], ...] = (
    (
        "audio_inspection_memory_bytes",
        "SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES",
        256 * 1024**2,
        "SPEECHNEXUS_INSPECTION_MEMORY_BYTES",
    ),
    (
        "audio_inspection_cpu_seconds",
        "SPEECHNEXUS_AUDIO_INSPECTION_CPU_SECONDS",
        5,
        "SPEECHNEXUS_INSPECTION_CPU_SECONDS",
    ),
    (
        "audio_inspection_output_bytes",
        "SPEECHNEXUS_AUDIO_INSPECTION_OUTPUT_BYTES",
        4096,
        "SPEECHNEXUS_INSPECTION_OUTPUT_BYTES",
    ),
)
# Prerequisites that only exist when the operator configures them. Limit settings
# are absent on purpose: they always carry a default, so a value cannot show that
# audio inspection was requested.
_INSPECTOR_PREREQUISITES: tuple[tuple[str, str], ...] = (
    ("asset_inspection_url", "SPEECHNEXUS_ASSET_INSPECTION_URL"),
    ("asset_inspection_token", "SPEECHNEXUS_ASSET_INSPECTION_TOKEN"),
)


def _configured(settings: Settings, fields: tuple[tuple[str, str], ...]) -> list[str]:
    """Name every prerequisite the operator explicitly configured.

    ``model_fields_set`` records values supplied through the constructor or the
    environment, so it distinguishes operator intent from a field pydantic filled
    with its default. A supplied blank counts as unconfigured: the settings
    validators already normalize blank selectors, and Compose forwards empty
    values for variables it does not set.
    """
    configured: list[str] = []
    for field, variable in fields:
        if field not in settings.model_fields_set:
            continue
        value = getattr(settings, field)
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        configured.append(variable)
    return configured


def _refuse_retired_inspector_settings(settings: Settings) -> None:
    """Fail startup instead of silently ignoring a setting this process retired.

    An ignored target, launcher path or resource limit would leave the operator
    believing a limit is enforced while the inspector container uses its own.
    """
    retired = _configured(settings, _RETIRED_PREREQUISITES)
    if retired:
        names = ", ".join(retired)
        raise ValueError(f"{names} no longer select a local inspection process; {_INFO}")
    drifted = [
        (variable, target)
        for field, variable, default, target in _MIGRATED_LIMITS
        if field in settings.model_fields_set and getattr(settings, field) != default
    ]
    if drifted:
        names = "; ".join(
            f"{variable} is owned by the inspector container now, set {target}"
            for variable, target in drifted
        )
        raise ValueError(names)


def _build_http_audio_inspector(
    settings: Settings,
) -> tuple[HttpAudioAssetInspector, InspectorCloser]:
    """Build the private HTTP inspector or fail naming the exact variable.

    No implicit default endpoint, host or token is invented here: a missing,
    unusable or self-contradictory configuration stops startup instead of
    degrading to the text inspector.
    """
    url_variable = "SPEECHNEXUS_ASSET_INSPECTION_URL"
    token_variable = "SPEECHNEXUS_ASSET_INSPECTION_TOKEN"
    url = settings.asset_inspection_url
    if url is None:
        raise ValueError(f"{url_variable} is required when SPEECHNEXUS_ASSET_INSPECTOR=audio")
    # The endpoint path is fixed by the protocol. Accepting a base URL with a path
    # would let two spellings of the same deployment look different.
    if (url.path or "/") != "/" or url.query or url.fragment:
        raise ValueError(
            f"{url_variable} must name the inspector origin only, without a path, "
            "query or fragment: the inspection path is fixed"
        )
    token = settings.asset_inspection_token
    if token is None:
        raise ValueError(f"{token_variable} is required when SPEECHNEXUS_ASSET_INSPECTOR=audio")
    wall_seconds = settings.audio_inspection_wall_seconds
    if settings.asset_inspection_timeout_seconds < wall_seconds + 2:
        raise ValueError(
            "SPEECHNEXUS_ASSET_INSPECTION_TIMEOUT_SECONDS must exceed "
            "SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS by at least 2 s; got "
            f"{settings.asset_inspection_timeout_seconds} and {wall_seconds}"
        )
    inspector = HttpAudioAssetInspector(
        str(url),
        token,
        timeout_seconds=settings.asset_inspection_timeout_seconds,
        wall_seconds=wall_seconds,
        max_duration_ms=settings.audio_inspection_max_duration_ms,
        max_input_bytes=settings.audio_inspection_max_input_bytes,
        # The receipt bound is the smaller of the inspector's own cap and this
        # process's read bound, so neither side can widen the other.
        max_receipt_bytes=min(
            settings.audio_inspection_max_receipt_bytes,
            settings.asset_inspection_max_response_bytes,
        ),
    )
    return inspector, inspector.close


def _select_asset_inspector(
    settings: Settings,
) -> tuple[AssetInspector, InspectorCloser | None]:
    """Build the configured inspector and its optional shutdown handle.

    ``None`` states that this inspector owns nothing to tear down; it is not
    inferred from the object's class, so the runtime shutdown path stays free of
    infrastructure imports and concrete-type checks.
    """
    _refuse_retired_inspector_settings(settings)
    if settings.asset_inspector == "audio":
        return _build_http_audio_inspector(settings)
    configured = _configured(settings, _INSPECTOR_PREREQUISITES)
    if configured:
        names = ", ".join(configured)
        raise ValueError(
            f"{names} configured but SPEECHNEXUS_ASSET_INSPECTOR=text; "
            "select audio or unset every asset-inspection setting"
        )
    return TextAssetInspector(), None


def build_asset_inspector(settings: Settings) -> AssetInspector:
    """Select the configured inspector; audio misconfiguration never becomes text.

    Every audio prerequisite is validated here, at composition time, so a
    deployment either starts with the configured inspector or fails naming the
    setting that is missing, unusable or self-contradictory. Selecting ``text``
    while explicitly configuring an audio prerequisite also fails: otherwise the
    operator's audio intent would silently degrade to an unavailable inspector.
    """
    return _select_asset_inspector(settings)[0]


class AssetRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.object_storage_access_key is None or settings.object_storage_secret_key is None:
            raise ValueError("Explicit object-storage credentials are required")
        # Resolve the inspector before creating owned transports: a rejected
        # audio configuration fails startup without opening a pool or S3 client.
        self.inspector, self._close_inspector = _select_asset_inspector(settings)
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
            self.inspector,
            AssetPolicy(settings.asset_default_ttl_seconds, settings.asset_max_bytes),
        )

    def open(self) -> None:
        try:
            self._pool.open()
            # Check the configured runtime principal, not a migration fallback.
            # This is an owner/DDL guard, not a full deployment privilege audit.
            with self._pool.connection() as connection:
                connection.execute("SET LOCAL statement_timeout = '3s'")
                identities = connection.execute(
                    "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
                ).fetchone()
                if identities is None:
                    raise AssetError("asset_runtime_role_rejected")
                # A libpq startup role can hide the login's authority. Inspect
                # both names without RESET ROLE (which restores that startup
                # role). MEMBER also sees switchable NOINHERIT owner roles.
                for role in dict.fromkeys((identities["login"], identities["effective"])):
                    row = connection.execute(
                        "SELECT r.rolsuper OR "
                        "pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
                        "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') OR EXISTS ("
                        "SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname=%s "
                        "AND pg_catalog.pg_has_role(%s::name,n.nspowner,'MEMBER')) OR EXISTS ("
                        "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = %s "
                        "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
                        "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                        "WHERE r.rolname = %s AND d.datname = CURRENT_DATABASE()",
                        (role, self._schema, role, self._schema, role, self._schema, role, role),
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

    def close(self) -> bool:
        """Refuse new inspections, release the inspector, then the transports.

        The inspector is closed first: it owns the only connection pool a
        dismantled runtime must not keep using, and closing it sets the flag that
        refuses new inspections while the other transports still exist. A request
        already inside an inspection fails as ``503`` and leaves no half-written
        state, because inspection precedes every database transaction and object
        PUT. This ordering is not a dependency: an inspection reads only the
        prepared body, not ``_objects`` or ``_pool``.

        Returns ``False`` only when the inspector did not confirm it stopped. The
        HTTP inspector owns no process to confirm, so it reports ``True``: the
        inspection container belongs to the deployment unit and this process
        never claims to have stopped it. A future closer that cannot confirm a
        stop is logged here and returned rather than raised, because this also
        runs while reporting an ``open()`` failure, where it would mask the
        startup error.
        """
        stopped = True
        try:
            if self._close_inspector is not None:
                stopped = self._close_inspector()
                if not stopped:
                    _LOG.warning(
                        "Asset inspector did not confirm it stopped; "
                        "inspection capacity may outlive shutdown"
                    )
        finally:
            self._objects.close()
            self._pool.close()
        return stopped
