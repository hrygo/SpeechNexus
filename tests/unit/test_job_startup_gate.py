"""The dispatcher startup gate must name the prerequisite it is missing.

``deploy/job-startup-gate.py`` runs as the dispatcher container's entrypoint, so
its failure text is the only explanation an operator gets before the worker
process would have claimed a lease. These tests exercise the naming with fakes
instead of PostgreSQL and MinIO, compare the gate's probe/write surface with the
read-only self-check, the grant declarations and the worker's own write
statements, and call ``main()`` with injected collaborators to prove the four
properties that make the gate a gate: every check runs, a failure exits 1, a
failure never ``exec``s, and only a clean run ``exec``s the worker.

The gate module lives outside the importable source roots; it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GATE_SPEC = importlib.util.spec_from_file_location(
    "job_startup_gate", ROOT / "deploy/job-startup-gate.py"
)
CHECK_SPEC = importlib.util.spec_from_file_location(
    "check_async_job_prereqs", ROOT / "scripts/dev-env/check-async-job-prereqs.py"
)
BACKEND = ROOT / "backend/src/speechnexus_backend"
WORKER_SQL_FILES = (
    BACKEND / "speech/infrastructure/job_dispatch_postgres.py",
    BACKEND / "speech/infrastructure/artifacts_postgres.py",
    BACKEND / "speech/infrastructure/lease_release_sql.py",
)
HEAD = "0053_voice_draft_publication"


def load(spec: Any, name: str) -> Any:
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GATE = load(GATE_SPEC, "job_startup_gate")
CHECK = load(CHECK_SPEC, "check_async_job_prereqs")


class Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class Connection:
    """Answers from a fixed queue; used by the per-check function tests."""

    def __init__(
        self,
        *,
        replies: list[Rows] | None = None,
        failing: set[str] | None = None,
    ) -> None:
        self.replies = list(replies or [])
        self.failing = failing or set()
        self.statements: list[str] = []

    def execute(self, statement: str, parameters: Any = None) -> Rows:
        self.statements.append(statement)
        for marker in self.failing:
            if marker in statement:
                raise RuntimeError(f"permission denied for {marker}")
        if self.replies:
            return self.replies.pop(0)
        return Rows([])

    def close(self) -> None:
        pass


class ScriptedConnection:
    """Answers by statement kind so ``main()`` can run a whole healthy pass."""

    def __init__(
        self,
        version: str | None,
        *,
        read_version: bool = True,
        forbidden: bool = False,
        write_failure: bool = False,
        version_present: bool = True,
        missing_column: bool = False,
    ) -> None:
        self.version = version
        self.read_version = read_version
        self.forbidden = forbidden
        self.write_failure = write_failure
        self.version_present = version_present
        self.missing_column = missing_column
        self.statements: list[str] = []

    def execute(self, statement: str, parameters: Any = None) -> Rows:
        self.statements.append(statement)
        if "has_schema_privilege" in statement:
            return Rows([(self.forbidden,)])
        if "SESSION_USER" in statement:
            return Rows([("sn_job_dispatch", "sn_job_dispatch")])
        if "to_regclass" in statement:
            return Rows([("speech.alembic_version" if self.version_present else None,)])
        if "version_num" in statement:
            if not self.read_version:
                raise RuntimeError('permission denied for relation "alembic_version"')
            return Rows([] if self.version is None else [(self.version,)])
        if "has_column_privilege" in statement:
            if self.missing_column:
                raise RuntimeError('column "result_json" does not exist')
            if self.write_failure:
                return Rows([("speech_jobs.result_json (UPDATE)",)])
            return Rows([])
        return Rows([])

    def close(self) -> None:
        pass


class Versioning:
    def __init__(self, status: Any) -> None:
        self.status = status


class BucketClient:
    def __init__(self, versioning: dict[str, Any], *, unreachable: bool = False) -> None:
        self.versioning = versioning
        self.unreachable = unreachable

    def bucket_exists(self, name: str) -> bool:
        if self.unreachable:
            raise RuntimeError("connection refused")
        return name in self.versioning

    def get_bucket_versioning(self, name: str) -> Versioning:
        return Versioning(self.versioning[name])


def write_gateway_config(path: Path, bucket: str) -> None:
    path.write_text(
        json.dumps({"protocol": "file_asr_v1", "objects": {"result_bucket": bucket}}),
        encoding="utf-8",
    )


def healthy_env(directory: Path) -> dict[str, str]:
    write_gateway_config(directory / "gateway-file-asr.json", "speechnexus-staging")
    write_gateway_config(directory / "gateway-file-tts.json", "speechnexus-staging")
    return {
        GATE.RUNTIME_DSN_ENV: "postgresql://sn_job_dispatch:pw@postgres:5432/speechnexus",
        GATE.ARTIFACT_BUCKET_ENV: "speechnexus-results",
        GATE.RESULT_BUCKET_ENV: "speechnexus-staging",
        GATE.ENDPOINT_ENV: "http://minio:9000",
        GATE.ACCESS_KEY_ENV: "access-key",
        GATE.SECRET_KEY_ENV: "secret-key",
        GATE.KINDS_ENV: "asr,tts",
        GATE.PRIVATE_DIR_ENV: str(directory),
    }


def bucket_client(*, staging: Any = None) -> BucketClient:
    return BucketClient(
        {
            "speechnexus-results": GATE.ENABLED,
            "speechnexus-staging": GATE.ENABLED if staging is None else staging,
        }
    )


def run_main(
    env: dict[str, str],
    *,
    argv: list[str] | None = None,
    connect: Any = None,
    client: Any = None,
    version: str | None = HEAD,
    read_version: bool = True,
) -> tuple[int, list[tuple[str, Any]], str]:
    executed: list[tuple[str, Any]] = []
    output = io.StringIO()
    result = GATE.main(
        argv if argv is not None else ["speechnexus-job-dispatch-worker"],
        env=env,
        connect=connect or (lambda dsn: ScriptedConnection(version, read_version=read_version)),
        client_factory=client or (lambda source: (bucket_client(), [])),
        head=lambda: HEAD,
        execute=lambda target, command: executed.append((target, list(command))),
        output=output,
    )
    return result, executed, output.getvalue()


def test_a_blank_dispatch_dsn_names_the_environment_variable() -> None:
    findings = GATE.dsn_findings({GATE.RUNTIME_DSN_ENV: ""})
    assert len(findings) == 1
    assert "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL is blank" in findings[0]


def test_a_malformed_dispatch_dsn_is_rejected_before_connecting() -> None:
    findings = GATE.dsn_findings({GATE.RUNTIME_DSN_ENV: "not-a-dsn"})
    assert findings and "not a usable postgresql:// DSN" in findings[0]


def test_the_result_bucket_is_probed_even_when_it_matches_the_artifact_bucket() -> None:
    buckets, findings = GATE.referenced_buckets(
        {
            GATE.ARTIFACT_BUCKET_ENV: "speechnexus-results",
            GATE.RESULT_BUCKET_ENV: "speechnexus-results",
        }
    )
    assert findings == []
    assert [bucket.name for bucket in buckets] == ["speechnexus-results"]
    assert buckets[0].env_keys == (
        "SPEECHNEXUS_OBJECT_STORAGE_BUCKET/SPEECHNEXUS_EXECUTION_RESULT_BUCKET"
    )


def test_a_blank_bucket_variable_names_the_key() -> None:
    _, findings = GATE.referenced_buckets(
        {GATE.ARTIFACT_BUCKET_ENV: "speechnexus-results", GATE.RESULT_BUCKET_ENV: " "}
    )
    assert len(findings) == 1
    assert "SPEECHNEXUS_EXECUTION_RESULT_BUCKET is blank" in findings[0]


def test_a_missing_result_bucket_fails_before_the_worker_starts() -> None:
    # This is the "claim first, fail at publication" gap: the gate must name the
    # bucket at startup, not let check_ready's Artifact-only probe pass.
    client = BucketClient({"speechnexus-inputs": GATE.ENABLED})
    buckets = [GATE.BucketCheck("speechnexus-staging", GATE.RESULT_BUCKET_ENV)]
    findings, passed = GATE.bucket_findings(client, buckets)
    assert passed == []
    assert len(findings) == 1
    assert (
        "bucket speechnexus-staging (SPEECHNEXUS_EXECUTION_RESULT_BUCKET) does not exist"
        in (findings[0])
    )
    assert "minio-init" in findings[0]


def test_an_unversioned_result_bucket_names_versioning() -> None:
    client = BucketClient({"speechnexus-staging": "Suspended"})
    findings, _ = GATE.bucket_findings(
        client, [GATE.BucketCheck("speechnexus-staging", GATE.RESULT_BUCKET_ENV)]
    )
    assert findings and "is not versioned" in findings[0]
    assert "SPEECHNEXUS_EXECUTION_RESULT_BUCKET" in findings[0]


def test_unreachable_object_storage_names_the_endpoint_variable() -> None:
    client = BucketClient({}, unreachable=True)
    findings, _ = GATE.bucket_findings(
        client, [GATE.BucketCheck("speechnexus-results", GATE.ARTIFACT_BUCKET_ENV)]
    )
    assert findings and GATE.ENDPOINT_ENV in findings[0]


def test_a_mismatched_gateway_result_bucket_names_the_private_config(tmp_path: Path) -> None:
    write_gateway_config(tmp_path / "gateway-file-asr.json", "speechnexus-other")
    write_gateway_config(tmp_path / "gateway-file-tts.json", "speechnexus-results")
    findings, checked = GATE.gateway_config_findings(
        tmp_path, "speechnexus-results", ("asr", "tts")
    )
    assert checked == ["gateway-file-tts.json"]
    assert len(findings) == 1
    assert "gateway-file-asr.json objects.result_bucket=speechnexus-other" in findings[0]
    assert "SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-results" in findings[0]


def test_a_missing_gateway_config_is_named(tmp_path: Path) -> None:
    findings, checked = GATE.gateway_config_findings(tmp_path, "speechnexus-results", ("asr",))
    assert checked == []
    assert findings and "gateway-file-asr.json is missing" in findings[0]


def test_a_lane_this_bootstrap_does_not_assemble_fails_before_any_claim() -> None:
    # The worker claims any kind inside the dispatchable union, so an unassembled
    # lane would be claimed and fail at submit time - the shape this gate removes.
    findings = GATE.gateway_lane_findings(("asr", "voice_design"))
    assert len(findings) == 1
    assert "voice_design" in findings[0]
    assert "assembles no gateway lane" in findings[0]
    assert "after the claim" in findings[0]


def test_an_unknown_lane_is_rejected_as_not_dispatchable() -> None:
    findings = GATE.gateway_lane_findings(("foo",))
    assert len(findings) == 1
    assert "not a dispatchable Job kind" in findings[0]
    assert "foo" in findings[0]


def test_the_assembled_lanes_are_the_ones_the_config_check_reads(tmp_path: Path) -> None:
    assert GATE.gateway_lane_findings(tuple(GATE.GATEWAY_CONFIGS)) == []
    findings, checked = GATE.gateway_config_findings(tmp_path, "b", ("voice_design",))
    assert findings == [] and checked == []


def test_the_write_surface_matches_the_read_only_self_check() -> None:
    assert GATE.DISPATCH_WRITE_SURFACE == CHECK.DISPATCH_WRITE_PROBES


def test_every_write_column_the_gate_requires_is_declared_by_a_grant_function() -> None:
    from speechnexus_backend.speech.infrastructure.coordination_grants import (
        coordination_runtime_grants,
    )
    from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
        cancellation_reconciliation_grants,
    )
    from speechnexus_backend.speech.infrastructure.job_schema import (
        job_dispatch_grants,
        job_runtime_grants,
    )
    from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants

    statements = [
        *job_runtime_grants("speech", "sn_role_x"),
        *job_dispatch_grants("speech", "sn_role_x"),
        *public_job_grants("speech", "sn_role_x"),
        *cancellation_reconciliation_grants("speech", "sn_role_x"),
        *coordination_runtime_grants("speech", speech_role="sn_role_x", gateway_role="sn_gw_x"),
    ]
    column_grant = re.compile(
        r'^GRANT (SELECT|INSERT|UPDATE) \(([^)]*)\) ON "[^"]+"\."?([A-Za-z0-9_]+)"?'
    )
    table_grant = re.compile(r'^GRANT (SELECT|INSERT|UPDATE) ON "[^"]+"\."?([A-Za-z0-9_]+)"?')
    grantable: set[tuple[str, str, str]] = set()
    table_level: set[tuple[str, str]] = set()
    for statement in statements:
        match = column_grant.match(statement.strip())
        if match is not None:
            privilege, columns, table = match.groups()
            for column in columns.split(","):
                grantable.add((table, privilege, column.strip()))
            continue
        table_match = table_grant.match(statement.strip())
        if table_match is not None:
            privilege, table = table_match.groups()
            table_level.add((table, privilege))
    required = {
        (table, privilege, column)
        for table, privilege, columns in GATE.DISPATCH_WRITE_SURFACE
        for column in columns
    }
    missing = sorted(
        (table, privilege, column)
        for table, privilege, column in required
        if (table, privilege, column) not in grantable and (table, privilege) not in table_level
    )
    assert missing == [], f"write columns no grant group declares: {missing}"


def worker_write_tables() -> set[str]:
    """Tables the dispatch worker writes, read from the worker's own SQL."""
    from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
        NOT_SUBMITTED_TABLE,
    )

    dynamic = {"NOT_SUBMITTED_TABLE": NOT_SUBMITTED_TABLE}
    pattern = re.compile(
        r"(?:INSERT INTO|UPDATE) (?!OF\b)(?:\{([A-Z_]+)\}|([a-z][a-z0-9]*_[a-z0-9_]+))"
    )
    found: set[str] = set()
    for path in WORKER_SQL_FILES:
        for dynamic_name, literal in pattern.findall(path.read_text(encoding="utf-8")):
            found.add(dynamic[dynamic_name] if dynamic_name else literal)
    return found


