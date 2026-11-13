#!/usr/bin/env python3
"""Run the whole reference-deployment acceptance in one command.

Why this exists: #572 asks for a from-zero executable step list that prints each
step's precondition and expected output. `8404efad` already shipped the startup
gate (`deploy/job-startup-gate.py`) and the role provisioning script
(`scripts/dev-env/provision-job-roles.py`), but an operator still had to retype
several command blocks from `docs/operations/compose-minimal.md`, and nothing
ran them end to end with a named failure. This entry point is that runner: it
executes the steps in order, prints step name / precondition / expected output /
actual conclusion, and stops at the first failure with that step's exit code.

It deliberately derives its inputs instead of restating them:

- required environment keys come from the `${KEY:?}` expansions in
  `deploy/compose.yaml`, plus the few keys the called scripts index directly;
- the migration head is parsed from the revision graph under the `script_location`
  declared in `alembic.ini`, never hardcoded;
- role provisioning calls `provision-job-roles.py`; the `grants: N statements`
  line it prints is the surface that was installed, and an idempotent rerun
  re-renders the same statements with the repository's own `--print-statements`;
- the startup gate is executed as-is (through its compose service, or with the
  same image and environment when the gateway lane cannot start), so the
  verdict semantics stay with the gate and this script only translates its
  ok/pending/FAIL lines.

Order: migration runs before role provisioning. The grants in
`provision-job-roles.py` target relations under `speech`, so provisioning a fresh
database before the migration fails with `relation does not exist`; the runbook
places section 5 (migration) before section 6 (roles) for the same reason.

Exit codes: 0 all steps passed; 10 tools and environment; 20 migration; 30 roles;
40 buckets; 50 startup gate; 60 stack; 70 health checks; 2 argument error.
`--dry-run` only runs static checks and prints the plan: it never creates a
role, a bucket or a database row.

Read-only boundary: this script does not edit `deploy/`,
`docs/operations/compose-minimal.md`, backend or contract files. Containers and
volumes are named per `--project-name`, but `deploy/compose.yaml` pins the stack
network to `172.28.0.0/24`, so two reference stacks cannot run concurrently on
one host; `--project-name` isolates reruns of the same stack, not a parallel one.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / "deploy/compose.env"
DEFAULT_COMPOSE_FILE = REPO_ROOT / "deploy/compose.yaml"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
PROVISION_SCRIPT = REPO_ROOT / "scripts/dev-env/provision-job-roles.py"
GATE_SCRIPT = REPO_ROOT / "deploy/job-startup-gate.py"

MANDATORY_EXPANSION = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")
ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
SCRIPT_LOCATION = re.compile(r"(?m)^script_location\s*=\s*(\S+)\s*$")
SQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
DSN_SCHEMES = frozenset({"postgresql", "postgres"})

# Keys the scripts called by this entry point index directly; compose.yaml gives
# most of them a `:-` default, so they are not in MANDATORY_EXPANSION. A default
# silently accepted here would let the acceptance pass on a bucket name the
# operator never wrote down, so the entry point requires them to be explicit.
EXTRA_REQUIRED = (
    "SN_ADMIN_DSN",
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL",
    "SPEECHNEXUS_OBJECT_STORAGE_BUCKET",
    "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET",
)
JOB_DSN_KEYS = (
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL",
)
BASELINE_DSN_KEYS = (
    "SPEECHNEXUS_API_DATABASE_URL",
    "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL",
    "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL",
)
DEFAULT_SCHEMA = "speech"
DEFAULT_GATEWAY_ROLE = "sn_gateway"
DEFAULT_API_PORT = "8000"
DEFAULT_GATEWAY_PORT = "8010"
PRIVATE_CONTAINER_DIR = "/run/speechnexus-private"
GATE_CONTAINER_PATH = "job-startup-gate.py"
MINIMUM_PYTHON = (3, 12)

# Steps and their exit codes. The code is what an operator (or a wrapper) reads
# when the run stops, so it names exactly one step.
EXIT_TOOLS = 10
EXIT_MIGRATE = 20
EXIT_ROLES = 30
EXIT_BUCKETS = 40
EXIT_GATE = 50
EXIT_STACK = 60
EXIT_HEALTH = 70

# Read-only verification of the buckets minio-init just created and versioned.
# It runs in the same image and with the same environment as `minio-init`, so a
# missing bucket or disabled versioning is reported by an independent read.
BUCKET_VERIFY_PROGRAM = """\
import os

from minio import Minio
from minio.commonconfig import ENABLED

endpoint = os.environ["MINIO_ENDPOINT"]
client = Minio(
    endpoint.removeprefix("http://").removeprefix("https://"),
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=endpoint.startswith("https://"),
)
names = sorted(
    {
        os.environ["SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET"],
        os.environ["SPEECHNEXUS_OBJECT_STORAGE_BUCKET"],
        os.environ.get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET")
        or os.environ["SPEECHNEXUS_OBJECT_STORAGE_BUCKET"],
    }
)
problems = []
for name in names:
    if not client.bucket_exists(name):
        problems.append(f"{name}: missing")
    elif client.get_bucket_versioning(name).status != ENABLED:
        problems.append(f"{name}: versioning disabled")
