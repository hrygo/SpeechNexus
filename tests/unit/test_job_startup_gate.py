"""The dispatcher startup gate must name the prerequisite it is missing.

``deploy/job-startup-gate.py`` runs as the dispatcher container's entrypoint, so
its failure text is the only explanation an operator gets before the worker
process would have claimed a lease. These tests exercise the naming with fakes
instead of PostgreSQL and MinIO, and compare the gate's probe/write surface with
the read-only self-check and the grant functions so the three cannot drift.

The gate module lives outside the importable source roots; it is loaded by path.
"""

from __future__ import annotations

import importlib.util
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


def write_gateway_config(path: Path, bucket: str) -> None:
    path.write_text(
        json.dumps({"protocol": "file_asr_v1", "objects": {"result_bucket": bucket}}),
        encoding="utf-8",
    )


def test_a_mismatched_gateway_result_bucket_names_the_private_config(tmp_path: Path) -> None:
    write_gateway_config(tmp_path / "gateway-file-asr.json", "speechnexus-other")
    write_gateway_config(tmp_path / "gateway-file-tts.json", "speechnexus-results")
    findings, checked, pending = GATE.gateway_config_findings(
        tmp_path, "speechnexus-results", ("asr", "tts")
    )
    assert checked == ["gateway-file-tts.json"]
    assert pending == []
    assert len(findings) == 1
    assert "gateway-file-asr.json objects.result_bucket=speechnexus-other" in findings[0]
    assert "SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-results" in findings[0]


def test_a_missing_gateway_config_is_named(tmp_path: Path) -> None:
    findings, checked, _ = GATE.gateway_config_findings(tmp_path, "speechnexus-results", ("asr",))
    assert checked == []
    assert findings and "gateway-file-asr.json is missing" in findings[0]


def test_an_unassembled_gateway_lane_is_pending_not_a_failure(tmp_path: Path) -> None:
    findings, checked, pending = GATE.gateway_config_findings(
        tmp_path, "speechnexus-results", ("voice_design",)
    )
    assert findings == [] and checked == []
    assert pending and "voice_design" in pending[0]


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


def test_a_missing_write_column_names_the_column_and_grant_hint() -> None:
    connection = Connection(replies=[Rows([("speech_jobs.state (UPDATE)",)])])
    findings, checked = GATE.write_privilege_findings(connection, "speech")
    assert checked == sum(len(columns) for _, _, columns in GATE.DISPATCH_WRITE_SURFACE)
    assert len(findings) == 1
    assert "speech_jobs.state (UPDATE)" in findings[0]
    assert "job_dispatch_grants" in findings[0]
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
    findings, head = GATE.migration_findings(connection, "speech", "0052_quarantine_releases")
    assert findings and "speech.alembic_version is missing" in findings[0]
    assert "migration profile" in findings[0]
    assert head is None


def test_a_dispatch_role_without_the_version_grant_reports_pending_not_failure() -> None:
    # Existing deployments provisioned before the read-only grant must still start:
    # the column probes then carry the migration verdict.
    connection = Connection(
        replies=[Rows([("speech.alembic_version",)])], failing={"alembic_version"}
    )
    findings, head = GATE.migration_findings(connection, "speech", "0052_quarantine_releases")
    assert findings == []
    assert head is None


def test_a_stale_migration_names_both_heads() -> None:
    connection = Connection(
        replies=[Rows([("speech.alembic_version",)]), Rows([("0049_workspace_commands",)])]
    )
    findings, head = GATE.migration_findings(connection, "speech", "0052_quarantine_releases")
    assert len(findings) == 1
    assert "0049_workspace_commands" in findings[0]
    assert "0052_quarantine_releases" in findings[0]
    assert head is None


def test_the_verified_head_is_reported_only_when_it_matches() -> None:
    connection = Connection(
        replies=[Rows([("speech.alembic_version",)]), Rows([("0052_quarantine_releases",)])]
    )
    findings, head = GATE.migration_findings(connection, "speech", "0052_quarantine_releases")
    assert findings == []
    assert head == "0052_quarantine_releases"


def test_an_empty_or_multiple_version_table_is_rejected() -> None:
    present = Rows([("speech.alembic_version",)])
    empty, _ = GATE.migration_findings(Connection(replies=[present, Rows([])]), "speech", "head")
    assert empty and "holds 0 versions" in empty[0]
    multiple, _ = GATE.migration_findings(
        Connection(replies=[present, Rows([("a",), ("b",)])]), "speech", "head"
    )
    assert multiple and "holds 2 versions" in multiple[0]


def test_the_packaged_migration_head_resolves() -> None:
    head = GATE.expected_migration_head()
    assert head and " " not in head


def test_a_blank_dispatch_dsn_never_tries_to_connect() -> None:
    def connect(dsn: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("the gate must not connect with a blank DSN")

    findings, passed, pending = GATE.database_findings(connect, {GATE.RUNTIME_DSN_ENV: ""}, "head")
    assert findings and "is blank" in findings[0]
    assert passed == [] and pending == []