def test_every_table_the_dispatch_worker_writes_is_in_the_write_surface() -> None:
    # The surface must come from the worker's statements, not from a hand-kept
    # list: a new post-claim write turns this red instead of failing in production.
    surface = {table for table, _, _ in GATE.DISPATCH_WRITE_SURFACE}
    missing = sorted(worker_write_tables() - surface)
    assert missing == [], f"post-claim writes the gate does not verify: {missing}"


def test_the_result_artifact_insert_is_part_of_the_write_surface() -> None:
    from speechnexus_backend.speech.infrastructure.artifact_schema import ARTIFACT_INSERT_COLUMNS

    surface = {
        table: (privilege, columns) for table, privilege, columns in GATE.DISPATCH_WRITE_SURFACE
    }
    assert surface["speech_artifacts"] == ("INSERT", ARTIFACT_INSERT_COLUMNS)


def test_the_release_columns_are_derived_from_the_coordination_grants() -> None:
    from speechnexus_backend.speech.infrastructure.coordination_grants import SPEECH_UPDATES

    surface = {table: columns for table, privilege, columns in GATE.DISPATCH_WRITE_SURFACE}
    for table in GATE.RELEASE_WRITE_TABLES:
        assert surface[table] == tuple(SPEECH_UPDATES[table].split()), table