print("buckets verified: " + ("; ".join(problems) if problems else ", ".join(names)))
raise SystemExit(1 if problems else 0)
"""


class StepFailureError(Exception):
    """One step's precondition or verdict failed; the message names the step."""

    def __init__(self, message: str, *, inputs: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.inputs = tuple(inputs)


@dataclass(frozen=True)
class CommandResult:
    """One executed (or planned) command and its output."""

    argv: tuple[str, ...]
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def output(self) -> str:
        return "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)

    def tail(self, lines: int = 4) -> str:
        joined = self.output().splitlines()
        return "\n".join(joined[-lines:]) if joined else "no output"


class Runner:
    """Execute one command. Tests replace this to avoid Docker and a database."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                env=None if env is None else dict(env),
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(tuple(argv), 127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return CommandResult(tuple(argv), 124, "", f"timed out: {' '.join(argv)}")
        return CommandResult(tuple(argv), completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class ServiceState:
    service: str
    state: str
    health: str


@dataclass
class Context:
    """Everything a step needs, with the write guard for ``--dry-run``."""

    env: Mapping[str, str]
    env_file: Path
    compose_file: Path
    project: str
    runner: Runner
    dry_run: bool
    gate_mode: str
    repo_root: Path = REPO_ROOT
    child_env: Mapping[str, str] = field(default_factory=dict)
    planned: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    @property
    def compose_argv(self) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "-p",
            self.project,
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.compose_file),
        )

    def compose(self, *arguments: str) -> tuple[str, ...]:
        return (*self.compose_argv, *arguments)

    def read(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> CommandResult:
        """Run a read-only command; allowed in ``--dry-run``."""
        return self.runner.run(argv, env=dict(env) if env is not None else self.child_env)

    def mutate(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> CommandResult:
        """Run a command that changes state; refused in ``--dry-run``."""
        if self.dry_run:
            raise RuntimeError(f"dry-run executed a mutating command: {' '.join(argv)}")
        return self.runner.run(argv, env=dict(env) if env is not None else self.child_env)

    def plan(self, argv: Sequence[str]) -> None:
        self.plan_text(" ".join(shlex.quote(part) for part in argv))

    def plan_text(self, text: str) -> None:
        self.planned.append(text)
        print(f"      计划: {text}")

    def require(self, ok: bool, message: str, *, inputs: Sequence[str] = ()) -> None:
        """Fail the step, or defer it as pending input under ``--dry-run``."""
        if ok:
            return
        if self.dry_run:
            self.pending.append(message)
            print(f"      待输入: {message}")
            return
        raise StepFailureError(message, inputs=inputs)

    def schema(self) -> str:
        name = self.env.get("SPEECHNEXUS_DATABASE_SCHEMA", DEFAULT_SCHEMA).strip() or DEFAULT_SCHEMA
        if SQL_IDENTIFIER.fullmatch(name) is None:
            raise StepFailureError(f"SPEECHNEXUS_DATABASE_SCHEMA 不是安全的小写标识符: {name!r}")
        return name


@dataclass(frozen=True)
class Step:
    ident: str
    title: str
    exit_code: int
    precondition: str
    expected: str
    run: Callable[[Context], str]


def load_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines without executing the file."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if match is None:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def effective_env(
    env_file: Path, host_env: Mapping[str, str]
) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """Return (merged view for checks, environment for child commands).

    The env file wins over the shell for keys it defines so a rerun is
    reproducible regardless of what the operator exported earlier; ``SN_ADMIN_DSN``
    is the one input that stays a shell-only secret.
    """
    file_values = load_env_file(env_file)
    merged = {**host_env, **file_values}
    if host_env.get("SN_ADMIN_DSN", "").strip():
        merged["SN_ADMIN_DSN"] = host_env["SN_ADMIN_DSN"].strip()
    return merged, merged


def mandatory_env_keys(compose_text: str) -> tuple[str, ...]:
    """Every key this entry point requires non-empty: compose-derived plus extras."""
    keys = set(compose_env_keys(compose_text)) | set(EXTRA_REQUIRED)
    return tuple(sorted(keys))


def compose_env_keys(compose_text: str) -> tuple[str, ...]:
    """The `${KEY:?}` expansions compose refuses to render without."""
    return tuple(sorted(set(MANDATORY_EXPANSION.findall(compose_text))))


def env_gaps(env: Mapping[str, str], keys: Sequence[str]) -> list[str]:
    return [key for key in keys if not env.get(key, "").strip()]


def parse_dsn(raw: str) -> tuple[str, str, str] | None:
    """Return (user, password, host) or None when the DSN is unusable."""
    text = raw.strip()
    if "://" not in text:
        return None
    scheme, _, remainder = text.partition("://")
    if scheme not in DSN_SCHEMES:
        return None
    authority, _, _path = remainder.partition("/")
    credentials, _, host = authority.rpartition("@")
    if not credentials or not host:
        return None
    user, _, password = credentials.partition(":")
    if not user or "@" in host:
        return None
    return user, password, host


def dsn_identity(env: Mapping[str, str], key: str, *, consumer: str) -> tuple[str, str]:
    raw = env.get(key, "").strip()
    if not raw:
        raise StepFailureError(f"{key} 为空；{consumer} 需要的身份来自这条 DSN", inputs=[key])
    parsed = parse_dsn(raw)
    if parsed is None:
        raise StepFailureError(
            f"{key} 不是可用的 postgresql:// DSN；{consumer} 无法连接", inputs=[key]
        )
    user, password, _host = parsed
    if SQL_IDENTIFIER.fullmatch(user) is None:
        raise StepFailureError(f"{key} 里的用户名 {user!r} 不是安全的角色标识符")
    return user, password


def migration_versions_dir(repo_root: Path) -> Path:
    """Resolve the versions directory from ``alembic.ini``, never a literal path."""
    ini = repo_root / "alembic.ini"
    if not ini.is_file():
        raise StepFailureError(f"{ini} 不存在；无法派生迁移目录", inputs=["在仓库根目录运行本脚本"])
    match = SCRIPT_LOCATION.search(ini.read_text(encoding="utf-8"))
    if match is None:
        raise StepFailureError(f"{ini} 没有 script_location 行；无法派生迁移目录")
    location = match.group(1).replace("%(here)s", str(repo_root))
    versions = Path(location) / "versions"
    if not versions.is_dir():
        raise StepFailureError(f"迁移 versions 目录不存在: {versions}")
    return versions


def migration_graph(versions_dir: Path) -> tuple[dict[str, tuple[str, ...]], set[str]]:
    """Parse every revision script's ``revision``/``down_revision``."""
    parents: dict[str, tuple[str, ...]] = {}
    revisions: set[str] = set()
    for path in sorted(versions_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as error:
            raise StepFailureError(f"迁移脚本无法解析: {path.name}: {error}") from error
        values: dict[str, object] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id not in {"revision", "down_revision"}:
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except ValueError:
                continue
        revision = values.get("revision")
        if not isinstance(revision, str) or not revision:
            continue
        revisions.add(revision)
        down = values.get("down_revision")
        if isinstance(down, str):
            parents[revision] = (down,)
        elif isinstance(down, tuple):
            parents[revision] = tuple(item for item in down if isinstance(item, str))
        else:
            parents[revision] = ()
    if not revisions:
        raise StepFailureError(f"{versions_dir} 下没有可解析的 revision")
    return parents, revisions


def migration_heads(versions_dir: Path) -> tuple[str, dict[str, tuple[str, ...]]]:
    """Return the single head and the parent map, refusing a branched graph."""
    parents, revisions = migration_graph(versions_dir)
    referenced = {parent for items in parents.values() for parent in items}
    heads = sorted(revisions - referenced)
    if len(heads) != 1:
        raise StepFailureError(
            "迁移脚本有 "
            f"{len(heads)} 个 head（{', '.join(heads)}）；单库只允许一个 head，先合并分支"
        )
    return heads[0], parents


def migration_verify(head: str, database_version: str) -> str | None:
    """Return a named failure when the database is not exactly at ``head``."""
    rows = [line.strip() for line in database_version.splitlines() if line.strip()]
    if len(rows) != 1:
        return (
            f"alembic_version 有 {len(rows)} 行（{', '.join(rows) or '空'}）；"
            f"迁移 profile 必须收敛到唯一 head {head}"
        )
    if rows[0] != head:
        return f"迁移落后: alembic_version={rows[0]}，迁移目录派生的 head={head}"
    return None


def psql_argv(ctx: Context, sql: str) -> tuple[str, ...]:
    user = ctx.env.get("POSTGRES_USER", "").strip()
    database = ctx.env.get("POSTGRES_DB", "").strip()
    if not user or not database:
        raise StepFailureError("POSTGRES_USER / POSTGRES_DB 为空；无法用 postgres 服务查询")
    return ctx.compose("exec", "-T", "postgres", "psql", "-U", user, "-d", database, "-Atc", sql)


def psql_query(ctx: Context, sql: str, *, purpose: str) -> str:
    result = ctx.read(psql_argv(ctx, sql))
    if not result.ok:
        raise StepFailureError(
            f"{purpose} 查询失败：{result.tail(3)}",
            inputs=["postgres 服务需 running/healthy，且迁移已应用"],
        )
    return result.stdout.strip()


def psql_rows(ctx: Context, sql: str, *, purpose: str) -> list[str]:
    return [line for line in psql_query(ctx, sql, purpose=purpose).splitlines() if line.strip()]


def roles_in_database(ctx: Context, names: Sequence[str]) -> set[str]:
    quoted = ", ".join(f"'{name}'" for name in names)
    rows = psql_rows(
        ctx,
        f"SELECT rolname FROM pg_roles WHERE rolname IN ({quoted})",
        purpose="角色存在性",
    )
    return set(rows)


@dataclass(frozen=True)
class PrivilegeProbe:
    role: str
    relation: str
    privilege: str
    column: str | None = None

    def label(self, schema: str) -> str:
        target = f"{schema}.{self.relation}" + (f".{self.column}" if self.column else "")
        return f"{self.role} 缺少 {target} 的 {self.privilege}"


def privilege_gaps(ctx: Context, probes: Sequence[PrivilegeProbe]) -> list[str]:
    """Return one label per missing privilege, using catalog functions only."""
    schema = ctx.schema()
    values: list[str] = []
    for probe in probes:
        for part in (probe.role, probe.relation, probe.column or ""):
            if part and SQL_IDENTIFIER.fullmatch(part) is None:
                raise StepFailureError(f"授权探针收到非标识符: {part!r}")
        function = "has_column_privilege" if probe.column else "has_table_privilege"
        target = f"'{schema}.{probe.relation}'" + (f", '{probe.column}'" if probe.column else "")
        label = probe.label(schema).replace("'", "''")
        values.append(
            f"('{label}', pg_catalog.{function}('{probe.role}', {target}, '{probe.privilege}'))"
        )
    sql = (
        "SELECT v.label FROM (VALUES "
        + ", ".join(values)
        + ") AS v(label, ok) WHERE v.ok IS NOT TRUE"
    )
    return psql_rows(ctx, sql, purpose="授权面")


def job_role_probes(ctx: Context) -> tuple[list[PrivilegeProbe], tuple[str, str, str]]:
    writer = dsn_identity(ctx.env, JOB_DSN_KEYS[0], consumer="API Job writer")[0]
    admission = dsn_identity(ctx.env, JOB_DSN_KEYS[1], consumer="原子准入")[0]
    dispatch = dsn_identity(ctx.env, JOB_DSN_KEYS[2], consumer="派发 worker")[0]
    probes = [
        PrivilegeProbe(writer, "speech_jobs", "SELECT"),
        PrivilegeProbe(writer, "coordination_jobs", "UPDATE", "claim_owner_id"),
        PrivilegeProbe(admission, "admission_operations", "INSERT", "operation_id"),
        PrivilegeProbe(admission, "workspace_quotas", "UPDATE", "version"),
        PrivilegeProbe(dispatch, "alembic_version", "SELECT"),
        PrivilegeProbe(dispatch, "speech_jobs", "UPDATE", "state"),
        PrivilegeProbe(dispatch, "coordination_jobs", "UPDATE", "claim_version"),
        PrivilegeProbe(dispatch, "speech_job_events", "INSERT", "job_id"),
    ]
    return probes, (writer, admission, dispatch)


# Privileges a rendered `GRANT` can carry for the two target classes this entry
# point verifies. The provision script renders these four shapes, and nothing
# else: `GRANT USAGE ON SCHEMA`, `GRANT <privileges> ON schema.relation`,
# `GRANT <privilege> (columns) ON schema.relation`, and the same with a quoted
# identifier. A new shape (sequence, function, ALL TABLES) fails loudly instead
# of being skipped, so an unparsed statement can never be reported as installed.
TABLE_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "TRIGGER", "TRUNCATE"}
)
COLUMN_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "REFERENCES"})


