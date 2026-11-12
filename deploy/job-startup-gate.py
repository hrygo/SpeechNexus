"""Deployment-owned startup gate for the public Job dispatcher container.

`job-dispatch-worker` in `deploy/compose.yaml` runs this module as its
entrypoint. It verifies every deployment prerequisite that the dispatcher
process cannot discover until *after* it has claimed a lease, prints one named
failure per missing item, and only then `exec`s `speechnexus-job-dispatch-worker`.
A missing bucket, a stale migration, an unversioned staging bucket or a role
without the write columns therefore fails while the container starts, before
any Job can be claimed, instead of surfacing as a Job stuck in `running`.

Two inputs are deliberately *not* failures here:

- the real engine and its registered instance (prerequisite 4 of #572) are a
  deployment input this bootstrap cannot verify without engine credentials, so
  the gate prints a `pending` line naming them and continues;
- the API-side Job writer/admission DSNs are only visible to the API container.
  A blank one already fails creation with `503 job_unavailable`, which is before
  any claim; the gate reports that boundary on its pending line instead.

The SELECT probes, the role guard and the migration head come from the running
production code (`speechnexus_backend.job_dispatch_runtime` and the packaged
alembic scripts), so this file never re-states a privilege surface that could
drift. `tests/ci/test_reference_deploy_job_prereqs.py` guards the wiring and
`tests/unit/test_job_startup_gate.py` exercises the failure naming with fakes.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from minio.commonconfig import ENABLED

from speechnexus_backend.job_dispatch_runtime import (
    DISPATCH_ROLE_GUARD_SQL,
    DISPATCH_STARTUP_PROBES,
    DISPATCH_VOICE_VALIDATION_PROBES,
)

DSN_ENV = "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"
RUNTIME_DSN_ENV = "SPEECHNEXUS_DATABASE_URL"
SCHEMA_ENV = "SPEECHNEXUS_DATABASE_SCHEMA"
KINDS_ENV = "SPEECHNEXUS_JOB_DISPATCH_KINDS"
ENDPOINT_ENV = "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT"
ARTIFACT_BUCKET_ENV = "SPEECHNEXUS_OBJECT_STORAGE_BUCKET"
RESULT_BUCKET_ENV = "SPEECHNEXUS_EXECUTION_RESULT_BUCKET"
ACCESS_KEY_ENV = "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY"
SECRET_KEY_ENV = "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY"
PRIVATE_DIR_ENV = "SPEECHNEXUS_JOB_GATE_PRIVATE_DIR"
DEFAULT_PRIVATE_DIR = "/run/speechnexus-private"
WORKER_COMMAND = "speechnexus-job-dispatch-worker"

# The dispatcher writes these columns after it claims. `job_dispatch_grants` and
# `cancellation_reconciliation_grants` declare them, and this gate re-checks them
# with `has_column_privilege` before the worker exists. The read-only self-check
# in scripts/dev-env/check-async-job-prereqs.py keeps the same surface; a unit
# test compares both copies and the grant functions they come from.
DISPATCH_WRITE_SURFACE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "speech_jobs",
        "UPDATE",
        ("state", "status_version", "result_json", "error_code", "updated_at_us"),
    ),
    (
        "coordination_jobs",
        "UPDATE",
        ("claim_owner_id", "claim_expires_at_us", "claim_version", "job_state"),
    ),
    (
        "speech_job_events",
        "INSERT",
        (
            "job_id",
            "event_seq",
            "state",
            "occurred_at_us",
            "kind",
            "progress_unit",
            "progress_completed",
            "progress_total",
        ),
    ),
    (
        "speech_job_cancellation_not_submitted",
        "INSERT",
        ("job_id", "observed_at_us", "observer"),
    ),
)

DISPATCH_GRANT_GROUPS = (
    "job_runtime_grants",
    "job_dispatch_grants",
    "public_job_grants",
    "cancellation_reconciliation_grants",
    "coordination_runtime_grants",
)

GATEWAY_CONFIGS = {
    "asr": "gateway-file-asr.json",
    "tts": "gateway-file-tts.json",
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{2,62}")
PROBE_TABLE = re.compile(r"\bFROM\s+([A-Za-z0-9_]+)")


@dataclass(frozen=True)
class BucketCheck:
    """One bucket name plus the environment keys that selected it."""

    name: str
    env_keys: str


def _identifier(value: str) -> str:
    if IDENTIFIER.fullmatch(value) is None:
        raise ValueError("schema must be a safe lowercase SQL identifier")
    return value


def dispatch_kinds(env: Mapping[str, str]) -> tuple[str, ...]:
    """Return the configured lane kinds, defaulting to the worker's own default."""
    raw = env.get(KINDS_ENV, "asr,tts")
    kinds = tuple(part.strip() for part in raw.split(",") if part.strip())
    return kinds or ("asr", "tts")