def test_a_missing_write_column_names_the_column_and_grant_hint() -> None:
    connection = Connection(replies=[Rows([("speech_artifacts.artifact_id (INSERT)",)])])
    findings, checked = GATE.write_privilege_findings(connection, "speech")
    assert checked == sum(len(columns) for _, _, columns in GATE.DISPATCH_WRITE_SURFACE)
    assert len(findings) == 1
    assert "speech_artifacts.artifact_id (INSERT)" in findings[0]
    for group in GATE.DISPATCH_GRANT_GROUPS:
        assert group in findings[0]
    assert "has_column_privilege" in connection.statements[0]


def test_a_rejected_select_probe_names_the_table_and_stops() -> None:
    connection = Connection(failing={"coordination_jobs"})
    findings, passed = GATE.probe_findings(connection, ("asr",))
    assert passed == ["speech_jobs", "speech_job_events"]
    assert len(findings) == 1
    assert "startup probe on coordination_jobs" in findings[0]
    for group in GATE.DISPATCH_GRANT_GROUPS:
        assert group in findings[0]
    # The failed probe aborts the transaction, so the gate must not keep going.
    assert len(connection.statements) == 3


def test_voice_validation_probes_are_added_only_for_that_lane() -> None:
    plain = Connection()
    GATE.probe_findings(plain, ("asr", "tts"))
    voice = Connection()
    GATE.probe_findings(voice, ("asr", "voice_validation"))
    assert len(voice.statements) == len(plain.statements) + len(
        GATE.DISPATCH_VOICE_VALIDATION_PROBES
    )