@dataclass(frozen=True)
class DeclaredGrant:
    """One catalog check: a declared privilege and the predicate that reads it."""

    label: str
    predicate: str


def unquote_identifier(raw: str, *, context: str) -> str:
    """Strip one layer of SQL identifier quotes and refuse anything unsafe."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    if SQL_IDENTIFIER.fullmatch(text) is None:
        raise StepFailureError(f"{context} 中的标识符不是安全的小写标识符: {raw!r}")
    return text


def identifier_list(raw: str, *, context: str) -> list[str]:
    items = [item for item in raw.split(",") if item.strip()]
    if not items:
        raise StepFailureError(f"{context} 的列列表为空")
    return [unquote_identifier(item, context=context) for item in items]


def parse_grant_statement(statement: str) -> list[DeclaredGrant]:
    """Turn one rendered `GRANT` line into catalog checks.

    Only the shapes `provision-job-roles.py --print-statements` renders are
    understood. Anything else raises, because silently ignoring a statement is
    how the previous version claimed a coverage it never checked.
    """
    if not statement.startswith("GRANT ") or not statement.endswith(";"):
        raise StepFailureError(
            "无法解析的授权语句（只认识 provision-job-roles.py 当前渲染的 GRANT 形态）: "
            + statement[:160]
        )
    body = statement[len("GRANT ") : -1].strip()
    head, separator, rest = body.partition(" ON ")
    if not separator:
        raise StepFailureError(f"授权语句缺少 ON 子句: {statement[:160]}")
    privileges_text = head
    columns: list[str] = []
    if " (" in head:
        privileges_text, _, columns_text = head.partition(" (")
        columns = identifier_list(columns_text.rstrip().removesuffix(")"), context=statement)
    privileges = [item.strip().upper() for item in privileges_text.split(",") if item.strip()]
    if not privileges:
        raise StepFailureError(f"授权语句没有权限名: {statement[:160]}")
    target, separator, role_text = rest.rpartition(" TO ")
    if not separator:
        raise StepFailureError(f"授权语句缺少 TO 子句: {statement[:160]}")
    role = unquote_identifier(role_text, context=statement)
    target = target.strip()
    if target.startswith("SCHEMA "):
        if columns:
            raise StepFailureError(f"schema 授权不接受列列表: {statement[:160]}")
        schema = unquote_identifier(target[len("SCHEMA ") :], context=statement)
        checks: list[DeclaredGrant] = []
        for privilege in privileges:
            if privilege != "USAGE":
                raise StepFailureError(f"不认识的 schema 权限 {privilege}: {statement[:160]}")
            checks.append(
                DeclaredGrant(
                    f"{role} 缺少 SCHEMA {schema} 的 USAGE",
                    f"pg_catalog.has_schema_privilege('{role}', '{schema}', 'USAGE')",
                )
            )
        return checks
    if " " in target:
        raise StepFailureError(
            "无法解析的授权目标（只支持 SCHEMA 与 schema.relation）: " + statement[:160]
        )
    schema_part, separator, relation_part = target.partition(".")
    if not separator:
        raise StepFailureError(f"授权目标不是 schema.relation: {statement[:160]}")
    schema = unquote_identifier(schema_part, context=statement)
    relation = unquote_identifier(relation_part, context=statement)
    checks = []
    if columns:
        if len(privileges) != 1:
            raise StepFailureError(f"列级授权语句带多个权限，无法判断列级作用域: {statement[:160]}")
        privilege = privileges[0]
        if privilege not in COLUMN_PRIVILEGES:
            raise StepFailureError(f"列级不支持的权限 {privilege}: {statement[:160]}")
        for column in columns:
            checks.append(
                DeclaredGrant(
                    f"{role} 缺少 {schema}.{relation}.{column} 的 {privilege}",
                    "pg_catalog.has_column_privilege("
                    f"'{role}', '{schema}.{relation}', '{column}', '{privilege}')",
                )
            )
        return checks
    for privilege in privileges:
        if privilege not in TABLE_PRIVILEGES:
            raise StepFailureError(f"不认识的表权限 {privilege}: {statement[:160]}")
        checks.append(
            DeclaredGrant(
                f"{role} 缺少 {schema}.{relation} 的 {privilege}",
                f"pg_catalog.has_table_privilege('{role}', '{schema}.{relation}', '{privilege}')",
            )
        )
    return checks


def rendered_grant_checks(text: str) -> tuple[int, list[DeclaredGrant]]:
    """Return (statement count, expanded catalog checks) for rendered grants."""
    statements = [line.strip() for line in text.splitlines() if line.strip()]
    if not statements:
        raise StepFailureError("provision-job-roles.py --print-statements 没有输出授权语句")
    checks: list[DeclaredGrant] = []
    for statement in statements:
        checks.extend(parse_grant_statement(statement))
    return len(statements), checks


def declared_grant_gaps(ctx: Context, checks: Sequence[DeclaredGrant]) -> list[str]:
    """Return one label per declared privilege the installed ACL does not grant.

    This is an inclusion check: every privilege the provision script declares
    must be readable from the catalog for that role. Extra grants are not an
    error, so a deployment that also holds baseline grants stays green.
    """
    labels: dict[str, str] = {}
    for check in checks:
        labels.setdefault(check.label, check.predicate)
    rows: list[str] = []
    batch: list[str] = []
    for label, predicate in labels.items():
        escaped = label.replace("'", "''")
        batch.append(f"('{escaped}', {predicate})")
        if len(batch) >= 300:
            rows.extend(acl_gap_batch(ctx, batch))
            batch = []
    if batch:
        rows.extend(acl_gap_batch(ctx, batch))
    return rows


def acl_gap_batch(ctx: Context, rows: Sequence[str]) -> list[str]:
    sql = (
        "SELECT v.label FROM (VALUES "
        + ", ".join(rows)
        + ") AS v(label, ok) WHERE v.ok IS NOT TRUE"
    )
    return psql_rows(ctx, sql, purpose="声明授权面")


def provision_argv(*extra: str) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--all-packages",
        "--locked",
        "python",
        str(PROVISION_SCRIPT),
        *extra,
    )


def provision_environment(ctx: Context) -> dict[str, str]:
    environment = dict(ctx.env)
    environment["SN_ADMIN_DSN"] = ctx.env.get("SN_ADMIN_DSN", "").strip()
    return environment


def compose_config(ctx: Context) -> Mapping[str, object]:
    """Resolve compose.yaml with the env file (read-only, no daemon needed)."""
    result = ctx.read(
        (*ctx.compose_argv, "--profile", "job-dispatch", "config", "--format", "json")
    )
    if not result.ok:
        raise StepFailureError(
            f"docker compose config 失败：{result.tail(3)}",
            inputs=["补齐 deploy/compose.env 中 compose.yaml 的 ${KEY:?} 必需键"],
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise StepFailureError(f"docker compose config 输出不是 JSON: {error}") from error
    return as_mapping(document, "compose config")


def as_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StepFailureError(f"{context} 不是映射")
    return cast(Mapping[str, object], value)


def service_environment(config: Mapping[str, object], service: str) -> Mapping[str, str]:
    services = as_mapping(config.get("services"), "compose config.services")
    block = as_mapping(services.get(service), f"compose config.services.{service}")
    environment: dict[str, str] = {}
    for key, value in as_mapping(block.get("environment") or {}, f"{service}.environment").items():
        if isinstance(value, str):
            environment[key] = value
    return environment


def service_volumes(config: Mapping[str, object], service: str) -> list[Mapping[str, object]]:
    services = as_mapping(config.get("services"), "compose config.services")
    block = as_mapping(services.get(service), f"compose config.services.{service}")
    raw = block.get("volumes") or []
    if not isinstance(raw, list):
        raise StepFailureError(f"{service}.volumes 不是列表")
    return [as_mapping(item, f"{service}.volumes[]") for item in raw]


def container_path_source(config: Mapping[str, object], service: str, target: str) -> Path | None:
    for volume in service_volumes(config, service):
        if str(volume.get("target")) != target:
            continue
        source = volume.get("source")
        if isinstance(source, str):
            return Path(source)
    return None


def compose_services(ctx: Context) -> dict[str, ServiceState]:
    result = ctx.read((*ctx.compose_argv, "ps", "-a", "--format", "json"))
    if not result.ok:
        raise StepFailureError(f"docker compose ps 失败：{result.tail(3)}")
    states: dict[str, ServiceState] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry = as_mapping(document, "compose ps entry")
        service = str(entry.get("Service") or "")
        if not service:
            continue
        states[service] = ServiceState(
            service=service,
            state=str(entry.get("State") or ""),
            health=str(entry.get("Health") or ""),
        )
    return states


def service_healthy(ctx: Context, service: str) -> bool:
    try:
        states = compose_services(ctx)
    except StepFailureError:
        return False
    state = states.get(service)
    return state is not None and state.state == "running" and state.health == "healthy"


def private_config_dir(ctx: Context) -> Path:
    raw = ctx.env.get("SPEECHNEXUS_PRIVATE_CONFIG_DIR", "").strip()
    if not raw:
        raise StepFailureError(
            "SPEECHNEXUS_PRIVATE_CONFIG_DIR 为空；compose 会把私有配置目录挂到容器",
            inputs=["SPEECHNEXUS_PRIVATE_CONFIG_DIR"],
        )
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (ctx.compose_file.parent / candidate).resolve()
    return candidate


def container_private_path(ctx: Context, container_path: str) -> Path:
    """Map ``/run/speechnexus-private/<name>`` back to the host bind source."""
    prefix = PRIVATE_CONTAINER_DIR + "/"
    if not container_path.startswith(prefix):
        raise StepFailureError(f"私有配置路径不在 {PRIVATE_CONTAINER_DIR} 下: {container_path}")
    return private_config_dir(ctx) / container_path.removeprefix(prefix)


def gateway_lane_configs(ctx: Context, config: Mapping[str, object]) -> list[Path]:
    environment = service_environment(config, "gateway")
    paths = [
        value
        for key, value in sorted(environment.items())
        if key.startswith("SPEECHNEXUS_GATEWAY_FILE_") and key.endswith("_CONFIG_FILE")
    ]
    if not paths:
        raise StepFailureError("compose gateway 服务没有配置任何 file lane 配置文件")
    return [container_private_path(ctx, path) for path in paths]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bucket_names(ctx: Context) -> list[str]:
    """The distinct buckets `init-object-storage.py` creates, by the same rules."""
    artifact = ctx.env.get("SPEECHNEXUS_OBJECT_STORAGE_BUCKET", "").strip()
    inputs = ctx.env.get("SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET", "").strip()
    result = ctx.env.get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET", "").strip() or artifact
    names = [name for name in (inputs, artifact, result) if name]
    if not names:
        raise StepFailureError("三个桶名键全为空；init-object-storage.py 没有可创建的桶")
    return sorted(set(names))


def http_json(url: str, *, timeout: float = 5.0) -> tuple[int, str, str]:
    """GET one probe endpoint; return (status, body, error).

    ``http.client.InvalidURL`` (a malformed host port makes it raise from
    ``urlopen``) and any other transport exception become the same error tuple,
    so a mistyped port can never escape as an unhandled traceback.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body, ""
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        return error.code, body, ""
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        return 0, "", f"{type(error).__name__}: {error}"


