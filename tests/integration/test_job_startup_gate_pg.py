"""Real-database proof that the dispatcher startup gate refuses a bad premise (#1054).

`deploy/job-startup-gate.py` is the `job-dispatch-worker` container entrypoint: it
checks the deployment prerequisites *before* anything can claim a lease and names
each missing one. Its docstring promises five database shapes. Until this module
they were only proven against injected fakes (`tests/unit/test_job_startup_gate.py`)
and against the gate's own text (`tests/ci/test_reference_deploy_job_prereqs.py`);
no run had ever put a migrated schema and a real non-owner role behind it. #1051
closed the neighbouring gap (the grant functions themselves) but its own review
noted it builds the role and replays the grants, so it cannot answer "did the
deployed role really run the function". This module answers that question on the
real surface the gate sees.

Each case provisions a throwaway schema with the real Alembic migration, creates
four non-owner roles and installs the *reference* provisioning output
(`scripts/dev-env/provision-job-roles.py: statements`) - the same SQL the
deployment installs, not a copy. It then calls the gate's real `main()`, whose
`env` points at the dispatch role's DSN.

Coverage (one case per shape the issue requires):

1. `test_a_provisioned_role_passes_and_execs_the_worker_last` - the full grant set
   passes every check and the worker `exec` is the last side effect, after the
   `job dispatch startup gate: OK` line.
2. `test_a_missing_claim_column_update_names_the_column_and_the_grant_groups` -
   `REVOKE UPDATE (claim_owner_id, claim_expires_at_us, claim_version, job_state)`
   on `coordination_jobs` makes the gate exit non-zero and print one line per
   missing column that names the column and the grant groups to install, instead
   of a generic "permission denied".
3. `test_a_role_without_the_version_grant_is_refused_with_the_one_grant` -
   `REVOKE SELECT ON <schema>.alembic_version` is refused, and the line names the
   exact `GRANT` statement that fixes it.
4. `test_a_stale_head_is_refused_with_a_message_distinct_from_an_unreadable_one`
   - a schema stamped at an older revision is refused with a message about the two
   heads, which is a *different* sentence from case 3's; the gate's hard-failure
   rationale is that the two used to be indistinguishable, so the messages being
   separate is the property under test, not an incidental detail.
5. `test_every_refusal_returns_before_the_worker_is_executed` - all three breakages
   are replayed with an `execute` collaborator that fails the test if it is called,
   proving every refusal is printed and returned before any `exec`.

What is real and what is injected: the database collaborator is the gate's own
`_psycopg_connect`, the migration head is the packaged one and the migration is
the real one, so no database input is faked. The only injected *input*
collaborator is object storage (`client_factory`), because these five shapes are
database shapes: a versioned-bucket stub keeps MinIO out of the case instead of
adding the asset errors it drags in (#1051's experience) - the bucket checks
themselves are covered by `tests/unit/test_job_startup_gate.py`. `execute` is
injected too, but only so a real `execvp` cannot replace the test process: the
cases assert on the arguments it receives and on whether it ran at all.

Role provenance: the four non-owner roles are created here with the attributes
the reference provisioning gives them (`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
NOINHERIT`), and every `GRANT` is rendered by
`scripts/dev-env/provision-job-roles.py: statements`, i.e. the SQL that script
would install. No grant statement is re-stated in this module.

Each case creates its own schema and roles and drops them in `finally` - including
when the migration or the grant install fails, not only when the case body fails.
Deleting a check from the gate (the claim-column probe was the one measured) or
turning a refusal into a warning turns the matching case red; merging the two
migration findings into one sentence turns case 4 red.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from minio.commonconfig import ENABLED
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.migrations import PRE_ASR_SETTLEMENT_REVISION
from tests.support.portable_pg import connect as portable_pg_connect

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_startup_gate"
ARTIFACT_BUCKET = "speechnexus-artifacts"
RESULT_BUCKET = "speechnexus-staging"
GATEWAY_LANES = ("asr", "tts")

# The four columns the dispatcher writes when it claims, renews or terminates a
# lease on `coordination_jobs`. Case 2 revokes exactly these.
CLAIM_COLUMNS = ("claim_owner_id", "claim_expires_at_us", "claim_version", "job_state")
# The grant groups the gate tells an operator to install, spelled out here rather
# than read back from the gate so a silently shrunk hint is red.
GRANT_GROUPS = (
    "job_runtime_grants",
    "job_dispatch_grants",
    "public_job_grants",
    "cancellation_reconciliation_grants",
    "coordination_runtime_grants",
)


def _load(name: str, path: Path) -> Any:
    """Import a hyphenated entry point by path, as its own consumers do."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Distinct module names: the unit suite loads the same file as `job_startup_gate`
