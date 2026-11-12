"""Composition and owned lifetime of asset ports; never install DDL or grants."""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from pathlib import Path, PurePosixPath

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
from speechnexus_backend.speech.infrastructure.audio_asset_inspector import (
    ProcessAudioAssetInspector,
)
from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    AudioInspectionProcess,
    InspectionProcessLimits,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.workspaces_postgres import PostgresWorkspaceReads

DEFAULT_BUBBLEWRAP = Path("/usr/bin/bwrap")
DEFAULT_PRLIMIT = Path("/usr/bin/prlimit")

_LOG = logging.getLogger(__name__)

# Narrow shutdown handle for the one inspector that owns an OS process. The
# AssetInspector protocol stays domain-only: no transport or supervisor type
# leaks into it, and "no closer" is stated by the composition root instead of
# guessed with isinstance at shutdown.
type InspectorCloser = Callable[[], bool]
# Prerequisites that only exist when the operator supplies them. Limit settings
# are absent here on purpose: they always carry a default, so their value cannot
# show that audio inspection was requested.
_AUDIO_PREREQUISITES: tuple[tuple[str, str], ...] = (
    ("audio_inspection_runtime_root", "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"),
    ("audio_inspection_command", "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"),
    ("audio_inspection_launcher_python", "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON"),
    ("audio_inspection_bubblewrap", "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP"),
    ("audio_inspection_prlimit", "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT"),
)