def step_tools(ctx: Context) -> str:
    docker = ctx.read(("docker", "version", "--format", "{{.Server.Version}}"))
    if not docker.ok:
        raise StepFailureError(
            "docker daemon 不可用（docker version 失败）",
            inputs=["启动 Docker daemon 或用 DOCKER_HOST 指向可用引擎"],
        )
    compose = ctx.read(("docker", "compose", "version"))
    if not compose.ok:
        raise StepFailureError("docker compose v2 不可用", inputs=["安装 Docker Compose v2 插件"])
    version_match = re.search(r"v(\d+)\.(\d+)", compose.output())
    if version_match is None or int(version_match.group(1)) < 2:
        raise StepFailureError(
            f"docker compose 版本不是 v2 或更高：{compose.output().splitlines()[0]}"
        )
    if sys.version_info[:2] < MINIMUM_PYTHON:
        raise StepFailureError(
            f"python3 {sys.version_info.major}.{sys.version_info.minor} 低于脚本要求"
            f" {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}"
        )
    uv = ctx.read(("uv", "--version"))
    if not uv.ok:
        raise StepFailureError(
            "uv 不可用；角色预置要在 workspace 依赖下运行 provision-job-roles.py",
            inputs=["安装 uv"],
        )
    if not ctx.env_file.is_file():
        raise StepFailureError(
            f"环境文件不存在: {ctx.env_file}",
            inputs=["cp deploy/compose.env.example deploy/compose.env 并填写"],
        )
    compose_text = ctx.compose_file.read_text(encoding="utf-8")
    gaps = env_gaps(ctx.env, mandatory_env_keys(compose_text))
    if gaps:
        raise StepFailureError(
            "环境键缺失或为空: " + ", ".join(gaps),
            inputs=[f"{key}=<非空值>" for key in gaps],
        )
    render = ctx.read((*ctx.compose_argv, "config", "--quiet"))
    if not render.ok:
        raise StepFailureError(
            f"docker compose config 渲染失败：{render.tail(3)}",
            inputs=["核对 deploy/compose.env 的插值与相对路径"],
        )
    identities = {dsn_identity(ctx.env, key, consumer="Job 角色")[0] for key in JOB_DSN_KEYS}
    if len(identities) != 3:
        raise StepFailureError(
            "三条 Job DSN 必须指向三个互不相同的角色，实际: " + ", ".join(sorted(identities)),
            inputs=list(JOB_DSN_KEYS),
        )
    return (
        f"docker {docker.stdout.strip()} / compose {version_match.group(0)} / "
        f"python {sys.version_info.major}.{sys.version_info.minor} / uv 可用；"
        f"{len(compose_env_keys(compose_text))} 个 compose 派生键 + "
        f"{len(EXTRA_REQUIRED)} 个入口额外键（共 {len(mandatory_env_keys(compose_text))} 个）"
        f"非空；compose 渲染通过"
    )