def dsn_findings(env: Mapping[str, str]) -> list[str]:
    """Name the blank or unparsable dispatcher DSN before anything connects."""
    raw = env.get(RUNTIME_DSN_ENV, "").strip()
    if not raw:
        return [
            f"{DSN_ENV} is blank: the dispatcher has no database role and refuses to "
            "claim a lease; fill the non-owner dispatch DSN in deploy/compose.env"
        ]
    parts = urlsplit(raw)
    if (
        parts.scheme not in {"postgresql", "postgres"}
        or not unquote(parts.username or "")
        or not parts.hostname
    ):
        return [
            f"{DSN_ENV} is not a usable postgresql:// DSN with a host and user; "
            "the dispatcher has nowhere to connect"
        ]
    return []


def role_guard_findings(connection: Any, schema: str, login: str, effective: str) -> list[str]:
    """Reject superuser, schema CREATE, database owner or table-owner membership."""
    findings: list[str] = []
    for role in dict.fromkeys((login, effective)):
        row = connection.execute(
            DISPATCH_ROLE_GUARD_SQL, {"role": role, "schema": _identifier(schema)}
        ).fetchone()
        if row is None or row[0]:
            findings.append(
                f"dispatch role {role} is a superuser, owns schema CREATE, owns the "
                f"database or is a member of a table owner in {schema}; provision the "
                "dedicated non-owner role from docs/operations/compose-minimal.md §6"
            )
    return findings


def _probe_table(statement: str) -> str:
    match = PROBE_TABLE.search(statement)
    return match.group(1) if match else "unknown relation"


def probe_findings(connection: Any, kinds: Sequence[str]) -> tuple[list[str], list[str]]:
    """Run the worker's own SELECT probes; return (findings, passed tables).

    The first rejected probe stops the check: a failed statement aborts the
    connection's transaction, so continuing would only report cascade errors
    instead of the one table that is actually missing its grant.
    """
    statements = list(DISPATCH_STARTUP_PROBES)
    if "voice_validation" in kinds:
        statements.extend(DISPATCH_VOICE_VALIDATION_PROBES)
    passed: list[str] = []
    for statement in statements:
        table = _probe_table(statement)
        try:
            connection.execute(statement)
        except Exception:
            attributes = (
                "; voice_validation also needs voice_validation_worker_grants"
                if table in {"speech_voice_draft_revisions", "speech_assets"}
                and "voice_validation" in kinds
                else ""
            )
            return [
                f"dispatch role cannot run the startup probe on {table}: install "
                + ", ".join(DISPATCH_GRANT_GROUPS)
                + " for this role"
                + attributes
            ], passed
        passed.append(table)
    return [], passed