# and a shared entry would make one import win over the other.
GATE = _load("job_startup_gate_real_db", ROOT / "deploy/job-startup-gate.py")
PROVISION = _load("provision_job_roles_real_db", ROOT / "scripts/dev-env/provision-job-roles.py")


@dataclass
class GateDatabase:
    """One migrated schema plus the four roles the provisioning script installed."""

    url: str = field(repr=False)
    schema: str
    dispatch: str
    roles: tuple[str, ...]
    admin: psycopg.Connection[Any] = field(repr=False)

    def dsn(self) -> str:
        """The dispatch role's own DSN, which is what the gate connects with.

        A `postgresql://` URL, not a key=value conninfo string: the gate parses the
        DSN to name a blank or unusable role before it connects, so the shape the
        deployment hands it is part of what is under test.
        """
        parts = urlsplit(self.url)
        netloc = f"{quote(self.dispatch, safe='')}:{quote(PASSWORD, safe='')}@{parts.hostname}"
        if parts.port is not None:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def revoke_claim_updates(self) -> None:
        self.admin.execute(
            sql.SQL("REVOKE UPDATE ({}) ON {}.coordination_jobs FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(column) for column in CLAIM_COLUMNS),
                sql.Identifier(self.schema),
                sql.Identifier(self.dispatch),
            )
        )

    def revoke_version_select(self) -> None:
        self.admin.execute(
            sql.SQL("REVOKE SELECT ON {}.alembic_version FROM {}").format(
                sql.Identifier(self.schema), sql.Identifier(self.dispatch)
            )
        )

    def grant_version_select(self) -> None:
        self.admin.execute(
            sql.SQL("GRANT SELECT ON {}.alembic_version TO {}").format(
                sql.Identifier(self.schema), sql.Identifier(self.dispatch)
            )
        )

    def stamp(self, revision: str) -> None:
        self.admin.execute(
            sql.SQL("UPDATE {}.alembic_version SET version_num = %s").format(
                sql.Identifier(self.schema)
            ),
            (revision,),
        )

    def version(self) -> str:
        row = self.admin.execute(
            sql.SQL("SELECT version_num FROM {}.alembic_version").format(
                sql.Identifier(self.schema)
            )
        ).fetchone()
        assert row is not None
        return str(row[0])


def _migrate(url: str, schema: str) -> None:
    """Run the real migration into one throwaway schema."""
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def _drop_role(admin: psycopg.Connection[Any], role: str) -> None:
    """Drop one role and its grants; absent roles are not an error during cleanup."""
    present = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if present is None:
        return
    admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _provisioned(
    url: str,
    schema: str,
    roles: tuple[str, ...],
    migrate: Callable[[str, str], None] = _migrate,
) -> Iterator[GateDatabase]:
    """Create the schema, migrate it, install the reference grants, then clean up.

    The admin connection is opened and the migration is run *inside* the same
    `try` as the cleanup: a migration or grant failure must still drop the schema
    and the roles, exactly like a case-body failure. Cleanup tolerates the
    half-provisioned states (`DROP ROLE` of a role that was never created, a
    schema the migration rolled back) so it never masks the original failure.
    """
    writer, admission, dispatch, gateway = roles
    admin = portable_pg_connect(url, autocommit=True)
    try:
        migrate(url, schema)
        for role in roles:
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(PASSWORD))
            )
        # The deployment's own provisioning SQL, rendered by the script that installs
        # it: no grant group is re-stated here, so a group the script drops is red.
        for statement in PROVISION.statements(
            schema, writer, admission, dispatch, gateway, GATEWAY_LANES
        ):
            admin.execute(statement)
        yield GateDatabase(url, schema, dispatch, roles, admin)
    finally:
        for role in roles:
            _drop_role(admin, role)
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def gate_database() -> Iterator[GateDatabase]:
    """A migrated schema and the reference provisioning's four roles, dropped after."""
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_gate_" + suffix
    roles = (
        "sn_gate_writer_" + suffix,
        "sn_gate_admission_" + suffix,
        "sn_gate_dispatch_" + suffix,
        "sn_gate_gateway_" + suffix,
    )
    yield from _provisioned(url, schema, roles)