def step_migrate(ctx: Context) -> str:
    head, _parents = migration_heads(migration_versions_dir(ctx.repo_root))
    schema = ctx.schema()
    dsn_identity(ctx.env, "SPEECHNEXUS_MIGRATION_DATABASE_URL", consumer="migration profile")
    start = ctx.compose("up", "-d", "--wait", "postgres")
    migrate = ctx.compose("--profile", "migration", "run", "--build", "--rm", "migrate")
    version_sql = f"SELECT version_num FROM {schema}.alembic_version"
    if ctx.dry_run:
        ctx.plan(start)
        ctx.plan(migrate)
        ctx.plan(psql_argv(ctx, version_sql))
        return f"计划：postgres healthy → migrate profile → 核对 alembic_version 单行 == {head}"

    started = ctx.mutate(start, env=ctx.child_env)
    if not started.ok:
        raise StepFailureError(f"postgres 未启动到 healthy：{started.tail(3)}")
    applied = ctx.mutate(migrate)
    if not applied.ok:
        raise StepFailureError(
            f"migration profile 失败：{applied.tail(5)}",
            inputs=["SPEECHNEXUS_MIGRATION_DATABASE_URL 必须是可写的迁移 owner"],
        )
    failure = migration_verify(head, psql_query(ctx, version_sql, purpose="迁移版本"))
    if failure is not None:
        raise StepFailureError(
            failure,
            inputs=["docker compose --profile migration run --rm migrate"],
        )
    return f"alembic_version={head}（单行，等于迁移目录派生的 head）；migrate 退出 0"


def step_roles(ctx: Context) -> str:
    gateway_role = (
        ctx.env.get("SN_GATEWAY_ROLE", DEFAULT_GATEWAY_ROLE).strip() or DEFAULT_GATEWAY_ROLE
    )
    probes, (writer, admission, dispatch) = job_role_probes(ctx)
    if ctx.dry_run:
        if not ctx.read(("uv", "--version")).ok:
            ctx.pending.append("uv 不可用；执行时无法预置角色")
        ctx.plan(provision_argv())
        return (
            f"计划：核对 {gateway_role} 基线角色 → 需要时执行 provision-job-roles.py → "
            "渲染授权语句并与已装 ACL 逐条比对 → 校验授权探针"
        )

    existing_baseline = roles_in_database(ctx, (gateway_role,))
    if gateway_role not in existing_baseline:
        raise StepFailureError(
            f"网关基线角色 {gateway_role} 不存在；Job 授权引用它，provision-job-roles.py 会先停下",
            inputs=["按 docs/operations/compose-minimal.md §6 预置基线角色后重跑"],
        )
    existing = roles_in_database(ctx, (writer, admission, dispatch))
    environment = provision_environment(ctx)
    reported_statements: int | None = None
    if not existing:
        provisioned = ctx.mutate(provision_argv(), env=environment)
        if not provisioned.ok:
            raise StepFailureError(
                f"provision-job-roles.py 失败：{provisioned.tail(5)}",
                inputs=["SN_ADMIN_DSN 必须是迁移 owner；schema 必须已迁移"],
            )
        match = re.search(r"grants: (\d+) statements", provisioned.output())
        if match is None:
            raise StepFailureError(f"角色预置输出无法解析：{provisioned.tail(3)}")
        reported_statements = int(match.group(1))
    elif existing != {writer, admission, dispatch}:
        partial = sorted({writer, admission, dispatch} - existing)
        raise StepFailureError(
            "Job 角色部分存在（已存在: "
            + ", ".join(sorted(existing))
            + "；缺失: "
            + ", ".join(partial)
            + "）；先核对归属再重跑，脚本拒绝覆盖",
            inputs=["人工核对 pg_roles 与 schema ACL"],
        )
    rendered = ctx.read(provision_argv("--print-statements"), env=environment)
    if not rendered.ok:
        raise StepFailureError(
            f"provision-job-roles.py --print-statements 失败：{rendered.tail(3)}",
            inputs=list(JOB_DSN_KEYS),
        )
    statements, checks = rendered_grant_checks(rendered.stdout)
    if reported_statements is not None and reported_statements != statements:
        raise StepFailureError(
            f"provision 报告安装 {reported_statements} 条 GRANT，重渲染得到 {statements} 条；"
            "授权面有两个来源，先核对 provision-job-roles.py 与本次环境"
        )
    gaps = declared_grant_gaps(ctx, checks)
    if gaps:
        shown = "; ".join(gaps[:8])
        more = f"（另有 {len(gaps) - 8} 项）" if len(gaps) > 8 else ""
        raise StepFailureError(
            f"已装 ACL 与声明的 {statements} 条 GRANT 不一致：{len(gaps)} 项缺失 — {shown}{more}",
            inputs=[
                "按 docs/operations/compose-minimal.md §6 重新安装点名的 grant 组；不要只补探针"
            ],
        )
    probe_gaps = privilege_gaps(ctx, probes)
    if probe_gaps:
        raise StepFailureError("授权探针失败: " + "; ".join(probe_gaps), inputs=list(JOB_DSN_KEYS))
    if reported_statements is not None:
        return (
            f"{writer} {admission} {dispatch} 已创建；grants: {reported_statements} statements；"
            f"{len(checks)} 项声明授权与已装 ACL 逐条核对全部在位；"
            f"{len(probes)} 个授权探针通过"
        )
    return (
        f"三个角色已存在（幂等：未重复 CREATE ROLE，未新增授权）；"
        f"重渲染 {statements} 条 GRANT、{len(checks)} 项授权与已装 ACL 逐条核对全部在位；"
        f"{len(probes)} 个授权探针通过"
    )


