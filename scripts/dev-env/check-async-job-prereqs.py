#!/usr/bin/env python3
"""只读检查参考部署能否跑通一次异步 Job 的 6 项前置.

为什么需要它:启用 `job-dispatch` profile 后,`up -d`/`--wait` 仍可能"成功",而 Job
卡在 `pending`/`running`。缺的往往不是进程,而是 6 项部署前置中的一项:API 的 Job
writer/admission DSN、迁移、派发角色的五组授权、已注册的 ready 引擎实例、gateway
的 `result_bucket`,以及 `network_mode: service:gateway` 下的启动顺序。

本脚本只做判断,不做修复:不改文件、不写数据库、不执行迁移、不重启容器。每项输出
PASS/FAIL,FAIL 时点名缺的是哪一项;需要 Docker daemon、psql 或对象存储凭据才能判定的
部分标 SKIP 并说明原因,因此没有 daemon 时静态部分仍可运行。

用法(仓库根目录):
  python3 scripts/dev-env/check-async-job-prereqs.py
  python3 scripts/dev-env/check-async-job-prereqs.py --env-file deploy/compose.env
  python3 scripts/dev-env/check-async-job-prereqs.py --offline
  python3 scripts/dev-env/check-async-job-prereqs.py --compose-file deploy/compose.yaml

退出码:0 无 FAIL(允许 SKIP);1 至少一项 FAIL;2 参数或路径错误.

判定边界:静态检查只读 `deploy/compose.env`、`deploy/compose.yaml` 与私有配置的
文件形态;运行时检查只做 `SELECT`、`LIMIT 0` 列权限探测、引擎 `/health` GET 和
`docker inspect` 读取,不插入、不更新、不授权、不建表。授权形状的最终权威仍是进程
启动校验,不是本脚本。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]

# `job_dispatch_grants`/`public_job_grants` 的成员由迁移决定;这里只列判定所需的
# 关系名。顺序不影响结果,缺失项会逐条点名。
REQUIRED_JOB_TABLES: tuple[str, ...] = (
    "coordination_jobs",
    "coordination_outbox",
    "admission_operations",
    "admission_attempts",
    "admission_requests",
    "admission_leases",
    "speech_jobs",
    "speech_job_events",
    "speech_job_submissions",
    "speech_job_cancellations",
    "speech_job_cancellation_fences",
    "speech_job_cancellation_reconciliations",
    "speech_job_cancellation_not_submitted",
    "speech_job_intents",
    "speech_artifacts",
)

# 与 job_dispatch_runtime.JobDispatchRuntime._check_role 的启动探测一致:worker 在
# 领取任何租约前先执行这些语句,因此这里执行同样的语句即可代表"启动能过"。
DISPATCH_PROBES: tuple[tuple[str, str], ...] = (
    ("speech_jobs", "SELECT job_id FROM {schema}.speech_jobs LIMIT 0"),
    (
        "speech_job_events",
        "SELECT job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
        "progress_completed, progress_total FROM {schema}.speech_job_events LIMIT 0",
    ),
    ("coordination_jobs", "SELECT job_id FROM {schema}.coordination_jobs LIMIT 0"),
    (
        "admission_operations",
        "SELECT operation_id, request_document FROM {schema}.admission_operations LIMIT 0",
    ),
    ("admission_attempts", "SELECT attempt_id FROM {schema}.admission_attempts LIMIT 0"),
    (
        "admission_requests",
        "SELECT request_document FROM {schema}.admission_requests LIMIT 0",
    ),
    (
        "admission_leases",
        "SELECT lease_id, lease_state, cleanup_state, deadline_at_us "
        "FROM {schema}.admission_leases LIMIT 0",
    ),
    ("speech_artifacts", "SELECT artifact_id FROM {schema}.speech_artifacts LIMIT 0"),
)

VOICE_VALIDATION_PROBES: tuple[tuple[str, str], ...] = (
    (
        "speech_voice_draft_revisions",
        "SELECT draft_id, revision FROM {schema}.speech_voice_draft_revisions LIMIT 0",
    ),
    ("speech_assets", "SELECT workspace_id, asset_id FROM {schema}.speech_assets LIMIT 0"),
)

DISPATCHABLE_KINDS = frozenset({"asr", "tts", "voice_design", "voice_clone", "voice_validation"})

# worker 的 owner/DDL 守卫,与启动校验使用同一表达式;结果必须是 `f`。
DISPATCH_ROLE_GUARD = (
    "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%(role)s::name, %(schema)s, 'CREATE') "
    "OR pg_catalog.pg_has_role(%(role)s::name, d.datdba, 'MEMBER') OR EXISTS ("
    "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
    "ON n.oid = c.relnamespace WHERE n.nspname = %(schema)s "
    "AND pg_catalog.pg_has_role(%(role)s::name, c.relowner, 'MEMBER')) "
    "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
    "WHERE r.rolname = %(role)s AND d.datname = CURRENT_DATABASE()"
)

Status = Literal["PASS", "FAIL"]


@dataclass(frozen=True)
class Dsn:
    """解析后的 PostgreSQL 连接串;只用于判定,不回写。"""

    scheme: str
    user: str
    password: str
    host: str
    port: int
    database: str
    sslmode: str | None
    raw: str

    def label(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database}"

    def server_key(self) -> tuple[str, int, str]:
        return (self.host.lower(), self.port, self.database)


@dataclass
class Prereq:
    """一项前置的判定结果:静态失败逐条记录,运行时证据单独说明。"""

    number: int
    title: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    runtime: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def probe(self, message: str) -> None:
        self.runtime.append(message)

    @property
    def status(self) -> Status:
        return "FAIL" if self.failures else "PASS"


@dataclass(frozen=True)
class PsqlResult:
    """一次只读 psql 调用的结果;`connected=False` 表示连接层失败。"""

    connected: bool
    ok: bool
    stdout: str
    error: str


def load_env_file(path: Path) -> dict[str, str]:
    """读取 KEY=VALUE 形式的 env 文件;忽略空行、注释与 `export` 前缀。"""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def parse_dsn(raw: str) -> Dsn | None:
    """解析 `postgresql://` 连接串;无法解析时返回 None,由调用方点名。"""
    text = raw.strip()
    if not text:
        return None
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in {"postgresql", "postgres", "postgresql+psycopg"}:
        return None
    if not parts.hostname:
        return None
    database = urllib.parse.unquote(parts.path.lstrip("/")) or "postgres"
    port = parts.port if parts.port is not None else 5432
    query = urllib.parse.parse_qs(parts.query)
    sslmode = query.get("sslmode", [None])[0]
    return Dsn(
        scheme=parts.scheme,
        user=urllib.parse.unquote(parts.username or ""),
        password=urllib.parse.unquote(parts.password or ""),
        host=parts.hostname,
        port=port,
        database=database,
        sslmode=sslmode,
        raw=text,
    )


def resolve_host(dsn: Dsn, env: dict[str, str]) -> tuple[str, int, str | None]:
    """把容器内服务名映射到宿主机发布端口,用于从宿主机执行只读检查.

    只映射参考部署的 `postgres:5432`;其它地址原样使用。返回 (host, port, note)。
    """
    if dsn.host == "postgres" and dsn.port == 5432:
        published = env.get("SPEECHNEXUS_POSTGRES_HOST_PORT", "55432").strip() or "55432"
        if published.isdigit():
            return (
                "127.0.0.1",
                int(published),
                f"容器内 postgres:5432 → 宿主机 127.0.0.1:{published}",
            )
    return (dsn.host, dsn.port, None)


def psql_select(
    dsn: Dsn, env: dict[str, str], statement: str, timeout_seconds: int = 12
) -> PsqlResult:
    """以 DSN 身份执行一条只读语句;凭据经 PG* 环境变量传递,不进 argv。"""
    binary = shutil.which("psql")
    if binary is None:
        return PsqlResult(connected=False, ok=False, stdout="", error="psql 不在 PATH")
    host, port, _ = resolve_host(dsn, env)
    child_env = os.environ.copy()
    child_env["PGHOST"] = host
    child_env["PGPORT"] = str(port)
    child_env["PGUSER"] = dsn.user
    child_env["PGDATABASE"] = dsn.database
    child_env["PGCONNECT_TIMEOUT"] = "5"
    child_env["PGAPPNAME"] = "speechnexus-async-job-prereqs-check"
    if dsn.password:
        child_env["PGPASSWORD"] = dsn.password
    if dsn.sslmode:
        child_env["PGSSLMODE"] = dsn.sslmode
    try:
        done = subprocess.run(
            [binary, "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", "-c", statement],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=child_env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PsqlResult(connected=True, ok=False, stdout="", error="psql 超时")
    except OSError as exc:  # pragma: no cover - 取决于本机安装
        return PsqlResult(connected=False, ok=False, stdout="", error=str(exc))
    if done.returncode == 0:
        return PsqlResult(connected=True, ok=True, stdout=done.stdout.strip(), error="")
    error = (done.stderr or done.stdout).strip().splitlines()
    message = error[0] if error else "psql 失败"
    connected = not any(
        marker in message.lower()
        for marker in (
            "could not connect",
            "connection refused",
            "no such host",
            "timeout expired",
            "server closed the connection",
        )
    )
    return PsqlResult(connected=connected, ok=False, stdout="", error=message)


def probe_database(dsn: Dsn, env: dict[str, str]) -> PsqlResult:
    """先确认能否连通;连不上不是 FAIL,而是运行时检查 SKIP。"""
    return psql_select(dsn, env, "SELECT 1")


def read_json(path: Path) -> object | None:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return document


def object_field(document: object, key: str) -> object | None:
    if isinstance(document, dict):
        value: object | None = document.get(key)
        return value
    return None


def private_dir(env: dict[str, str]) -> Path:
    raw = env.get("SPEECHNEXUS_PRIVATE_CONFIG_DIR", "").strip()
    if not raw:
        return REPO_ROOT / "deploy/.runtime/private"
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def compose_service_block(compose_text: str, service: str) -> str | None:
    """从 compose 文本中截取一个服务块(不引入 YAML 依赖,只做缩进切分)."""
    lines = compose_text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.rstrip() == f"  {service}:":
            start = index
            break
    if start is None:
        return None
    block: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            block.append(line)
            continue
        if not line.startswith("    ") and re.match(r"^  [A-Za-z0-9_.-]+:\s*$", line):
            break
        if not line.startswith(" "):
            break
        block.append(line)
    return "\n".join(block)


def compose_project_name(compose_text: str) -> str:
    match = re.search(r"(?m)^name:\s*([A-Za-z0-9_.-]+)\s*$", compose_text)
    return match.group(1) if match else "speechnexus"


def check_api_job_dsns(env: dict[str, str], compose_text: str, offline: bool) -> Prereq:
    check = Prereq(1, "API 侧 Job writer / admission DSN")
    parsed: dict[str, Dsn] = {}
    for name in (
        "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
        "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
    ):
        raw = env.get(name, "").strip()
        if not raw:
            check.fail(f"{name} 为空;POST /v1/nexus/jobs 直接返回 503 job_unavailable")
            continue
        dsn = parse_dsn(raw)
        if dsn is None:
            check.fail(f"{name} 不是可解析的 postgresql:// 连接串")
            continue
        parsed[name] = dsn
        if not dsn.user:
            check.fail(f"{name} 没有用户名;启动会按无效身份拒绝")
        owner = env.get("POSTGRES_USER", "").strip()
        if owner and dsn.user == owner:
            check.fail(f"{name} 复用了 bootstrap owner {owner};必须是独立非 owner 角色")
    writer = parsed.get("SPEECHNEXUS_JOB_WRITER_DATABASE_URL")
    admission = parsed.get("SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL")
    if writer is not None and admission is not None:
        if writer.user == admission.user:
            check.fail("writer 与 admission 使用同一角色;原子准入要求独立的非 owner 角色")
        if writer.server_key() != admission.server_key():
            check.fail(
                "writer 与 admission 不在同一服务器/数据库;启动会比对 current_database 后拒绝"
            )
    for name in (
        "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
        "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
    ):
        if name not in compose_text:
            check.fail(f"deploy/compose.yaml 没有把 {name} 传入 api;env 中的值不会被读取")
    if offline:
        check.probe("运行时: SKIP — --offline 未执行数据库探测")
        return check
    if writer is None or admission is None:
        check.probe("运行时: SKIP — DSN 不完整,跳过角色与同库实测")
        return check
    identities: dict[str, str] = {}
    for name, dsn in (
        ("writer", writer),
        ("admission", admission),
    ):
        probe = probe_database(dsn, env)
        if not probe.connected:
            check.probe(f"运行时: SKIP — {name} DSN 从本机不可达({probe.error})")
            return check
        if not probe.ok:
            check.fail(f"{name} 角色无法连接或查询失败:{probe.error}")
            return check
        identity = psql_select(
            dsn,
            env,
            "SELECT current_user || '|' || current_database() || '|' || "
            "COALESCE(host(inet_server_addr()), 'local') || '|' || "
            "COALESCE(inet_server_port(), 0)::text || '|' || "
            "(SELECT (rolsuper OR rolcreatedb OR rolcreaterole)::text "
            "FROM pg_roles WHERE rolname = current_user)",
        )
        if not identity.ok or identity.stdout.count("|") != 4:
            check.fail(f"{name} 角色身份探测失败:{identity.error or identity.stdout}")
            return check
        role, database, server, port, privileged = identity.stdout.split("|")
        if privileged.strip() == "true":
            check.fail(f"{name} 角色 {role} 仍持有 superuser/createdb/createrole")
        identities[name] = f"{server}:{port}/{database}"
    if len(set(identities.values())) == 1:
        check.probe(f"运行时: OK — writer/admission 实测同库 {next(iter(identities.values()))}")
    else:
        check.fail(f"writer/admission 实测不在同一服务器/数据库:{identities}")
    return check


def check_migration(env: dict[str, str], offline: bool) -> Prereq:
    check = Prereq(2, "迁移到唯一 head")
    raw = env.get("SPEECHNEXUS_MIGRATION_DATABASE_URL", "").strip()
    dsn = parse_dsn(raw) if raw else None
    if not raw:
        check.fail(
            "SPEECHNEXUS_MIGRATION_DATABASE_URL 为空;migration profile 无法执行,Job 表不存在"
        )
    elif dsn is None:
        check.fail("SPEECHNEXUS_MIGRATION_DATABASE_URL 不是可解析的 postgresql:// 连接串")
    if offline or dsn is None:
        check.probe("运行时: SKIP — 未执行迁移版本探测")
        return check
    probe = probe_database(dsn, env)
    if not probe.connected:
        check.probe("运行时: SKIP — 迁移 DSN 从本机不可达;请在部署网络内或用宿主机发布端口重跑")
        return check
    if not probe.ok:
        check.fail(f"迁移身份无法连接:{probe.error}")
        return check
    version = psql_select(dsn, env, "SELECT version_num FROM speech.alembic_version")
    if not version.ok:
        check.fail("speech.alembic_version 不存在或不可读;迁移未执行")
        return check
    if not version.stdout:
        check.fail("speech.alembic_version 没有版本行;迁移未提交")
        return check
    placeholders = ", ".join(f"'{name}'" for name in REQUIRED_JOB_TABLES)
    existing = psql_select(
        dsn,
        env,
        "SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = 'speech' AND c.relkind = 'r' AND relname IN ({placeholders})",
    )
    if not existing.ok:
        check.fail(f"Job 表存在性查询失败:{existing.error}")
        return check
    present = {name for name in existing.stdout.split(",") if name}
    missing = [name for name in REQUIRED_JOB_TABLES if name not in present]
    if missing:
        check.fail("缺少 Job/准入关系:" + ", ".join(missing))
        return check
    check.note(f"迁移版本 {version.stdout}")
    check.probe(f"运行时: OK — alembic_version={version.stdout},{len(present)} 张关系齐全")
    return check


def check_dispatch_role(env: dict[str, str], compose_text: str, offline: bool) -> Prereq:
    check = Prereq(3, "派发角色与五组 grant")
    raw = env.get("SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL", "").strip()
    dsn = parse_dsn(raw) if raw else None
    if not raw:
        check.fail(
            "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL 为空;派发 worker 在领取租约前 fail-closed"
        )
    elif dsn is None:
        check.fail("SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL 不是可解析的 postgresql:// 连接串")
    else:
        owner = env.get("POSTGRES_USER", "").strip()
        if owner and dsn.user == owner:
            check.fail(f"派发 DSN 复用了 bootstrap owner {owner};必须是独立非 owner 角色")
        for name in (
            "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
            "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
        ):
            other = parse_dsn(env.get(name, ""))
            if other is not None and dsn is not None and other.user == dsn.user:
                check.fail(f"派发角色与 {name} 复用同一身份;三者必须各自独立")
        if "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL" not in compose_text:
            check.fail(
                "deploy/compose.yaml 没有把 SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL 传给 worker"
            )
    kinds_raw = env.get("SPEECHNEXUS_JOB_DISPATCH_KINDS", "asr,tts").strip() or "asr,tts"
    kinds = [item.strip() for item in kinds_raw.split(",") if item.strip()]
    unknown = [kind for kind in kinds if kind not in DISPATCHABLE_KINDS]
    if unknown:
        check.fail("SPEECHNEXUS_JOB_DISPATCH_KINDS 含不可派发 kind:" + ", ".join(unknown))
    if "voice_validation" in kinds:
        check.note(
            "kinds 含 voice_validation;额外要求 voice_validation_worker_grants 的两张 SELECT"
        )
    if offline or dsn is None:
        check.probe("运行时: SKIP — 未执行角色/授权探测")
        return check
    probe = probe_database(dsn, env)
    if not probe.connected:
        check.probe("运行时: SKIP — 派发 DSN 从本机不可达,无法验证角色与授权")
        return check
    if not probe.ok:
        check.fail(f"派发角色无法连接:{probe.error}")
        return check
    guard = psql_select(
        dsn,
        env,
        DISPATCH_ROLE_GUARD.replace("%(role)s", f"'{dsn.user}'").replace("%(schema)s", "'speech'"),
    )
    if not guard.ok:
        check.fail(f"角色守卫查询失败:{guard.error}")
        return check
    if guard.stdout.strip() != "f":
        check.fail("派发角色命中 owner/DDL 守卫(superuser、schema CREATE 或表 owner 成员)")
    probes = DISPATCH_PROBES + (VOICE_VALIDATION_PROBES if "voice_validation" in kinds else ())
    rejected: list[str] = []
    for table, statement in probes:
        outcome = psql_select(dsn, env, statement.format(schema="speech"))
        if not outcome.ok:
            rejected.append(f"{table}({outcome.error})")
    if rejected:
        check.fail("派发角色的列权限探测失败:" + "; ".join(rejected[:4]))
        return check
    check.probe(f"运行时: OK — 角色守卫通过,{len(probes)} 条列权限探测全部通过")
    return check


def engine_health_url(env: dict[str, str]) -> str:
    raw = env.get("SPEECHNEXUS_ENGINE_HEALTH_URL", "").strip()
    if not raw:
        return ""
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    if parts.path in {"", "/"}:
        return raw.rstrip("/") + "/health"
    return raw


def check_engine(env: dict[str, str], offline: bool) -> Prereq:
    check = Prereq(4, "真实引擎与实例注册")
    raw = env.get("SPEECHNEXUS_ENGINE_HEALTH_URL", "").strip()
    url = engine_health_url(env)
    if not raw:
        check.fail("SPEECHNEXUS_ENGINE_HEALTH_URL 为空;注册与提交都没有目标引擎")
    elif not url:
        check.fail("SPEECHNEXUS_ENGINE_HEALTH_URL 不是 http(s) origin")
    instance = env.get("SPEECHNEXUS_ENGINE_INSTANCE_ID", "").strip()
    generation = env.get("SPEECHNEXUS_ENGINE_GENERATION", "").strip()
    if not instance:
        check.fail("SPEECHNEXUS_ENGINE_INSTANCE_ID 为空;注册与准入都需要实例身份")
    elif not re.fullmatch(r"[A-Za-z0-9._:-]+", instance):
        check.fail("SPEECHNEXUS_ENGINE_INSTANCE_ID 含非法字符")
    if not generation:
        check.fail("SPEECHNEXUS_ENGINE_GENERATION 为空")
    elif not generation.isdigit() or int(generation) < 1:
        check.fail("SPEECHNEXUS_ENGINE_GENERATION 必须是正整数")
    recipe = private_dir(env) / "transcription-recipe.json"
    if not recipe.is_file():
        check.fail(f"私有配方不存在:{recipe};engine-registration 无法读取")
    else:
        check.note(f"私有配方 {recipe}(mode {oct(recipe.stat().st_mode & 0o777)})")
    if offline:
        check.probe("运行时: SKIP — --offline 未探测引擎与注册表")
        return check
    if url:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            check.fail(f"引擎健康端点 {url} 返回 HTTP {exc.code}")
            status = 0
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            check.probe(
                f"运行时: SKIP — 引擎健康端点从本机不可达({exc});"
                "host.docker.internal 需在容器网络内探测"
            )
            status = 0
        if status == 200:
            check.probe(f"运行时: OK — {url} 返回 200")
        elif status not in {0, 200}:
            check.probe(f"运行时: 引擎端点返回 {status}")
    migration = parse_dsn(env.get("SPEECHNEXUS_MIGRATION_DATABASE_URL", ""))
    if migration is None or not instance or not generation.isdigit():
        check.probe("运行时: SKIP — 缺迁移 DSN 或实例身份,无法查询注册事实")
        return check
    probe = probe_database(migration, env)
    if not probe.connected:
        check.probe("运行时: SKIP — 数据库不可达,未查询 engine_instances")
        return check
    if not probe.ok:
        check.fail(f"查询引擎注册事实失败:{probe.error}")
        return check
    rows = psql_select(
        migration,
        env,
        "SELECT e.instance_generation::text || '|' || e.instance_state FROM "
        "speech.engine_instances e JOIN speech.runtime_instance_heads h "
        "ON h.instance_id = e.instance_id AND h.current_generation = e.instance_generation "
        f"WHERE e.instance_id = '{instance}' AND e.instance_generation = {int(generation)}",
    )
    if not rows.ok:
        check.fail(f"engine_instances 查询失败:{rows.error}")
        return check
    if not rows.stdout:
        check.fail(
            f"实例 {instance} generation {generation} 未注册;"
            "先执行 engine-registration 并取得 outcome=registered"
        )
        return check
    state = rows.stdout.split("|")[-1]
    if state != "ready":
        check.fail(f"实例 {instance} 的 instance_state={state};准入只接受 ready")
        return check
    check.probe(f"运行时: OK — 实例 {instance} g{generation} 已注册且 ready")
    return check


def check_result_bucket(env: dict[str, str], compose_text: str, offline: bool) -> Prereq:
    check = Prereq(5, "gateway result_bucket 存在且被创建/版本化")
    bucket = env.get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET", "").strip()
    if not bucket:
        check.fail("SPEECHNEXUS_EXECUTION_RESULT_BUCKET 为空;结果发布在 clamp 之后首次读回失败")
    artifact = env.get("SPEECHNEXUS_OBJECT_STORAGE_BUCKET", "").strip()
    if not artifact:
        check.fail("SPEECHNEXUS_OBJECT_STORAGE_BUCKET 为空;结果 Artifact 没有落点")
    directory = private_dir(env)
    for name in ("gateway-file-asr.json", "gateway-file-tts.json"):
        path = directory / name
        if not path.is_file():
            check.fail(f"私有 gateway 配置不存在:{path};无法核对 result_bucket")
            continue
        document = read_json(path)
        objects = object_field(document, "objects")
        configured = object_field(objects, "result_bucket")
        if not isinstance(configured, str) or not configured.strip():
            check.fail(f"{name} 的 objects.result_bucket 缺失或不是字符串")
            continue
        if bucket and configured != bucket:
            check.fail(
                f"{name} 的 result_bucket={configured} 与 env 的 {bucket} 不一致;"
                "worker 会按 env 桶名读取,而 gateway 写入另一个桶"
            )
    minio_block = compose_service_block(compose_text, "minio-init")
    if minio_block is None:
        check.fail("deploy/compose.yaml 缺少 minio-init;结果桶不会被创建")
    elif "SPEECHNEXUS_EXECUTION_RESULT_BUCKET" not in minio_block:
        check.fail("minio-init 没有接收 SPEECHNEXUS_EXECUTION_RESULT_BUCKET;该桶不会被创建")
    worker_block = compose_service_block(compose_text, "job-dispatch-worker")
    if worker_block is not None and "SPEECHNEXUS_EXECUTION_RESULT_BUCKET" not in worker_block:
        check.fail("job-dispatch-worker 没有接收 SPEECHNEXUS_EXECUTION_RESULT_BUCKET")
    if bucket and artifact and bucket == artifact:
        check.note(f"Artifact 与 staging 共用桶 {bucket}(参考部署取值)")
    check.probe(
        "运行时: SKIP — 桶存在与版本化需要对象存储签名凭据;"
        "本检查不持有凭据,请核对 minio-init 的 'bucket ready' 输出与 mc version info"
    )
    return check


def docker_containers(project: str) -> list[tuple[str, str, str]] | None:
    """读取 Compose 项目容器;daemon 或 CLI 不可用时返回 None(SKIP)."""
    binary = shutil.which("docker")
    if binary is None:
        return None
    try:
        done = subprocess.run(
            [
                binary,
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                '{{.ID}}\t{{.Label "com.docker.compose.service"}}\t{{.State}}',
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    if done.returncode != 0:
        return None
    rows: list[tuple[str, str, str]] = []
    for line in done.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            rows.append((fields[0], fields[1], fields[2]))
    return rows


def docker_inspect(container_id: str, template: str) -> str | None:
    binary = shutil.which("docker")
    if binary is None:
        return None
    try:
        done = subprocess.run(
            [binary, "inspect", "--format", template, container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def check_network_lifecycle(env: dict[str, str], compose_text: str, offline: bool) -> Prereq:
    check = Prereq(6, "gateway 网络命名空间与启动顺序")
    worker_block = compose_service_block(compose_text, "job-dispatch-worker")
    if worker_block is None:
        check.fail("deploy/compose.yaml 缺少 job-dispatch-worker 服务")
        check.probe("运行时: SKIP — 服务定义缺失")
        return check
    if 'network_mode: "service:gateway"' not in worker_block:
        check.fail("job-dispatch-worker 没有声明 network_mode: service:gateway")
    if "depends_on:" not in worker_block or "condition: service_healthy" not in worker_block:
        check.fail("job-dispatch-worker 没有以 gateway healthy 作为依赖条件")
    if "restart: true" not in worker_block:
        check.fail(
            "job-dispatch-worker 的 gateway 依赖没有 restart: true;重建 gateway 不会连带重建 worker"
        )
    gateway_block = compose_service_block(compose_text, "gateway")
    if gateway_block is None or "healthcheck:" not in gateway_block:
        check.fail("gateway 没有 healthcheck;worker 的 healthy 依赖无从满足")
    if "healthcheck:" in worker_block:
        check.note("worker 自带 healthcheck;请确认它真能反映领取状态")
    else:
        check.note("worker 没有 healthcheck:up -d/--wait 成功不代表它已就绪")
    if offline:
        check.probe("运行时: SKIP — --offline 未检查容器状态")
        return check
    rows = docker_containers(compose_project_name(compose_text))
    if rows is None:
        check.probe("运行时: SKIP — Docker CLI/daemon 不可用,未检查容器网络与重启次数")
        return check
    by_service: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        by_service.setdefault(row[1], row)
    if not by_service:
        check.probe("运行时: SKIP — 未发现该 Compose 项目的容器(部署尚未启动)")
        return check
    gateway = by_service.get("gateway")
    worker = by_service.get("job-dispatch-worker")
    if gateway is None:
        check.fail("gateway 容器不存在;先 up 基础拓扑再启用 job-dispatch profile")
        return check
    if worker is None:
        check.fail("job-dispatch-worker 未运行;--profile job-dispatch 未启用或进程已退出")
        return check
    network_mode = docker_inspect(worker[0], "{{.HostConfig.NetworkMode}}")
    if network_mode is None:
        check.probe("运行时: SKIP — docker inspect 读取失败")
        return check
    # `docker ps` 只给短 ID,inspect 的网络模式里是完整 ID;两边都取完整 ID 比较。
    gateway_id = docker_inspect(gateway[0], "{{.Id}}") or gateway[0]
    if network_mode != f"container:{gateway_id}":
        check.fail(
            f"worker 网络命名空间是 {network_mode},不是 gateway 容器 {gateway[0][:12]};"
            "gateway 重建后必须显式重建 worker"
        )
    restarts = docker_inspect(worker[0], "{{.RestartCount}}")
    running = docker_inspect(worker[0], "{{.State.Running}}")
    if running is not None and running != "true":
        check.fail(f"job-dispatch-worker 未在运行(状态 {worker[2]})")
    if restarts is not None and restarts.isdigit() and int(restarts) > 0:
        check.fail(f"job-dispatch-worker 已重启 {restarts} 次;查 logs 中的 DSN/授权/网关错误")
    gateway_health = docker_inspect(
        gateway[0], "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    )
    if gateway_health is not None and gateway_health != "healthy":
        check.fail(f"gateway 健康状态为 {gateway_health},worker 的依赖条件未满足")
    if not check.failures:
        check.probe(
            f"运行时: OK — worker 共享 gateway 命名空间,重启 {restarts or '0'} 次,"
            f"gateway 健康 {gateway_health}"
        )
    return check


def build_checks(env: dict[str, str], compose_text: str, offline: bool) -> list[Prereq]:
    return [
        check_api_job_dsns(env, compose_text, offline),
        check_migration(env, offline),
        check_dispatch_role(env, compose_text, offline),
        check_engine(env, offline),
        check_result_bucket(env, compose_text, offline),
        check_network_lifecycle(env, compose_text, offline),
    ]


def render(checks: list[Prereq], env_path: Path, compose_path: Path) -> None:
    print(f"async-job 前置自检: env={env_path} compose={compose_path}")
    print("只读检查:不改文件、不写数据库、不执行迁移、不重启容器")
    print()
    for check in checks:
        print(f"[{check.status}] 前置 {check.number}  {check.title}")
        for note in check.notes:
            print(f"        · {note}")
        for failure in check.failures:
            print(f"        ✗ {failure}")
        for runtime in check.runtime:
            print(f"        {runtime}")
        print()
    passed = sum(1 for check in checks if check.status == "PASS")
    failed = len(checks) - passed
    if failed:
        names = "、".join(f"前置 {check.number}" for check in checks if check.status == "FAIL")
        print(f"结果: {passed}/{len(checks)} PASS;缺项或配置不满足:{names}")
        print("修复后重跑本脚本;SKIP 项需在部署网络或持有凭据的环境单独核对。")
    else:
        print(f"结果: {passed}/{len(checks)} PASS;6 项前置的静态判定全部满足。")
        print("SKIP 项仍须按提示在部署网络内核对;通过本脚本不等于异步 Job 已跑通。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读检查参考部署的异步 Job 前置条件(6 项)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / "deploy/compose.env",
        help="Compose 环境文件(默认 deploy/compose.env)",
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=REPO_ROOT / "deploy/compose.yaml",
        help="Compose 定义(默认 deploy/compose.yaml)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="跳过 psql/HTTP/Docker 探测,只做静态判定",
    )
    args = parser.parse_args(argv)
    env_path: Path = args.env_file
    compose_path: Path = args.compose_file
    if not env_path.is_file():
        print(f"env 文件不存在:{env_path}", file=sys.stderr)
        print(
            "先按 docs/operations/compose-minimal.md 第 2 节生成 deploy/compose.env,"
            "或用 --env-file 指向实际文件。",
            file=sys.stderr,
        )
        return 1
    if not compose_path.is_file():
        print(f"compose 文件不存在:{compose_path}", file=sys.stderr)
        return 2
    env = load_env_file(env_path)
    try:
        compose_text = compose_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取 compose 文件失败:{exc}", file=sys.stderr)
        return 2
    checks = build_checks(env, compose_text, args.offline)
    render(checks, env_path, compose_path)
    return 0 if all(check.status == "PASS" for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