def _configured_audio_prerequisites(settings: Settings) -> list[str]:
    """Name every audio prerequisite the operator explicitly configured.

    ``model_fields_set`` records values supplied through the constructor or the
    environment, so it distinguishes operator intent from a field pydantic
    filled with its default. A supplied blank counts as unconfigured: the
    settings validators already normalize blank selectors, and Compose forwards
    empty values for variables it does not set. Only fields without a usable
    default appear in ``_AUDIO_PREREQUISITES``; limits such as
    ``audio_inspection_max_duration_ms`` never count as evidence of intent.
    """
    configured: list[str] = []
    for field, variable in _AUDIO_PREREQUISITES:
        if field not in settings.model_fields_set:
            continue
        value = getattr(settings, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        configured.append(variable)
    return configured


def _inspection_root(settings: Settings) -> Path:
    variable = "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"
    root = settings.audio_inspection_runtime_root
    if root is None:
        raise ValueError(f"{variable} is required when SPEECHNEXUS_ASSET_INSPECTOR=audio")
    resolved = root.resolve()
    # The supervisor rejects "/" as an image; fail before Popen with the setting's name.
    if not root.is_absolute() or resolved == Path("/") or not resolved.is_dir():
        raise ValueError(f"{variable} must name a private inspection image directory: {root}")
    return resolved


def _inspection_command(settings: Settings) -> tuple[str, ...]:
    variable = "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"
    raw = settings.audio_inspection_command
    if not raw.strip():
        raise ValueError(f"{variable} is required when SPEECHNEXUS_ASSET_INSPECTOR=audio")
    try:
        command = tuple(shlex.split(raw))
    except ValueError:
        raise ValueError(f"{variable} is not a valid shell command") from None
    if not command:
        raise ValueError(f"{variable} is not a valid shell command")
    executable = PurePosixPath(command[0])
    if (
        not executable.is_absolute()
        or ".." in executable.parts
        or any("\0" in argument or len(argument) > 8192 for argument in command)
    ):
        raise ValueError(f"{variable} must start with an absolute image executable")
    return command


def _inspection_launcher(settings: Settings) -> str:
    variable = "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON"
    interpreter = PurePosixPath(settings.audio_inspection_launcher_python)
    if not interpreter.is_absolute() or ".." in interpreter.parts or "\0" in str(interpreter):
        raise ValueError(f"{variable} must be an absolute interpreter path inside the image")
    return settings.audio_inspection_launcher_python


def _inspection_tool(variable: str, configured: Path | None, default: Path) -> Path:
    path = configured if configured is not None else default
    if not path.is_file():
        raise ValueError(f"{variable} does not name an installed executable: {path}")
    return path


def _build_audio_inspector(
    settings: Settings,
) -> tuple[ProcessAudioAssetInspector, InspectorCloser]:
    root = _inspection_root(settings)
    command = _inspection_command(settings)
    launcher = _inspection_launcher(settings)
    bubblewrap = _inspection_tool(
        "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP",
        settings.audio_inspection_bubblewrap,
        DEFAULT_BUBBLEWRAP,
    )
    prlimit = _inspection_tool(
        "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT",
        settings.audio_inspection_prlimit,
        DEFAULT_PRLIMIT,
    )
    process = AudioInspectionProcess(
        root,
        command,
        InspectionProcessLimits(
            input_bytes=settings.audio_inspection_max_input_bytes,
            memory_bytes=settings.audio_inspection_memory_bytes,
            cpu_seconds=settings.audio_inspection_cpu_seconds,
            wall_seconds=settings.audio_inspection_wall_seconds,
            output_bytes=settings.audio_inspection_output_bytes,
        ),
        launcher_python=launcher,
        bubblewrap=bubblewrap,
        prlimit=prlimit,
    )
    inspector = ProcessAudioAssetInspector(
        process,
        max_duration_ms=settings.audio_inspection_max_duration_ms,
        max_receipt_bytes=settings.audio_inspection_max_receipt_bytes,
    )
    # The supervisor is the only inspector part with real OS teardown. Hand the
    # runtime its close handle so shutdown never has to name its type.
    return inspector, process.close


def _select_asset_inspector(
    settings: Settings,
) -> tuple[AssetInspector, InspectorCloser | None]:
    """Build the configured inspector and its optional supervisor shutdown handle.

    ``None`` states that this inspector owns no process to tear down; it is not
    inferred from the object's class, so the runtime shutdown path stays free of
    infrastructure imports and concrete-type checks.
    """
    if settings.asset_inspector == "audio":
        return _build_audio_inspector(settings)
    configured = _configured_audio_prerequisites(settings)
    if configured:
        names = ", ".join(configured)
        raise ValueError(
            f"{names} configured but SPEECHNEXUS_ASSET_INSPECTOR=text; "
            "select audio or unset every audio inspection prerequisite"
        )
    return TextAssetInspector(), None


def build_asset_inspector(settings: Settings) -> AssetInspector:
    """Select the configured inspector; audio misconfiguration never becomes text.

    Every audio prerequisite is validated here, at composition time, so a
    deployment either starts with the configured inspector or fails naming the
    setting that is missing, unusable or absent from the host. Selecting ``text``
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
        """Stop the inspector supervisor, then release the owned transports.

        The inspector is stopped first: it owns a live OS process and pidfds
        that no transport teardown can reach, and it is the only step that can
        report an unconfirmed stop. Stopping it before the pool and object
        client also sets the supervisor's ``_closed`` flag while those
        transports still exist, so no new inspection can start into a runtime
        that is already being dismantled. This ordering is not a dependency:
        an inspection reads only the prepared body and the supervisor pipe, not
        ``_objects`` or ``_pool``.

        Returns ``False`` when the inspector did not confirm that its
        supervisor stopped. The transports are still closed, but callers must
        not read that as a clean shutdown: the unconfirmed stop is logged here
        and returned. No exception is raised because this also runs while
        reporting an ``open()`` failure, where it would mask the startup error.
        """
        stopped = True
        try:
            if self._close_inspector is not None:
                stopped = self._close_inspector()
                if not stopped:
                    _LOG.warning(
                        "Asset inspector did not confirm its supervisor stopped; "
                        "an inspection process may survive shutdown"
                    )
        finally:
            self._objects.close()
            self._pool.close()
        return stopped