def step_buckets(ctx: Context) -> str:
    names = bucket_names(ctx)
    start = ctx.compose("up", "-d", "--wait", "minio")
    initialize = ctx.compose("run", "--rm", "-T", "minio-init")
    verify = ctx.compose(
        "run",
        "--rm",
        "-T",
        "--no-deps",
        "--entrypoint",
        "python",
        "minio-init",
        "-c",
        BUCKET_VERIFY_PROGRAM,
    )
    if ctx.dry_run:
        ctx.plan(start)
        ctx.plan(initialize)
        ctx.plan_text(f"在 minio 镜像内只读核对 {', '.join(names)} 存在且已版本化")
        return "计划：minio healthy → minio-init 建桶并开版本化 → 独立只读核对"

    started = ctx.mutate(start, env=ctx.child_env)
    if not started.ok:
        raise StepFailureError(f"minio 未启动到 healthy：{started.tail(3)}")
    initialized = ctx.mutate(initialize)
    if not initialized.ok:
        raise StepFailureError(
            f"minio-init 失败：{initialized.tail(5)}",
            inputs=["MINIO_ROOT_USER / MINIO_ROOT_PASSWORD 与实际 MinIO 一致"],
        )
    checked = ctx.mutate(verify)
    if not checked.ok:
        raise StepFailureError(
            f"桶核对失败：{checked.tail(3)}",
            inputs=["minio-init 必须在每个桶上启用版本化"],
        )
    verified = [
        line for line in checked.output().splitlines() if line.startswith("buckets verified:")
    ]
    ready = [line for line in initialized.output().splitlines() if line.startswith("bucket ready:")]
    if len(ready) != len(names) or not verified:
        raise StepFailureError(
            f"桶数量不符：创建 {len(ready)} 个（预期 {len(names)}），核对输出: {checked.tail(2)}"
        )
    return f"{', '.join(names)} 存在且已版本化；minio-init 输出 {len(ready)} 行 bucket ready"


def gate_compose_argv(ctx: Context) -> tuple[str, ...]:
    # --build: `compose run` reuses an already built image otherwise, so a
    # checkout that moved the migration head could be judged by an old image.
    return ctx.compose(
        "--profile", "job-dispatch", "run", "--build", "--rm", "-T", "job-dispatch-worker", "true"
    )


def gate_direct_argv(
    ctx: Context, config: Mapping[str, object]
) -> tuple[tuple[str, ...], Mapping[str, str]]:
    """Run the gate with the worker's own environment, without its netns dependency."""
    environment = dict(service_environment(config, "job-dispatch-worker"))
    gate_path = container_path_source(
        config, "job-dispatch-worker", f"/opt/speechnexus/{GATE_CONTAINER_PATH}"
    )
    private_path = container_path_source(config, "job-dispatch-worker", PRIVATE_CONTAINER_DIR)
    if gate_path is None or private_path is None:
        raise StepFailureError("compose 没有把 job-startup-gate.py 或私有配置目录挂进 worker")
    argv: list[str] = [
        *ctx.compose_argv,
        "run",
        "--build",
        "--rm",
        "-T",
        "--no-deps",
        "--entrypoint",
        "python",
        "-v",
        f"{gate_path}:/opt/speechnexus/{GATE_CONTAINER_PATH}:ro",
        "-v",
        f"{private_path}:{PRIVATE_CONTAINER_DIR}:ro",
    ]
    for key in sorted(environment):
        argv.extend(("-e", key))
    argv.extend(("minio-init", f"/opt/speechnexus/{GATE_CONTAINER_PATH}", "true"))
    child = {**ctx.child_env, **environment}
    return tuple(argv), child


def gate_verdict(result: CommandResult) -> tuple[list[str], list[str]]:
    """Return (ok lines, pending lines) or raise when the gate did not pass.

    FAIL is decided first: a gate output that carries both a FAIL line and an OK
    line must never be read as a pass.
    """
    output = result.output()
    failures = [
        line.strip().removeprefix("FAIL: ") for line in output.splitlines() if "FAIL: " in line
    ]
    if failures:
        raise StepFailureError(
            "启动闸门判定失败: " + "; ".join(failures),
            inputs=["补齐闸门点名的授权/迁移/桶或私有配置后重跑"],
        )
    if "job dispatch startup gate: OK" in output and result.ok:
        ok_lines = [
            line.strip().removeprefix("ok: ") for line in output.splitlines() if "ok: " in line
        ]
        pending = [
            line.strip().removeprefix("pending: ")
            for line in output.splitlines()
            if "pending: " in line
        ]
        return ok_lines, pending
    if "cannot share network namespace" in output:
        raise StepFailureError(
            "启动闸门容器无法启动：job-dispatch-worker 使用 network_mode: service:gateway，"
            "gateway 必须先运行；" + result.tail(2),
            inputs=[
                "gate 在 stack 之后运行；栈步已要求 gateway running/healthy。"
                "若单独执行本步失败，先跑 stack 步或改用 --gate-mode direct"
            ],
        )
    raise StepFailureError(f"启动闸门没有给出判定（退出码 {result.code}）：{result.tail(3)}")


def step_gate(ctx: Context) -> str:
    config = compose_config(ctx)
    if ctx.gate_mode == "compose":
        argv = gate_compose_argv(ctx)
        environment = ctx.child_env
        ctx.require(
            service_healthy(ctx, "gateway"),
            "gateway 未 healthy；job-dispatch-worker 的 network_mode: service:gateway 需要它先运行",
            inputs=[
                "gate 在 stack 之后运行：栈步已要求 api/gateway running/healthy，"
                "先看 stack 步的失败原因（私有配置/模型/容器日志）"
            ],
        )
    else:
        argv, environment = gate_direct_argv(ctx, config)
    if ctx.dry_run:
        if ctx.gate_mode == "compose":
            ctx.plan(argv)
        else:
            ctx.plan_text(
                "docker compose run --no-deps --entrypoint python minio-init "
                f"/opt/speechnexus/{GATE_CONTAINER_PATH} true（沿用 worker 的同源 env）"
            )
        return f"计划：以 {ctx.gate_mode} 方式运行 deploy/job-startup-gate.py，读取 ok/FAIL 判定"
    result = ctx.mutate(argv, env=environment)
    ok_lines, pending = gate_verdict(result)
    detail = "; ".join(ok_lines) if ok_lines else "no ok line"
    return f"闸门 OK（{len(ok_lines)} 项）: {detail}；pending 外部输入 {len(pending)} 项"