def test_the_role_guard_names_superuser_and_ddl_roles() -> None:
    # A superuser, schema CREATE or owner membership rejects the role.
    connection = Connection(replies=[Rows([(True,)])])
    findings = GATE.role_guard_findings(connection, "speech", "sn_job_dispatch", "sn_job_dispatch")
    assert findings and "sn_job_dispatch is a superuser" in findings[0]


def test_a_missing_migration_version_table_names_the_profile() -> None:
    # to_regclass needs no grant, so "the migration profile never ran" is always a
    # named failure even for a dispatch role that cannot read the version table.
    connection = Connection(replies=[Rows([(None,)])])
    findings, head = GATE.migration_findings(connection, "speech", HEAD, "sn_job_dispatch")
    assert findings and "speech.alembic_version is missing" in findings[0]
    assert "migration profile" in findings[0]
    assert head is None


def test_a_dispatch_role_without_the_version_grant_fails_and_names_the_grant() -> None:
    # A role provisioned before the read-only grant cannot be told apart from a
    # stale database, so the gate fails closed instead of starting unverified.
    connection = Connection(
        replies=[Rows([("speech.alembic_version",)])], failing={"alembic_version"}
    )
    findings, head = GATE.migration_findings(connection, "speech", HEAD, "sn_job_dispatch")
    assert head is None
    assert len(findings) == 1
    assert 'GRANT SELECT ON speech.alembic_version TO "sn_job_dispatch"' in findings[0]
    assert "upgrade prerequisite" in findings[0]
    assert "does not start the worker on an unverified head" in findings[0]