def test_a_failed_migration_still_drops_its_schema() -> None:
    """A provisioning failure leaves no schema and no role behind.

    The cleanup path is the same one every case uses, so this guards the real
    leak: a migration or grant failure after the schema exists would otherwise
    accumulate one schema and four roles per red run.
    """
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_gate_leak_" + suffix
    roles = (
        "sn_gate_leak_writer_" + suffix,
        "sn_gate_leak_admission_" + suffix,
        "sn_gate_leak_dispatch_" + suffix,
        "sn_gate_leak_gateway_" + suffix,
    )
    created: list[str] = []

    def migrate_then_fail(url: str, schema: str) -> None:
        # Alembic runs `CREATE SCHEMA` and the revisions in one transaction, so an
        # unknown revision alone rolls the schema back and the assertion below
        # would be vacuous. Fail with the schema already committed, which is the
        # shape a failed grant install (or a post-migration step) leaves behind.
        admin = portable_pg_connect(url, autocommit=True)
        try:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        finally:
            admin.close()
        created.append(schema)
        raise RuntimeError("synthetic provisioning failure after the schema existed")

    iterator = _provisioned(url, schema, roles, migrate_then_fail)
    with pytest.raises(RuntimeError, match="synthetic provisioning failure"):
        next(iterator)

    # The failure really happened with the schema in place, so the cleanup below is
    # what removed it - not a no-op against a schema that never existed.
    assert created == [schema]
    with portable_pg_connect(url) as inspector:
        assert inspector.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone() == (None,)
        assert (
            inspector.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(roles),)
            ).fetchall()
            == []
        )


class VersionedBuckets:
    """Stand-in for the object-storage collaborator; these cases are database shapes."""

    def __init__(self) -> None:
        self.probed: list[str] = []

    def bucket_exists(self, name: str) -> bool:
        self.probed.append(name)
        return True

    def get_bucket_versioning(self, name: str) -> Any:
        return SimpleNamespace(status=ENABLED)


@dataclass
class GateRun:
    code: int
    output: str
    executed: list[tuple[str, list[str]]]

    @property
    def failures(self) -> list[str]:
        """The lines an operator would read as the refusal."""
        return [
            line.removeprefix("  FAIL: ")
            for line in self.output.splitlines()
            if line.startswith("  FAIL: ")
        ]


def run_gate(
    database: GateDatabase,
    private_dir: Path,
    *,
    stream: io.StringIO | None = None,
    execute: Any = None,
) -> GateRun:
    """Call the gate's real `main()` against the real dispatch role.

    Only two collaborators are injected: `execute` (a real `execvp` would replace
    the test process) and `client_factory` (the bucket stub above). `connect`,
    `head` and the migration are the packaged ones.
    """
    output = io.StringIO() if stream is None else stream
    executed: list[tuple[str, list[str]]] = []
    for lane in GATEWAY_LANES:
        (private_dir / f"gateway-file-{lane}.json").write_text(
            json.dumps({"objects": {"result_bucket": RESULT_BUCKET}}), encoding="utf-8"
        )
    environment = {
        GATE.RUNTIME_DSN_ENV: database.dsn(),
        GATE.SCHEMA_ENV: database.schema,
        GATE.ARTIFACT_BUCKET_ENV: ARTIFACT_BUCKET,
        GATE.RESULT_BUCKET_ENV: RESULT_BUCKET,
        GATE.PRIVATE_DIR_ENV: str(private_dir),
    }
    buckets = VersionedBuckets()

    def record(command: str, arguments: Sequence[str]) -> Any:
        executed.append((command, list(arguments)))
        if execute is not None:
            return execute(command, arguments)
        return None

    code = GATE.main(
        # The container starts the gate without arguments; passing the list
        # explicitly keeps the in-process call from inheriting pytest's argv.
        argv=[],
        env=environment,
        client_factory=lambda _: (buckets, []),
        execute=record,
        output=output,
    )
    assert buckets.probed, "the gate never reached the bucket collaborator"
    return GateRun(int(code), output.getvalue(), executed)