def stack_private_inputs(ctx: Context, config: Mapping[str, object]) -> list[str]:
    """Check the private files gateway and api read at startup (static)."""
    problems: list[str] = []
    directory = private_config_dir(ctx)
    if not directory.is_dir():
        problems.append(f"私有配置目录不存在: {directory}")
        return problems
    for path in gateway_lane_configs(ctx, config):
        if not path.is_file():
            problems.append(f"缺少 gateway lane 配置: {path}")
            continue
        try:
            document = as_mapping(json.loads(path.read_text(encoding="utf-8")), path.name)
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{path.name} 不是可读 JSON: {error}")
            continue
        objects = document.get("objects")
        if not isinstance(objects, Mapping):
            problems.append(f"{path.name} 缺少 objects 段")
            continue
        result_bucket = ctx.env.get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET", "").strip()
        if result_bucket and str(objects.get("result_bucket")) != result_bucket:
            problems.append(
                f"{path.name} objects.result_bucket={objects.get('result_bucket')} 与 "
                f"SPEECHNEXUS_EXECUTION_RESULT_BUCKET={result_bucket} 不一致"
            )
        model = document.get("vad_model_file")
        digest = document.get("vad_model_sha256")
        if isinstance(model, str) and isinstance(digest, str):
            model_path = container_private_path(ctx, model)
            if not model_path.is_file():
                problems.append(f"{path.name} 的 vad_model_file 不存在: {model_path}")
            elif sha256_file(model_path) != digest:
                problems.append(
                    f"{path.name} 的 vad_model_sha256 与 {model_path.name} 实际摘要不符"
                )
    recipe = service_environment(config, "api").get("SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE")
    if recipe:
        recipe_path = container_private_path(ctx, recipe)
        if not recipe_path.is_file():
            problems.append(f"缺少转写 recipe: {recipe_path}")
    return problems


def surface_command_output(result: CommandResult, *, label: str, lines: int = 6) -> None:
    """Print the tail of a mutating command so its output is not invisible."""
    text = result.output()
    if not text:
        return
    tail = text.splitlines()[-lines:]
    print(f"      {label}（退出码 {result.code}，最后 {len(tail)} 行）:")
    for line in tail:
        print(f"        {line}")


def step_stack(ctx: Context) -> str:
    config = compose_config(ctx)
    problems = stack_private_inputs(ctx, config)
    if ctx.dry_run:
        for problem in problems:
            ctx.pending.append(problem)
            print(f"      待输入: {problem}")
        ctx.plan(ctx.compose("up", "-d", "--build", "--wait", "api", "gateway"))
        return "计划：api + gateway 启动到 healthy"
    if problems:
        raise StepFailureError(
            "; ".join(problems),
            inputs=["按 docs/operations/compose-minimal.md §3 准备并核对私有配置"],
        )
    baseline = {
        dsn_identity(ctx.env, key, consumer="api/gateway 基线角色")[0] for key in BASELINE_DSN_KEYS
    }
    present = roles_in_database(ctx, sorted(baseline))
    if present != baseline:
        raise StepFailureError(
            "基线运行角色缺失: " + ", ".join(sorted(baseline - present)),
            inputs=["按 docs/operations/compose-minimal.md §6 预置基线角色"],
        )
    started = ctx.mutate(ctx.compose("up", "-d", "--build", "--wait", "api", "gateway"))
    if not started.ok:
        raise StepFailureError(
            f"栈未起来（docker compose up 退出码 {started.code}）：{started.tail(6)}",
            inputs=[
                "看上面构建输出修复 Dockerfile/构建参数；"
                "旧容器仍可能是 running/healthy，ps 的状态不能替代 up 的退出码",
            ],
        )
    surface_command_output(started, label="构建/启动输出")
    states = compose_services(ctx)

    def describe(service: str) -> str:
        state = states.get(service)
        return f"{service}=absent/-" if state is None else f"{service}={state.state}/{state.health}"

    unhealthy = [
        service
        for service in ("api", "gateway")
        if (state := states.get(service)) is None
        or state.state != "running"
        or state.health != "healthy"
    ]
    if unhealthy:
        raise StepFailureError(
            "栈未 healthy: "
            + ", ".join(describe(service) for service in unhealthy)
            + f"；compose 退出码 {started.code}：{started.tail(4)}",
            inputs=["核对 api/gateway 容器日志与 §9 失败对照表"],
        )
    return "api 与 gateway 均 running/healthy（compose up 退出 0）"


def host_port(ctx: Context, key: str, default: str) -> str:
    """Read a published host port and refuse anything a URL cannot carry."""
    raw = ctx.env.get(key, "").strip() or default
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise StepFailureError(
            f"{key}={raw!r} 不是 1..65535 的端口；健康检查无法构造 URL",
            inputs=[f"{key}=<host port>"],
        )
    return raw


def step_health(ctx: Context) -> str:
    api_port = host_port(ctx, "SPEECHNEXUS_API_HOST_PORT", DEFAULT_API_PORT)
    gateway_port = host_port(ctx, "SPEECHNEXUS_GATEWAY_HOST_PORT", DEFAULT_GATEWAY_PORT)
    probes = (
        (f"http://127.0.0.1:{api_port}/healthz", "ok"),
        (f"http://127.0.0.1:{api_port}/readyz", "ready"),
        (f"http://127.0.0.1:{gateway_port}/healthz", "ok"),
        (f"http://127.0.0.1:{gateway_port}/readyz", "ready"),
    )
    if ctx.dry_run:
        for url, expected in probes:
            print(f"      计划: GET {url} → 200 且 status={expected}")
        return f"计划：{len(probes)} 个探针 200"
    passed: list[str] = []
    for url, expected in probes:
        status, body, error = http_json(url)
        if error:
            raise StepFailureError(
                f"{url} 不可达: {error}",
                inputs=["确认对应容器 healthy 且宿主机端口未被占用"],
            )
        if status != 200:
            raise StepFailureError(
                f"{url} 返回 {status}：{body[:200]}", inputs=["查看对应容器日志"]
            )
        try:
            document = as_mapping(json.loads(body), url)
        except json.JSONDecodeError as error:
            raise StepFailureError(f"{url} 返回的不是 JSON: {error}") from None
        if str(document.get("status")) != expected:
            raise StepFailureError(
                f"{url} status={document.get('status')}，预期 {expected}：{body[:200]}"
            )
        passed.append(url.rsplit("/", 1)[-1])
    return f"{len(passed)}/{len(probes)} 探针 200（api/gateway 的 healthz+readyz）"


STEPS: tuple[Step, ...] = (
    Step(
        "tools",
        "工具、版本与必需环境键",
        EXIT_TOOLS,
        "docker daemon + compose v2 + python3 ≥ 3.12 + uv；"
        "deploy/compose.yaml 的 ${KEY:?} 键与三条 Job DSN 均非空；compose 能渲染",
        "各工具版本行；无空白必需键；docker compose config --quiet 退出 0",
        step_tools,
    ),
    Step(
        "migrate",
        "迁移到产物 head",
        EXIT_MIGRATE,
        "alembic.ini 的 script_location 可解析；迁移目录恰好一个 head；"
        "SPEECHNEXUS_MIGRATION_DATABASE_URL 可用",
        "postgres healthy；migration profile 退出 0；speech.alembic_version 单行且等于派生 head",
        step_migrate,
    ),
    Step(
        "roles",
        "预置三个非 owner Job 角色（幂等）",
        EXIT_ROLES,
        "§6 基线角色（含 SN_GATEWAY_ROLE）已存在；"
        "三条 Job DSN 身份互不相同；SN_ADMIN_DSN 是迁移 owner",
        "job roles ready: ... grants: N statements（或已存在时报告幂等）；授权探针全部通过",
        step_roles,
    ),
    Step(
        "buckets",
        "初始化并核对私有桶",
        EXIT_BUCKETS,
        "MINIO_* 与三个桶名键非空；minio 服务可启动",
        "minio-init 对每个不同桶名输出 bucket ready: ... (versioning enabled)；独立核对退出 0",
        step_buckets,
    ),
    Step(
        "stack",
        "API 与 gateway 起来",
        EXIT_STACK,
        "私有配置与批准模型齐备；api/gateway 的基线角色已预置",
        "docker compose up -d --build --wait api gateway 退出 0；两个服务 running/healthy",
        step_stack,
    ),
    Step(
        "gate",
        "启动闸门在真实栈上的判定",
        EXIT_GATE,
        "api 与 gateway 已 running/healthy（gate 在 stack 之后）；"
        "job-startup-gate.py 与私有配置可在 worker 内读到",
        "job dispatch startup gate: OK；ok 行列出数据库/对象存储/私有配置结论，"
        "pending 行是真引擎等外部输入",
        step_gate,
    ),
    Step(
        "health",
        "健康检查",
        EXIT_HEALTH,
        "api 与 gateway 容器 healthy，宿主机端口已发布",
        "api/gateway 的 /healthz 返回 200 status=ok，/readyz 返回 200 status=ready",
        step_health,
    ),
)