def test_a_stale_migration_names_both_heads() -> None:
    connection = Connection(
        replies=[
            Rows([("speech.alembic_version",)]),
            Rows([("0049_workspace_commands",)]),
        ]
    )
    findings, head = GATE.migration_findings(connection, "speech", HEAD, "sn_job_dispatch")
    assert len(findings) == 1
    assert "0049_workspace_commands" in findings[0]
    assert HEAD in findings[0]
    assert head is None


def test_the_verified_head_is_reported_only_when_it_matches() -> None:
    connection = Connection(replies=[Rows([("speech.alembic_version",)]), Rows([(HEAD,)])])
    findings, head = GATE.migration_findings(connection, "speech", HEAD, "sn_job_dispatch")
    assert findings == []
    assert head == HEAD


def test_an_empty_or_multiple_version_table_is_rejected() -> None:
    present = Rows([("speech.alembic_version",)])
    empty, _ = GATE.migration_findings(
        Connection(replies=[present, Rows([])]), "speech", "head", "sn_job_dispatch"
    )
    assert empty and "holds 0 versions" in empty[0]
    multiple, _ = GATE.migration_findings(
        Connection(replies=[present, Rows([("a",), ("b",)])]), "speech", "head", "sn_job_dispatch"
    )
    assert multiple and "holds 2 versions" in multiple[0]