def test_a_provisioned_role_passes_and_execs_the_worker_last(
    gate_database: GateDatabase, tmp_path: Path
) -> None:
    """The full grant set passes every check, and `exec` happens after the OK line."""
    stream = io.StringIO()
    before_exec: list[str] = []

    def execute(command: str, arguments: Sequence[str]) -> None:
        before_exec.append(stream.getvalue())

    run = run_gate(gate_database, tmp_path, stream=stream, execute=execute)

    # An injected `execute` returns, so the gate's trailing `return 1` is what an
    # in-process success run reports; the real `execvp` never comes back.
    assert run.code == 1
    assert run.executed == [(GATE.WORKER_COMMAND, [GATE.WORKER_COMMAND])]
    assert run.failures == []
    head = GATE.expected_migration_head()
    assert f"  ok: database: migration head {head}" in run.output
    assert "role guard" in run.output
    assert "startup SELECT probes" in run.output
    assert "post-claim write columns" in run.output
    assert f"  ok: object storage: versioned buckets {ARTIFACT_BUCKET}, {RESULT_BUCKET}" in (
        run.output
    )
    assert "job dispatch startup gate: OK" in run.output
    # The two documented `pending` inputs stay pending; they are not failures.
    assert run.output.count("  pending: ") == 2
    # The exec is the last side effect: at the moment it runs, the OK line is
    # already on the stream the container's log captures.
    assert len(before_exec) == 1
    assert "job dispatch startup gate: OK" in before_exec[0]
    assert "FAIL:" not in before_exec[0]


def test_a_missing_claim_column_update_names_the_column_and_the_grant_groups(
    gate_database: GateDatabase, tmp_path: Path
) -> None:
    """Without the four claim-column UPDATEs the gate names each column and the groups."""
    gate_database.revoke_claim_updates()

    run = run_gate(gate_database, tmp_path)

    assert run.code == 1
    # #5: the refusal returns before the worker could be exec'd.
    assert run.executed == []
    # One named line per missing column, and no extra failures to pad the count.
    assert len(run.failures) == len(CLAIM_COLUMNS)
    for column in CLAIM_COLUMNS:
        assert f"  FAIL: dispatch role lacks coordination_jobs.{column} (UPDATE)" in run.output
    # The actionable half: the grant groups the provisioning script installs are
    # named, so an operator is not sent to a generic permission hunt.
    for group in GRANT_GROUPS:
        assert group in run.output
    assert (
        "it would fail after claiming a Job - install " + ", ".join(GRANT_GROUPS) + " for this role"
    ) in run.output
    # Nothing reproduced a driver error into the operator's log.
    assert "permission denied" not in run.output
    assert "InsufficientPrivilege" not in run.output
    assert "Traceback" not in run.output
    # The gate never claims a clean database after a post-claim surface failure.
    assert "  ok: database:" not in run.output
    assert run.output.splitlines()[-1] == (
        f"job dispatch startup gate: {len(CLAIM_COLUMNS)} prerequisite(s) missing; "
        f"refusing to start {GATE.WORKER_COMMAND}"
    )