def git_commit(ctx: Context) -> str:
    """Read the checkout commit for the acceptance record; never fail the run."""
    result = ctx.read(("git", "-C", str(ctx.repo_root), "rev-parse", "--short", "HEAD"))
    if not result.ok:
        return "unknown"
    return result.stdout.strip() or "unknown"


def environment_record(ctx: Context) -> str:
    """The environment part of the acceptance record #572 asks for."""
    schema = ctx.env.get("SPEECHNEXUS_DATABASE_SCHEMA", DEFAULT_SCHEMA).strip() or DEFAULT_SCHEMA
    mode = "dry-run" if ctx.dry_run else "apply"
    return (
        f"环境记录: repo={ctx.repo_root} commit={git_commit(ctx)} "
        f"project={ctx.project} mode={mode} gate={ctx.gate_mode} schema={schema}"
    )


def images_record(ctx: Context) -> str:
    """Best-effort image record: which service image ran this acceptance."""
    result = ctx.read((*ctx.compose_argv, "images", "--format", "json"))
    if not result.ok:
        return "镜像记录: 不可用（docker compose images 失败）"
    images: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(document, Mapping):
            continue
        service = str(document.get("Service") or "")
        if not service:
            continue
        name = str(document.get("Repository") or "")
        tag = str(document.get("Tag") or "")
        image_id = str(document.get("ID") or "")[:12]
        images.append(f"{service}={name}:{tag}@{image_id}")
    if not images:
        return "镜像记录: 无（compose 项目还没有构建服务镜像）"
    return "镜像记录: " + ", ".join(sorted(images))


def print_records(records: Sequence[tuple[str, str]], ctx: Context, steps: Sequence[Step]) -> None:
    """Print per-step pass/fail/not-run plus the environment and image record."""
    done = " ".join(f"{ident}={status}" for ident, status in records)
    skipped = [step.ident for step in steps if step.ident not in {r[0] for r in records}]
    extras = f" 未运行: {', '.join(skipped)}" if skipped else ""
    print(f"步骤记录: {done}{extras}")
    print(environment_record(ctx))
    print(images_record(ctx))


def run_steps(steps: Sequence[Step], ctx: Context) -> int:
    total = len(steps)
    records: list[tuple[str, str]] = []
    for index, step in enumerate(steps, start=1):
        print(f"[{index}/{total}] {step.ident} — {step.title}")
        print(f"      前置检查: {step.precondition}")
        print(f"      预期输出: {step.expected}")
        try:
            conclusion = step.run(ctx)
        except StepFailureError as failure:
            records.append((step.ident, "FAIL"))
            print(f"      结论: FAIL — {failure.message}")
            for needed in failure.inputs:
                print(f"      需要输入: {needed}")
            remaining = ", ".join(item.ident for item in steps[index:])
            if remaining:
                print(f"      未执行: {remaining}")
            print_records(records, ctx, steps)
            print(f"reference acceptance: FAIL step={step.ident} exit={step.exit_code}")
            return step.exit_code
        except Exception as unexpected:  # one summary line, never a traceback
            records.append((step.ident, "FAIL"))
            print(f"      结论: FAIL — 未预期异常 {type(unexpected).__name__}: {unexpected}")
            print(
                "      需要输入: 核对本步的环境键取值、端口与文件路径；"
                "该异常不是步骤自身的判定，修复后重跑同一命令"
            )
            remaining = ", ".join(item.ident for item in steps[index:])
            if remaining:
                print(f"      未执行: {remaining}")
            print_records(records, ctx, steps)
            print(f"reference acceptance: FAIL step={step.ident} exit={step.exit_code}")
            return step.exit_code
        status = "PLAN" if ctx.dry_run else "PASS"
        records.append((step.ident, status))
        print(f"      结论: {status} — {conclusion}")
    print_records(records, ctx, steps)
    if ctx.dry_run:
        suffix = f" pending={len(ctx.pending)}" if ctx.pending else ""
        print(f"reference acceptance: DRY-RUN PASS steps={total}{suffix}")
        return 0
    print(f"reference acceptance: PASS steps={total}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    step_table = "\n".join(
        f"  {index}. {step.ident:<8} exit={step.exit_code:<3} {step.title}"
        for index, step in enumerate(STEPS, start=1)
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "步骤与失败退出码\n"
            f"{step_table}\n\n"
            "顺序: 迁移在角色预置之前，因为 provision-job-roles.py 的 GRANT 需要迁移后的关系\n"
            "（compose-minimal.md §5 在 §6 之前）。栈在闸门之前：job-dispatch-worker 使用\n"
            "network_mode: service:gateway，gateway 未 healthy 时 compose run 无法启动。\n\n"
            "示例\n"
            "  set -a; . deploy/compose.env; set +a\n"
            '  export SN_ADMIN_DSN="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:'
            '$SPEECHNEXUS_POSTGRES_HOST_PORT/$POSTGRES_DB"\n'
            "  python3 scripts/dev-env/reference-acceptance.py --dry-run\n"
            "  python3 scripts/dev-env/reference-acceptance.py\n"
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Compose 环境文件（默认 deploy/compose.env）",
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=DEFAULT_COMPOSE_FILE,
        help="Compose 文件（默认 deploy/compose.yaml）",
    )
    parser.add_argument(
        "--project-name",
        default="speechnexus",
        help="Compose 项目名，用于隔离容器与网络（默认 speechnexus）",
    )
    parser.add_argument(
        "--gate-mode",
        choices=("compose", "direct"),
        default="compose",
        help="compose: 按部署方式经 job-dispatch-worker 服务运行闸门；"
        "direct: 同镜像同环境直连运行闸门（gateway 未就绪时仍可判定闸门自身检查）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只做静态检查与打印计划，不建角色、不建桶、不改库",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    env_file: Path = arguments.env_file
    compose_file: Path = arguments.compose_file
    if not compose_file.is_file():
        print(f"compose 文件不存在: {compose_file}", file=sys.stderr)
        return 2
    if not env_file.is_file():
        print(f"环境文件不存在: {env_file}（先 cp deploy/compose.env.example）", file=sys.stderr)
        return 2
    merged, child = effective_env(env_file, dict(os.environ))
    context = Context(
        env=merged,
        env_file=env_file,
        compose_file=compose_file,
        project=arguments.project_name,
        runner=Runner(),
        dry_run=bool(arguments.dry_run),
        gate_mode=str(arguments.gate_mode),
        child_env=child,
    )
    print("参考部署一条命令验收（#572）")
    print(
        f"repo={context.repo_root} env={env_file} compose={compose_file} project={context.project}"
    )
    print(
        f"steps={len(STEPS)} order={', '.join(step.ident for step in STEPS)}"
        f" mode={'dry-run' if context.dry_run else 'apply'} gate={context.gate_mode}"
    )
    if context.dry_run:
        print("dry-run: 只做静态检查与打印计划，不执行任何写操作")
    return run_steps(STEPS, context)


if __name__ == "__main__":
    raise SystemExit(main())
