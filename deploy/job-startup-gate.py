"""Deployment-owned startup gate for the public Job dispatcher container.

`job-dispatch-worker` in `deploy/compose.yaml` runs this module as its
entrypoint. It verifies the deployment prerequisites that the dispatcher process
cannot discover until *after* it has claimed a lease, prints one named failure
per missing item, and only then `exec`s `speechnexus-job-dispatch-worker`. A
missing bucket, a stale or unreadable migration head, an unversioned staging
bucket, a lane this bootstrap does not assemble or a role without the post-claim
write columns therefore fails while the container starts, before any Job can be
claimed, instead of surfacing as a Job stuck in `running` or failed after claim.

Every check is a hard failure except two inputs the gate deliberately reports as
`pending`:

- the real engine and its registered instance (prerequisite 4 of #572) are a
  deployment input this bootstrap cannot verify without engine credentials;
- the API-side Job writer/admission DSNs are only visible to the API container.
  A blank one already fails creation with `503 job_unavailable`, which is before
  any claim.

The migration head is a hard failure, not a `pending` line: a dispatch role
provisioned before the read-only `speech.alembic_version` grant is
indistinguishable from a database that is genuinely behind, so the gate refuses
to start and names the one `GRANT` that fixes it (upgrade prerequisite,
documented in `docs/operations/compose-minimal.md` §6). The startup SELECT
probes cannot stand in for it: they cover the columns this build reads, not the
views, tables, indexes or other columns a migration may add.

The SELECT probes, the role guard, the migration head, the Artifact INSERT
columns and the release-path UPDATE columns all come from the running production
code (`speechnexus_backend.job_dispatch_runtime` and
`speechnexus_backend.speech.infrastructure.*`), so this file never re-states a
privilege surface that could drift. `main()` takes its `env`, `connect`,
`client`, `head` and `execute` collaborators as parameters, which is how
`tests/unit/test_job_startup_gate.py` proves the wiring, the exit code and the
"no `exec` after a failure" ordering without PostgreSQL or MinIO;
`tests/ci/test_reference_deploy_job_prereqs.py` guards the text-level wiring.
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
from typing import Any, TextIO
from urllib.parse import unquote, urlsplit

from minio.commonconfig import ENABLED

from speechnexus_backend.job_dispatch_runtime import (
    DISPATCH_ROLE_GUARD_SQL,
    DISPATCH_STARTUP_PROBES,
    DISPATCH_VOICE_VALIDATION_PROBES,
)
from speechnexus_backend.speech.domain.job_dispatch import DISPATCHABLE_JOB_KIND_UNION
from speechnexus_backend.speech.infrastructure.artifact_schema import ARTIFACT_INSERT_COLUMNS
from speechnexus_backend.speech.infrastructure.coordination_grants import SPEECH_UPDATES

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

# The dispatcher writes these columns after it claims. `job_dispatch_grants`,
# `artifact_publisher_grants`, `cancellation_reconciliation_grants` and
# `coordination_runtime_grants` declare them, and this gate re-checks them with
# `has_column_privilege` before the worker exists. The read-only self-check in
# scripts/dev-env/check-async-job-prereqs.py keeps the same surface; a unit test
# compares both copies and the grant functions they come from.
#
# The tables below are every relation the dispatch worker writes after a claim,
# taken from the dispatcher code:
#   - the claim/renew/terminal transition (`PostgresDispatchQueue` and
#     `PostgresDispatchStateStore` in job_dispatch_postgres.py);
#   - the committed result Artifact (`ResultArtifactPublisher.publish` ->
#     `PostgresArtifactWriter.insert_ready` in results.py / artifacts_postgres.py);
#   - the capacity release of a terminal asr/tts Job (`PostgresLeaseSettler` ->
#     `LeaseService.release` -> lease_release_sql.py).
# `artifact_publisher_grants`/`coordination_runtime_grants` are the grant
# declarations the provisioning script installs for the dispatch role.
RELEASE_WRITE_TABLES = (
    "admission_leases",
    "admission_attempts",
    "workspace_quotas",
    "host_nodes",
    "engine_instances",
)
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
    # Result publication writes the Artifact row after the claim; the columns come
    # from the publisher's own declaration so an added column cannot slip through.
    ("speech_artifacts", "INSERT", ARTIFACT_INSERT_COLUMNS),
    # Capacity release of a terminal Job updates the same coordination columns the
    # bindings declare; deriving them keeps this gate and the grant in step.
    *((table, "UPDATE", tuple(SPEECH_UPDATES[table].split())) for table in RELEASE_WRITE_TABLES),
)

DISPATCH_GRANT_GROUPS = (
    "job_runtime_grants",
    "job_dispatch_grants",
    "public_job_grants",
    "cancellation_reconciliation_grants",
    "coordination_runtime_grants",
)

# The lanes this bootstrap assembles: `deploy/compose.yaml` forwards exactly the
# asr and tts private configs to the gateway, and the gate verifies those two
# lanes' `objects.result_bucket`. A kind outside this map is not served by the
# reference deployment, so the worker would claim it and fail after the claim.
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
        + ", ".join(DISPATCH_GRANT_GROUPS)
        + " for this role"
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
    connection: Any, schema: str, expected_head: str, role: str
) -> tuple[list[str], str | None]:
    """Return (findings, verified head); an unreadable head is a failure.

    ``to_regclass`` needs no grant, so "the migration profile never ran" is always
    a named startup failure. Reading the exact head needs the read-only ``SELECT``
    that the provisioning script installs. A role provisioned before that grant is
    indistinguishable from a genuinely stale database, so the check fails closed
    and names the one statement that fixes it instead of starting the worker on an
    unverified head. The startup SELECT probes are not a substitute: they cover
    the columns this build reads, not views, tables, indexes or other columns.
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
        target = '"' + role.replace('"', '""') + '"'
        return [
            f"{table} exists but the dispatch role cannot read it, so the migration "
            "head cannot be verified: run "
            f"GRANT SELECT ON {table} TO {target} (upgrade prerequisite) and restart "
            "the container; the gate does not start the worker on an unverified head"
        ], None
    versions = [row[0] for row in rows]
    if len(versions) != 1:
        return [
            f"{table} holds {len(versions)} versions ({', '.join(map(str, versions))}): "
            f"the migration profile must converge on the single head {expected_head}"
        ], None
    if versions[0] != expected_head:
        return [
            f"migration differs from the artifact head: {table} is {versions[0]}, the "
            f"artifact head is {expected_head}; run the migration profile before the "
            "job-dispatch profile"
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


def gateway_lane_findings(kinds: Sequence[str]) -> list[str]:
    """Reject every lane this bootstrap cannot serve, before any claim.

    The dispatcher claims any kind inside `DISPATCHABLE_JOB_KIND_UNION`, so a lane
    without an assembled gateway config would be claimed and then fail at submit
    or publication time - exactly the "fails after claim" shape this gate exists
    to remove. Both the typo case and the known-but-unassembled case are failures;
    the messages differ so an operator can tell them apart.
    """
    findings: list[str] = []
    for kind in dict.fromkeys(kinds):
        if kind not in DISPATCHABLE_JOB_KIND_UNION:
            findings.append(
                f"{KINDS_ENV} names {kind!r}, which is not a dispatchable Job kind "
                f"({', '.join(DISPATCHABLE_JOB_KIND_UNION)}); the worker refuses to start "
                "on an unknown lane"
            )
        elif kind not in GATEWAY_CONFIGS:
            findings.append(
                f"{KINDS_ENV} names {kind!r} but this bootstrap assembles no gateway lane "
                f"for it (only {', '.join(GATEWAY_CONFIGS)}); the worker would claim "
                f"{kind} Jobs and fail after the claim. Remove the kind, or assemble the "
                "lane (private gateway config, SPEECHNEXUS_GATEWAY_FILE_*_CONFIG_FILE and "
                "GATEWAY_CONFIGS) before enabling it"
            )
    return findings


def gateway_config_findings(
    private_dir: Path, result_bucket: str, kinds: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Compare the gateway's own `objects.result_bucket` with the worker's env.

    Only assembled lanes are read here; `gateway_lane_findings` rejects the rest.
    Returns (findings, checked files).
    """
    findings: list[str] = []
    checked: list[str] = []
    for kind in dict.fromkeys(kinds):
        filename = GATEWAY_CONFIGS.get(kind)
        if filename is None:
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
    return findings, checked


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
) -> tuple[list[str], list[str]]:
    """Run the role guard, the migration head, the SELECT probes and the writes.

    The migration head is compared before the column checks: a database that is
    behind must be named as such, instead of surfacing later as a missing column or
    a crashed probe in a surface this build happens to look at.
    """
    findings = dsn_findings(env)
    if findings:
        return findings, []
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
        )
    passed: list[str] = []
    try:
        connection.execute("SELECT set_config('search_path', %s, true)", (schema,))
        identities = connection.execute("SELECT SESSION_USER, CURRENT_USER").fetchone()
        if identities is None:
            return ["dispatch role identity is unavailable"], passed
        findings += role_guard_findings(connection, schema, identities[0], identities[1])
        if findings:
            return findings, passed
        migration_failures, verified_head = migration_findings(
            connection, schema, expected_head, identities[1]
        )
        findings += migration_failures
        if migration_failures:
            return findings, passed
        probe_failures, _ = probe_findings(connection, kinds)
        findings += probe_failures
        if probe_failures:
            return findings, passed
        try:
            write_failures, checked_writes = write_privilege_findings(connection, schema)
        except Exception as error:
            # A missing relation or column means the migration and this build
            # disagree; name that instead of printing a driver traceback.
            reason = str(error).strip().splitlines()[0][:300]
            return (
                [
                    f"the post-claim write columns cannot be read from {schema}: "
                    f"{reason or 'no detail'}; run the migration profile and re-install "
                    "the dispatch grants"
                ],
                passed,
            )
        findings += write_failures
        if write_failures:
            return findings, passed
        passed.append(
            "migration head "
            + str(verified_head)
            + ", role guard, startup SELECT probes and "
            + f"{checked_writes} post-claim write columns"
        )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    return findings, passed


def _psycopg_connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, connect_timeout=10)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    connect: Callable[[str], Any] = _psycopg_connect,
    client_factory: Callable[[Mapping[str, str]], tuple[Any, list[str]]] = object_storage_client,
    head: Callable[[], str] = expected_migration_head,
    execute: Callable[[str, Sequence[str]], Any] = os.execvp,
    output: TextIO | None = None,
) -> int:
    """Run every check, print one named line per item, then `exec` the worker.

    The collaborators are parameters so the tests can prove the wiring, the exit
    code and the ordering ("a failure returns 1 and never calls `execute`")
    without PostgreSQL, MinIO or a real `exec`. A successful run never returns:
    `execute` replaces the process, and the trailing `return 1` only satisfies the
    type checker for the case where an injected `execute` returns.
    """
    source = os.environ if env is None else env
    stream = sys.stdout if output is None else output
    arguments = list(argv if argv is not None else sys.argv[1:])
    expected_head = head()
    failures: list[str] = []
    passed: list[str] = []

    db_failures, db_passed = database_findings(connect, source, expected_head)
    failures += db_failures
    if db_passed and not db_failures:
        passed.append("database: " + ", ".join(db_passed))

    buckets, bucket_env_failures = referenced_buckets(source)
    failures += bucket_env_failures
    if buckets:
        store, client_failures = client_factory(source)
        failures += client_failures
        if store is not None:
            bucket_failures, bucket_passed = bucket_findings(store, buckets)
            failures += bucket_failures
            if bucket_passed and not bucket_failures:
                passed.append("object storage: versioned buckets " + ", ".join(bucket_passed))

    kinds = dispatch_kinds(source)
    result_bucket = source.get(RESULT_BUCKET_ENV, "").strip()
    private_dir = Path(source.get(PRIVATE_DIR_ENV, DEFAULT_PRIVATE_DIR))
    failures += gateway_lane_findings(kinds)
    config_failures, config_checked = gateway_config_findings(private_dir, result_bucket, kinds)
    failures += config_failures
    if config_checked:
        passed.append("gateway staging bucket matches " + ", ".join(config_checked))

    print("job dispatch startup gate: deployment prerequisites before any claim", file=stream)
    for line in passed:
        print(f"  ok: {line}", file=stream)
    print(
        "  pending: real engine and a registered ready instance are a deployment "
        "input this gate cannot verify; register one (engine-registration profile) "
        "before submitting a Job",
        file=stream,
    )
    print(
        "  pending: API-side writer/admission DSNs live in the api container; a "
        "blank one already fails Job creation with 503 job_unavailable, before any claim",
        file=stream,
    )
    if failures:
        for line in failures:
            print(f"  FAIL: {line}", file=stream)
        print(
            f"job dispatch startup gate: {len(failures)} prerequisite(s) missing; "
            f"refusing to start {WORKER_COMMAND}",
            file=stream,
        )
        return 1
    print("job dispatch startup gate: OK", file=stream)
    # `exec` replaces the process, so buffered stdout would be lost. The image sets
    # PYTHONUNBUFFERED, but the gate must not depend on that when it runs outside it.
    stream.flush()
    command = arguments or [WORKER_COMMAND]
    execute(command[0], command)
    return 1  # pragma: no cover - execvp never returns on success


if __name__ == "__main__":
    sys.exit(main())