def test_a_role_without_the_version_grant_is_refused_with_the_one_grant(
    gate_database: GateDatabase, tmp_path: Path
) -> None:
    """A role provisioned before the read-only grant is refused and told the GRANT."""
    gate_database.revoke_version_select()

    run = run_gate(gate_database, tmp_path)

    assert run.code == 1
    assert run.executed == []
    assert len(run.failures) == 1
    message = run.failures[0]
    assert (
        f'GRANT SELECT ON {gate_database.schema}.alembic_version TO "{gate_database.dispatch}"'
        in message
    )
    assert "cannot read it, so the migration head cannot be verified" in message
    assert "upgrade prerequisite" in message
    assert "does not start the worker on an unverified head" in message
    # It is the unreadable-head refusal, not the stale-head one.
    assert "migration differs from the artifact head" not in message


def test_a_stale_head_is_refused_with_a_message_distinct_from_an_unreadable_one(
    gate_database: GateDatabase, tmp_path: Path
) -> None:
    """A stale stamp and a missing read grant produce two different sentences."""
    gate_database.revoke_version_select()
    unreadable = run_gate(gate_database, tmp_path)

    gate_database.grant_version_select()
    stale_revision = PRE_ASR_SETTLEMENT_REVISION
    assert stale_revision != GATE.expected_migration_head()
    gate_database.stamp(stale_revision)
    stale = run_gate(gate_database, tmp_path)

    assert unreadable.code == 1
    assert stale.code == 1
    assert unreadable.executed == []
    assert stale.executed == []
    assert len(unreadable.failures) == 1
    assert len(stale.failures) == 1
    # The docstring's premise is that a missing grant and a genuinely behind
    # database are indistinguishable *except* by this message; if the gate ever
    # merges the two sentences this assertion is what fails.
    assert unreadable.failures[0] != stale.failures[0]
    stale_message = stale.failures[0]
    assert "migration differs from the artifact head" in stale_message
    assert f"is {stale_revision}, the artifact head is {GATE.expected_migration_head()}" in (
        stale_message
    )
    assert "run the migration profile before the job-dispatch profile" in stale_message
    assert "GRANT SELECT" not in stale_message
    # And the unreadable-head sentence is not the stale one either.
    assert "GRANT SELECT" in unreadable.failures[0]
    assert gate_database.version() == stale_revision


BREAKAGES = ("claim-columns", "version-grant", "stale-head")

# Each breakage must produce its own named refusal, not merely a non-zero exit: a
# gate that stopped connecting altogether would also exit 1.
REFUSALS = {
    "claim-columns": "dispatch role lacks coordination_jobs.claim_owner_id (UPDATE)",
    "version-grant": "cannot read it, so the migration head cannot be verified",
    "stale-head": "migration differs from the artifact head",
}


def _break(database: GateDatabase, breakage: str) -> None:
    if breakage == "claim-columns":
        database.revoke_claim_updates()
    elif breakage == "version-grant":
        database.revoke_version_select()
    elif breakage == "stale-head":
        database.stamp(PRE_ASR_SETTLEMENT_REVISION)
    else:  # pragma: no cover - the parametrization above is the whole input set
        raise AssertionError(f"unknown breakage {breakage}")


@pytest.mark.parametrize("breakage", BREAKAGES)
def test_every_refusal_returns_before_the_worker_is_executed(
    gate_database: GateDatabase, tmp_path: Path, breakage: str
) -> None:
    """Every refusal prints its named failure and returns without reaching `exec`."""
    _break(gate_database, breakage)

    def execute(command: str, arguments: Sequence[str]) -> None:
        raise AssertionError(
            f"the gate exec'd {command} after refusing to start; the worker would run "
            "with the broken premise"
        )

    run = run_gate(gate_database, tmp_path, execute=execute)

    assert run.code == 1, run.output
    assert run.executed == []
    assert run.failures, "a refusal must print at least one named FAIL line"
    assert REFUSALS[breakage] in run.output, run.output
    lines = [line for line in run.output.splitlines() if line.strip()]
    # The summary that immediately precedes `return 1` is the last line: nothing
    # after it ran, which is where the `exec` call sits in `main()`.
    assert lines[-1] == (
        f"job dispatch startup gate: {len(run.failures)} prerequisite(s) missing; "
        f"refusing to start {GATE.WORKER_COMMAND}"
    )