def write_privilege_sql(schema: str) -> str:
    """Return the has_column_privilege query for the post-claim write surface."""
    rows = ", ".join(
        f"('{table}','{column}','{privilege}')"
        for table, privilege, columns in DISPATCH_WRITE_SURFACE
        for column in columns
    )
    return (
        "SELECT v.t || '.' || v.c || ' (' || v.p || ')' FROM (VALUES "
        f"{rows}"
        ") AS v(t, c, p) WHERE NOT pg_catalog.has_column_privilege("
        "current_user, "
        f"'{_identifier(schema)}.' || v.t, v.c, v.p)"
    )


def write_privilege_findings(connection: Any, schema: str) -> tuple[list[str], int]:
    """Return named missing columns and the number of checked columns."""
    rows = connection.execute(write_privilege_sql(schema)).fetchall()
    checked = sum(len(columns) for _, _, columns in DISPATCH_WRITE_SURFACE)
    findings = [
        f"dispatch role lacks {row[0]}; it would fail after claiming a Job - install "
        "job_dispatch_grants and cancellation_reconciliation_grants for this role"
        for row in rows
    ]
    return findings, checked


def expected_migration_head() -> str:
    """Return the packaged single migration head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option(
        "script_location",
        str(files("speechnexus_backend.speech.infrastructure").joinpath("migrations")),
    )
    return str(ScriptDirectory.from_config(config).get_current_head())


def migration_findings(
    connection: Any, schema: str, expected_head: str
) -> tuple[list[str], str | None]:
    """Return (findings, verified head), never failing on a missing grant.

    ``to_regclass`` needs no grant, so "the migration profile never ran" is always
    a named startup failure. Comparing the exact head needs a read-only ``SELECT``
    that the provisioning script installs; a deployment whose dispatch role
    predates that grant keeps the column probes (they cover what this build uses)
    and the gate reports the head as pending instead of failing an upgrade.
    """
    table = f"{_identifier(schema)}.alembic_version"
    try:
        present = connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()
    except Exception:
        present = None
    if present is None or present[0] is None:
        return [
            f"{table} is missing: run the migration profile "
            "(docker compose --profile migration run --rm migrate) before the "
            "job-dispatch profile"
        ], None
    try:
        rows = connection.execute(f"SELECT version_num FROM {table}").fetchall()
    except Exception:
        return [], None
    versions = [row[0] for row in rows]
    if len(versions) != 1:
        return [
            f"{table} holds {len(versions)} versions ({', '.join(map(str, versions))}): "
            f"the migration profile must converge on the single head {expected_head}"
        ], None
    if versions[0] != expected_head:
        return [
            f"migration is behind: {table} is {versions[0]}, the artifact head is "
            f"{expected_head}; run the migration profile before the job-dispatch profile"
        ], None
    return [], expected_head


def bucket_findings(client: Any, buckets: Iterable[BucketCheck]) -> tuple[list[str], list[str]]:
    """Require every referenced bucket to exist and return immutable versions."""
    findings: list[str] = []
    passed: list[str] = []
    for bucket in buckets:
        try:
            exists = bool(client.bucket_exists(bucket.name))
        except Exception:
            findings.append(
                f"object storage is unreachable for {bucket.env_keys}={bucket.name}; "
                f"check {ENDPOINT_ENV} and the object-storage credentials"
            )
            continue
        if not exists:
            findings.append(
                f"bucket {bucket.name} ({bucket.env_keys}) does not exist: minio-init "
                "creates and versions it; run the base topology before job-dispatch"
            )
            continue
        try:
            versioning = client.get_bucket_versioning(bucket.name)
        except Exception:
            findings.append(
                f"bucket {bucket.name} versioning cannot be read; minio-init must "
                "enable it before the dispatcher starts"
            )
            continue
        if versioning.status != ENABLED:
            findings.append(
                f"bucket {bucket.name} ({bucket.env_keys}) is not versioned: the Job "
                "result read-back requires object versions; minio-init enables them"
            )
            continue
        passed.append(bucket.name)
    return findings, passed


def referenced_buckets(env: Mapping[str, str]) -> tuple[list[BucketCheck], list[str]]:
    """Collect the Artifact and execution-result buckets, de-duplicated by name."""
    findings: list[str] = []
    selected: dict[str, list[str]] = {}
    for key in (ARTIFACT_BUCKET_ENV, RESULT_BUCKET_ENV):
        name = env.get(key, "").strip()
        if not name:
            findings.append(
                f"{key} is blank: the dispatcher cannot name the bucket it publishes "
                "Artifacts to or reads gateway staging results from"
            )
            continue
        selected.setdefault(name, []).append(key)
    return (
        [BucketCheck(name, "/".join(keys)) for name, keys in selected.items()],
        findings,
    )


def gateway_config_findings(
    private_dir: Path, result_bucket: str, kinds: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """Compare the gateway's own `objects.result_bucket` with the worker's env.

    Returns (findings, checked files, pending lanes).
    """
    findings: list[str] = []
    checked: list[str] = []
    pending: list[str] = []
    for kind in dict.fromkeys(kinds):
        filename = GATEWAY_CONFIGS.get(kind)
        if filename is None:
            pending.append(
                f"gateway lane {kind} is not assembled by this bootstrap; its "
                "result_bucket cannot be verified and no engine can serve it"
            )
            continue
        path = private_dir / filename
        if not path.is_file():
            findings.append(
                f"{filename} is missing under {private_dir}: copy the private gateway "
                "config and mount SPEECHNEXUS_PRIVATE_CONFIG_DIR before job-dispatch"
            )
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            objects = document["objects"]
            configured = objects["result_bucket"]
        except Exception:
            findings.append(
                f"{filename} has no readable objects.result_bucket: the gateway "
                "cannot stage this lane's results"
            )
            continue
        if configured != result_bucket:
            findings.append(
                f"{filename} objects.result_bucket={configured} but "
                f"{RESULT_BUCKET_ENV}={result_bucket}: the worker would fail after "
                "claiming the first Job; make them identical"
            )
            continue
        checked.append(filename)
    return findings, checked, pending


def object_storage_client(env: Mapping[str, str]) -> tuple[Any, list[str]]:
    """Build a MinIO client, or name the missing connection inputs."""
    endpoint = env.get(ENDPOINT_ENV, "").strip()
    access = env.get(ACCESS_KEY_ENV, "").strip()
    secret = env.get(SECRET_KEY_ENV, "").strip()
    missing = [
        key
        for key, value in (
            (ENDPOINT_ENV, endpoint),
            (ACCESS_KEY_ENV, access),
            (SECRET_KEY_ENV, secret),
        )
        if not value
    ]
    if missing:
        return None, [f"{', '.join(missing)} blank: the dispatcher cannot verify the Job buckets"]
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None, [f"{ENDPOINT_ENV} is not an http(s) origin"]
    from minio import Minio

    client = Minio(
        parts.netloc,
        access_key=access,
        secret_key=secret,
        secure=parts.scheme == "https",
    )
    return client, []


def database_findings(
    connect: Callable[[str], Any], env: Mapping[str, str], expected_head: str
) -> tuple[list[str], list[str], list[str]]:
    """Run the role guard, the SELECT probes, writes and the migration check."""
    findings = dsn_findings(env)
    if findings:
        return findings, [], []
    dsn = env[RUNTIME_DSN_ENV].strip()
    schema = env.get(SCHEMA_ENV, "speech").strip() or "speech"
    kinds = dispatch_kinds(env)
    try:
        connection = connect(dsn)
    except Exception as error:
        # psycopg reports the host, database and server error but never the
        # password, so this stays safe to print and tells auth from network
        # failures instead of only "not ready".
        reason = str(error).strip().splitlines()[0][:300]
        return (
            [
                f"{DSN_ENV} cannot connect to PostgreSQL: {reason or 'no detail'}; "
                "the dispatch role or the database network is not ready"
            ],
            [],
            [],
        )
    passed: list[str] = []
    pending: list[str] = []
    try:
        connection.execute("SELECT set_config('search_path', %s, true)", (schema,))
        identities = connection.execute("SELECT SESSION_USER, CURRENT_USER").fetchone()
        if identities is None:
            return ["dispatch role identity is unavailable"], passed, pending
        findings += role_guard_findings(connection, schema, identities[0], identities[1])
        if findings:
            return findings, passed, pending
        probe_failures, _ = probe_findings(connection, kinds)
        findings += probe_failures
        if probe_failures:
            return findings, passed, pending
        write_failures, checked_writes = write_privilege_findings(connection, schema)
        findings += write_failures
        if write_failures:
            return findings, passed, pending
        migration_failures, verified_head = migration_findings(connection, schema, expected_head)
        findings += migration_failures
        if findings:
            return findings, passed, pending
        passed.append(
            f"role guard, startup SELECT probes and {checked_writes} post-claim write columns"
        )
        if verified_head is not None:
            passed.append(f"migration head {verified_head}")
        else:
            pending.append(
                "migration head not verifiable: the dispatch role cannot read "
                f"{schema}.alembic_version; grant the read-only SELECT from "
                "docs/operations/compose-minimal.md §6 for an exact-head check "
                "(the startup SELECT probes still verify the columns this build uses)"
            )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    return findings, passed, pending


def main(argv: Sequence[str] | None = None) -> int:
    env = os.environ
    arguments = list(argv if argv is not None else sys.argv[1:])
    expected_head = expected_migration_head()
    failures: list[str] = []
    passed: list[str] = []

    db_failures, db_passed, db_pending = database_findings(_psycopg_connect, env, expected_head)
    failures += db_failures
    if db_passed and not db_failures:
        passed.append("database: " + ", ".join(db_passed))

    buckets, bucket_env_failures = referenced_buckets(env)
    failures += bucket_env_failures
    if buckets:
        client, client_failures = object_storage_client(env)
        failures += client_failures
        if client is not None:
            bucket_failures, bucket_passed = bucket_findings(client, buckets)
            failures += bucket_failures
            if bucket_passed and not bucket_failures:
                passed.append("object storage: versioned buckets " + ", ".join(bucket_passed))

    kinds = dispatch_kinds(env)
    result_bucket = env.get(RESULT_BUCKET_ENV, "").strip()
    private_dir = Path(env.get(PRIVATE_DIR_ENV, DEFAULT_PRIVATE_DIR))
    config_failures, config_checked, lane_pending = gateway_config_findings(
        private_dir, result_bucket, kinds
    )
    failures += config_failures
    config_pending = [*db_pending]
    config_pending += lane_pending
    if config_checked:
        passed.append("gateway staging bucket matches " + ", ".join(config_checked))

    print("job dispatch startup gate: deployment prerequisites before any claim")
    for line in passed:
        print(f"  ok: {line}")
    for line in config_pending:
        print(f"  pending: {line}")
    print(
        "  pending: real engine and a registered ready instance are a deployment "
        "input this gate cannot verify; register one (engine-registration profile) "
        "before submitting a Job"
    )
    print(
        "  pending: API-side writer/admission DSNs live in the api container; a "
        "blank one already fails Job creation with 503 job_unavailable, before any claim"
    )
    if failures:
        for line in failures:
            print(f"  FAIL: {line}")
        print(
            f"job dispatch startup gate: {len(failures)} prerequisite(s) missing; "
            f"refusing to start {WORKER_COMMAND}"
        )
        return 1
    print("job dispatch startup gate: OK")
    command = arguments or [WORKER_COMMAND]
    os.execvp(command[0], command)
    return 1  # pragma: no cover - execvp never returns on success


def _psycopg_connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, connect_timeout=10)


if __name__ == "__main__":
    sys.exit(main())