def test_the_packaged_migration_head_resolves() -> None:
    head = GATE.expected_migration_head()
    assert head and " " not in head


def recording(calls: list[str], name: str, result: Any) -> Any:
    """A stand-in that records the call and returns the expected answer."""

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        return result

    return wrapped


def test_a_blank_dispatch_dsn_never_tries_to_connect() -> None:
    def connect(dsn: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("the gate must not connect with a blank DSN")

    findings, passed = GATE.database_findings(connect, {GATE.RUNTIME_DSN_ENV: ""}, "head")
    assert findings and "is blank" in findings[0]
    assert passed == []


def test_database_findings_runs_the_head_the_probes_and_the_writes(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(GATE, "role_guard_findings", recording(calls, "guard", []))
    monkeypatch.setattr(GATE, "migration_findings", recording(calls, "head", ([], HEAD)))
    monkeypatch.setattr(GATE, "probe_findings", recording(calls, "probe", ([], [])))
    monkeypatch.setattr(GATE, "write_privilege_findings", recording(calls, "writes", ([], 7)))
    connection = ScriptedConnection(HEAD)
    findings, passed = GATE.database_findings(
        lambda dsn: connection,
        {
            GATE.RUNTIME_DSN_ENV: "postgresql://sn_job_dispatch:pw@postgres:5432/speechnexus",
        },
        HEAD,
    )
    assert findings == []
    # The head is compared before the column probes: a stale database names itself
    # instead of surfacing as a missing column or a crashed probe.
    assert calls == ["guard", "head", "probe", "writes"]
    assert passed and "7 post-claim write columns" in passed[0]
    assert passed[0].startswith(f"migration head {HEAD}")


def test_database_findings_names_a_column_surface_that_cannot_be_read() -> None:
    connection = ScriptedConnection(HEAD, missing_column=True)
    findings, passed = GATE.database_findings(
        lambda dsn: connection,
        {GATE.RUNTIME_DSN_ENV: "postgresql://sn_job_dispatch:pw@postgres:5432/speechnexus"},
        HEAD,
    )
    assert passed == []
    assert len(findings) == 1
    assert "cannot be read" in findings[0]
    assert "re-install the dispatch grants" in findings[0]


def test_main_runs_every_prerequisite_check(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[str] = []

    def spy(name: str, original: Any) -> Any:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return original(*args, **kwargs)

        return wrapped

    for name in (
        "database_findings",
        "referenced_buckets",
        "bucket_findings",
        "gateway_lane_findings",
        "gateway_config_findings",
    ):
        monkeypatch.setattr(GATE, name, spy(name, getattr(GATE, name)))
    result, executed, output = run_main(healthy_env(tmp_path))
    assert result == 1  # the injected execute returns; the real one never does
    assert executed == [("speechnexus-job-dispatch-worker", ["speechnexus-job-dispatch-worker"])]
    assert set(calls) == {
        "database_findings",
        "referenced_buckets",
        "bucket_findings",
        "gateway_lane_findings",
        "gateway_config_findings",
    }
    assert "job dispatch startup gate: OK" in output
    assert "FAIL:" not in output


def test_main_execs_the_worker_with_the_configured_argv(tmp_path: Path) -> None:
    result, executed, output = run_main(healthy_env(tmp_path), argv=["sh", "-c", "echo worker"])
    assert result == 1
    assert executed == [("sh", ["sh", "-c", "echo worker"])]
    assert "job dispatch startup gate: OK" in output


def test_main_returns_one_and_never_execs_when_a_database_check_fails(
    tmp_path: Path,
) -> None:
    result, executed, output = run_main(healthy_env(tmp_path), version="0052_quarantine_releases")
    assert result == 1
    assert executed == []
    assert "  FAIL: migration differs from the artifact head" in output
    assert "refusing to start speechnexus-job-dispatch-worker" in output


def test_main_returns_one_and_never_execs_when_the_version_grant_is_missing(
    tmp_path: Path,
) -> None:
    result, executed, output = run_main(healthy_env(tmp_path), read_version=False)
    assert result == 1
    assert executed == []
    assert "cannot read it" in output
    assert 'GRANT SELECT ON speech.alembic_version TO "sn_job_dispatch"' in output


def test_main_returns_one_and_never_execs_on_a_missing_write_column(tmp_path: Path) -> None:
    connection = ScriptedConnection(HEAD, write_failure=True)
    result, executed, output = run_main(healthy_env(tmp_path), connect=lambda dsn: connection)
    assert result == 1
    assert executed == []
    assert "  FAIL: dispatch role lacks speech_jobs.result_json (UPDATE)" in output


def test_main_returns_one_and_never_execs_on_an_unversioned_bucket(tmp_path: Path) -> None:
    result, executed, output = run_main(
        healthy_env(tmp_path),
        client=lambda source: (bucket_client(staging="Suspended"), []),
    )
    assert result == 1
    assert executed == []
    assert "  FAIL: bucket speechnexus-staging" in output


def test_main_returns_one_and_never_execs_on_a_gateway_bucket_mismatch(
    tmp_path: Path,
) -> None:
    env = healthy_env(tmp_path)
    write_gateway_config(tmp_path / "gateway-file-asr.json", "speechnexus-other")
    result, executed, output = run_main(env)
    assert result == 1
    assert executed == []
    assert "gateway-file-asr.json objects.result_bucket=speechnexus-other" in output


def test_main_returns_one_and_never_execs_on_an_unassembled_lane(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    env[GATE.KINDS_ENV] = "asr,tts,voice_design"
    result, executed, output = run_main(env)
    assert result == 1
    assert executed == []
    assert "voice_design" in output
    assert "assembles no gateway lane" in output


def test_main_names_every_failure_instead_of_stopping_at_the_first(tmp_path: Path) -> None:
    env = healthy_env(tmp_path)
    env[GATE.ARTIFACT_BUCKET_ENV] = ""
    env[GATE.KINDS_ENV] = "asr,tts,foo"
    result, executed, output = run_main(env, version="0052_quarantine_releases")
    assert result == 1
    assert executed == []
    failures = [line for line in output.splitlines() if line.startswith("  FAIL: ")]
    assert len(failures) == 3  # stale head, blank bucket, unknown lane
    assert "0052_quarantine_releases" in failures[0]


def test_main_keeps_the_two_pending_lines_that_are_not_failures(tmp_path: Path) -> None:
    result, _, output = run_main(healthy_env(tmp_path))
    assert result == 1
    pending = [line for line in output.splitlines() if "pending:" in line]
    assert len(pending) == 2
    assert "real engine" in pending[0]
    assert "503 job_unavailable" in pending[1]


def test_main_rejects_a_probe_failure_before_the_write_check(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(GATE, "probe_findings", recording(calls, "probe", (["boom"], [])))
    monkeypatch.setattr(GATE, "write_privilege_findings", recording(calls, "writes", ([], 0)))
    result, executed, output = run_main(healthy_env(tmp_path))
    assert result == 1
    assert executed == []
    assert calls == ["probe"]
    assert "  FAIL: boom" in output
