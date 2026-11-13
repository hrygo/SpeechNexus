#!/usr/bin/env python3
"""只读检查参考部署能否跑通一次异步 Job 的 6 项前置.

为什么需要它:启用 `job-dispatch` profile 后,`up -d`/`--wait` 仍可能"成功",而 Job
卡在 `pending`/`running`。缺的往往不是进程,而是 6 项部署前置中的一项:API 的 Job
writer/admission DSN、迁移到唯一 head、派发角色的读写授权、已注册的 ready 引擎实例、
gateway 的 `result_bucket`,以及 `network_mode: service:gateway` 下的启动顺序。

本脚本只做判断,不做修复:不改文件、不写数据库、不执行迁移、不重启容器。每项输出
PASS/FAIL/PENDING,FAIL 时点名缺的是哪一项;需要 Docker daemon、psql 或对象存储凭据才能
判定的部分标 SKIP 并说明原因,因此没有 daemon 时静态部分仍可运行。

三种判定:
  PASS    该项当前满足。
  FAIL    配置写了但不对(桶名不一致、DSN 为空、迁移落后、权限缺失等),任何阶段都必须修。
  PENDING 依赖部署产物(私有配置、ready 实例)且尚未到该阶段;部署前没有意义,不是错误。

阶段由 `--phase auto|pre|post` 决定,默认 `auto`:Compose 项目容器、`deploy/.runtime`
或私有配置目录下已生成文件,任一出现即按部署后判定,否则按部署前;`mkdir -p` 出来的
空骨架不算部署。只有标为部署后的前置(4、5)会在 pre 阶段把缺项记为 PENDING;真实
配置错误永远 FAIL,不会被阶段降级。

用法(仓库根目录):
  python3 scripts/dev-env/check-async-job-prereqs.py
  python3 scripts/dev-env/check-async-job-prereqs.py --env-file deploy/compose.env
  python3 scripts/dev-env/check-async-job-prereqs.py --offline
  python3 scripts/dev-env/check-async-job-prereqs.py -f deploy/compose.yaml
  python3 scripts/dev-env/check-async-job-prereqs.py --phase pre|post|auto

退出码:0 无 FAIL(允许 SKIP 与 PENDING;只有 PENDING 时摘要明确"尚未到该阶段");1 至少
一项 FAIL(env 文件缺失也是 1);2 参数或 compose 路径错误.

判定边界:静态检查只读 `deploy/compose.env`、`deploy/compose.yaml` 与私有配置的
文件形态;运行时检查只做 `SELECT`(含 `has_*_privilege` 与 `LIMIT 0` 列探测)、引擎
`/health` GET 和 `docker inspect` 读取,不插入、不更新、不授权、不建表。脚本验证的是
"缺少权限"这一类静默失败,不验证"多授予了权限"——后者的权威仍是进程启动校验。

脚本里的探测语句与生产代码同源:常量复制自 `job_dispatch_runtime` 与 `job_runtime`,
由 `tests/unit/test_async_job_prereqs_check.py` 断言逐字相等,生产改动而脚本未跟随时
该测试变红。
"""

from __future__ import annotations

import argparse
import ast
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
VERSIONS_DIR = (
    REPO_ROOT / "backend/src/speechnexus_backend/speech/infrastructure/migrations/versions"
)

# 迁移必须已建出的 Job/准入关系;缺失时创建或领取必然失败。这些名字都必须是
# job_runtime 声明的 Job writer 关系(同源测试断言其子集)。
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

# 复制自 job_dispatch_runtime.DISPATCH_STARTUP_PROBES;语句不带 schema 前缀,依赖
# search_path(psql 由 PGOPTIONS 设置,worker 由连接池设置)。第一项是用于报错的表名。
DISPATCH_PROBES: tuple[tuple[str, str], ...] = (
    ("speech_jobs", "SELECT job_id FROM speech_jobs LIMIT 0"),
    (
        "speech_job_events",
        "SELECT job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
        "progress_completed, progress_total FROM speech_job_events LIMIT 0",
    ),
    ("coordination_jobs", "SELECT job_id FROM coordination_jobs LIMIT 0"),
    (
        "admission_operations",
        "SELECT operation_id, request_document FROM admission_operations LIMIT 0",
    ),
    ("admission_attempts", "SELECT attempt_id FROM admission_attempts LIMIT 0"),
    ("admission_requests", "SELECT request_document FROM admission_requests LIMIT 0"),
    (
        "admission_leases",
        "SELECT lease_id, lease_state, cleanup_state, deadline_at_us FROM admission_leases LIMIT 0",
    ),
    ("speech_artifacts", "SELECT artifact_id FROM speech_artifacts LIMIT 0"),
)

# 复制自 job_dispatch_runtime.DISPATCH_VOICE_VALIDATION_PROBES;只在 kinds 含
# voice_validation 时探测,与 worker 启动条件一致。
VOICE_VALIDATION_PROBES: tuple[tuple[str, str], ...] = (
    (
        "speech_voice_draft_revisions",
        "SELECT draft_id, revision FROM speech_voice_draft_revisions LIMIT 0",
    ),
    ("speech_assets", "SELECT workspace_id, asset_id FROM speech_assets LIMIT 0"),
)

# 复制自 job_dispatch_runtime.DISPATCH_ROLE_GUARD_SQL;为真即拒绝启动。
DISPATCH_ROLE_GUARD = (
    "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%(role)s::name,%(schema)s,'CREATE') "
    "OR pg_catalog.pg_has_role(%(role)s::name,d.datdba,'MEMBER') OR EXISTS ("
    "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
    "ON n.oid=c.relnamespace WHERE n.nspname=%(schema)s "
    "AND pg_catalog.pg_has_role(%(role)s::name,c.relowner,'MEMBER')) AS forbidden "
    "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
    "WHERE r.rolname=%(role)s AND d.datname=CURRENT_DATABASE()"
)

# worker 在领取租约前只探测 SELECT,写权限靠部署授权;这里补上它认领后实际写入的
# 每一列,否则 REVOKE INSERT ON speech.speech_artifacts 或撤销终止释放列之后仍会
# 误报 PASS(写入会在领取之后才失败)。关系清单与列清单分别来自 worker 的代码:
# job_dispatch_postgres.py(认领/终止)、artifacts_postgres.py(结果发布)与
# lease_release_sql.py(容量释放);列声明来自 artifact_schema.ARTIFACT_INSERT_COLUMNS
# 与 coordination_grants.SPEECH_UPDATES。deploy/job-startup-gate.py 的
# DISPATCH_WRITE_SURFACE 是该表的另一份副本,由 tests/unit 的等式测试约束。
DISPATCH_WRITE_PROBES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
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
    (
        "speech_artifacts",
        "INSERT",
        (
            "artifact_id",
            "workspace_id",
            "artifact_kind",
            "artifact_state",
            "media_type",
            "bytes",
            "sha256",
            "receipt_id",
            "source_operation_id",
            "object_key",
            "storage_version_id",
            "created_at_us",
            "expires_at_us",
        ),
    ),
    (
        "admission_leases",
        "UPDATE",
        (
            "lease_state",
            "cleanup_state",
            "budget_released_at_us",
            "quarantine_observed_at_us",
            "quarantine_reason_code",
            "version",
        ),
    ),
    ("admission_attempts", "UPDATE", ("quota_state", "version")),
    (
        "workspace_quotas",
        "UPDATE",
        ("held_attempts", "quarantined_attempts", "version"),
    ),
    (
        "host_nodes",
        "UPDATE",
        (
            "node_state",
            "version",
            "held_unified_physical_memory_bytes",
            "held_device_memory_bytes",
            "held_host_memory_bytes",
            "held_cpu_decode_millicores",
            "held_host_pcm_buffer_bytes",
            "held_scratch_disk_bytes",
            "quarantined_unified_physical_memory_bytes",
            "quarantined_device_memory_bytes",
            "quarantined_host_memory_bytes",
            "quarantined_cpu_decode_millicores",
            "quarantined_host_pcm_buffer_bytes",
            "quarantined_scratch_disk_bytes",
        ),
    ),
    (
        "engine_instances",
        "UPDATE",
        (
            "instance_state",
            "held_slots",
            "quarantined_slots",
            "base_budget_kind",
            "ready_at_us",
            "stopped_at_us",
            "quarantine_reason",
            "last_fencing_token",
            "version",
        ),
    ),
)

# 复制自 job_runtime.REQUIRED_SELECT:API 的 Job writer 启动时要求这些表可读。
WRITER_REQUIRED_SELECT: tuple[str, ...] = (
    "speech_workspaces",
    "speech_publications",
    "speech_publication_heads",
    "speech_catalog_state",
    "speech_publication_retirements",
    "speech_binding_revisions",
    "speech_assets",
    "speech_pronunciation_revisions",
    "speech_jobs",
    "speech_job_intents",
    "speech_job_events",
    "speech_job_submissions",
    "speech_job_cancellations",
    "speech_artifacts",
    "speech_voice_drafts",
    "speech_voice_draft_revisions",
    "speech_voice_draft_publications",
    "speech_voice_metadata_commands",
)

# 复制自 job_writer_columns.JOB_WRITER_INSERT_COLUMNS:表级 INSERT 会被拒绝,
# 写者只允许这些列的列级 INSERT。
WRITER_INSERT_COLUMNS: dict[str, tuple[str, ...]] = {
    "coordination_jobs": (
        "job_id",
        "operation_id",
        "attempt_id",
        "initial_event_id",
        "initial_event_digest",
        "request_digest",
    ),
    "coordination_outbox": (
        "event_id",
        "job_id",
        "event_digest",
    ),
    "admission_operations": (
        "operation_id",
        "workspace_id",
        "request_digest",
        "request_document",
        "initial_attempt_id",
        "current_attempt_id",
        "recorded_at_us",
    ),
    "speech_job_intents": (
        "job_id",
        "request_digest",
        "request_document",
        "recorded_at_us",
    ),
    "speech_artifacts": (
        "artifact_id",
        "workspace_id",
        "artifact_kind",
        "artifact_state",
        "media_type",
        "bytes",
        "sha256",
        "receipt_id",
        "source_operation_id",
        "object_key",
        "storage_version_id",
        "created_at_us",
        "expires_at_us",
    ),
    "speech_job_cancellation_not_submitted": (
        "job_id",
        "observed_at_us",
        "observer",
    ),
    "speech_jobs": (
        "job_id",
        "workspace_id",
        "kind",
        "model_id",
        "request_digest",
        "idempotency_key_digest",
        "state",
        "status_version",
        "result_json",
        "error_code",
        "created_at_us",
        "updated_at_us",
        "retry_of_job_id",
    ),
    "speech_job_events": (
        "job_id",
        "event_seq",
        "state",
        "occurred_at_us",
        "kind",
        "progress_unit",
        "progress_completed",
        "progress_total",
    ),
    "speech_job_submissions": (
        "job_id",
        "request_id",
        "client_reference",
        "effective_configuration_json",
        "execution_revision",
        "progress_unit",
        "progress_total",
        "submitted_at_us",
    ),
    "speech_job_cancellations": (
        "job_id",
        "workspace_id",
        "operation_id",
        "attempt_id",
        "request_id",
        "request_digest",
        "idempotency_key_digest",
        "accepted",
        "requested_at_us",
    ),
    "speech_job_cancellation_fences": (
        "job_id",
        "accepted_at_us",
        "request_id",
    ),
    "speech_job_cancellation_reconciliations": (
        "job_id",
        "attempts",
        "next_check_at_us",
        "last_outcome",
        "settled_at_us",
        "updated_at_us",
    ),
    "speech_voice_drafts": (
        "draft_id",
        "generation_job_id",
        "kind",
        "status",
        "head_revision",
        "metadata_revision",
        "display_name",
        "input_json",
        "binding_json",
        "created_at_us",
        "updated_at_us",
    ),
    "speech_voice_draft_revisions": (
        "draft_id",
        "revision",
        "kind",
        "generation_job_id",
        "input_json",
        "binding_json",
        "created_at_us",
    ),
    "speech_voice_metadata_commands": (
        "draft_id",
        "key_digest",
        "request_digest",
        "result_json",
    ),
}

# 复制自 job_runtime._JOB_UPDATE_SURFACE:唯一允许的 UPDATE 列面。
WRITER_UPDATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "speech_voice_drafts": (
        "display_name",
        "metadata_revision",
        "updated_at_us",
        "head_revision",
        "generation_job_id",
    ),
    "speech_jobs": (
        "state",
        "status_version",
        "result_json",
        "error_code",
        "updated_at_us",
    ),
    "coordination_jobs": (
        "claim_owner_id",
        "claim_expires_at_us",
        "claim_version",
        "job_state",
    ),
    "speech_job_cancellation_reconciliations": (
        "attempts",
        "next_check_at_us",
        "last_outcome",
        "settled_at_us",
        "updated_at_us",
    ),
}

# 复制自 speech.domain.job_dispatch.DispatchJobKind 的取值。
DISPATCHABLE_KINDS = frozenset({"asr", "tts", "voice_design", "voice_clone", "voice_validation"})
# 本参考部署装配的车道:deploy/compose.yaml 只把 asr 与 tts 的私有 gateway 配置传给
# gateway,启动闸门的 GATEWAY_CONFIGS 与之相同。其余 kind 会被 worker 认领后在提交/
# 发布阶段失败,所以与闸门一样判 FAIL,而不是留作部署后再说。
ASSEMBLED_KINDS = frozenset({"asr", "tts"})

# 本次运行按部署前还是部署后判定;pre 表示私有配置/容器/ready 实例还不存在。
Phase = Literal["pre", "post"]
# FAIL 是"配置写了但不对",PENDING 是"部署产物尚未生成"。
Status = Literal["PASS", "FAIL", "PENDING"]


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
    """一项前置的判定结果:静态失败逐条记录,运行时证据单独说明。

    `required_phase` 是该项依赖的部署产物从哪个阶段起必须存在:标为 `post` 的前置
    (4、5)在 pre 阶段把缺项记 PENDING;`phase` 是本次运行的阶段。真实配置错误只走
    `fail()`,不受阶段影响。
    """

    number: int
    title: str
    required_phase: Phase = "pre"
    phase: Phase = "pre"
    failures: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    runtime: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def unresolved(self, message: str) -> None:
        """部署产物尚未就位:pre 阶段记 PENDING,post 阶段记 FAIL。"""
        if self.phase == "pre" and self.required_phase == "post":
            self.pending.append(message)
        else:
            self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def probe(self, message: str) -> None:
        self.runtime.append(message)

    @property
    def status(self) -> Status:
        if self.failures:
            return "FAIL"
        if self.pending:
            return "PENDING"
        return "PASS"


@dataclass(frozen=True)
class PhaseDecision:
    """本次运行采用的阶段与判定依据,用于让输出可解释。"""

    phase: Phase
    reason: str


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
    # 探测语句与生产一样不带 schema 前缀,由 search_path 定位。
    child_env["PGOPTIONS"] = "-c search_path=speech"
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


def read_json(path: Path) -> tuple[object | None, str | None]:
    """读取 JSON;返回 (文档, 错误说明)。

    解析失败与读文件失败都是"配置写了但不对",不是部署产物:调用方据此点名该修文件,
    而不是与"缺键"共用一句含糊提示。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"无法读取({exc})"
    try:
        document: object = json.loads(text)
    except ValueError as exc:
        return None, f"不是合法 JSON({exc})"
    return document, None


def object_field(document: object, key: str) -> object | None:
    if isinstance(document, dict):
        value: object | None = document.get(key)
        return value
    return None


def private_dir(env: dict[str, str], compose_path: Path) -> Path:
    """Compose 按 `deploy/compose.yaml` 所在目录解析相对挂载源,这里保持一致。"""
    raw = env.get("SPEECHNEXUS_PRIVATE_CONFIG_DIR", "").strip()
    if not raw:
        return compose_path.parent / ".runtime/private"
    path = Path(raw)
    if path.is_absolute():
        return path
    return (compose_path.parent / path).resolve()


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


def compose_environment(block: str) -> dict[str, str]:
    """读取服务块 `environment:` 下的键值;注释行与列表形式都按 YAML 语义处理."""
    values: dict[str, str] = {}
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "environment:":
            continue
        base = len(line) - len(line.lstrip())
        for inner in lines[index + 1 :]:
            if not inner.strip():
                continue
            indent = len(inner) - len(inner.lstrip())
            if indent <= base:
                break
            text = inner.strip()
            if text.startswith("#"):
                continue
            if text.startswith("- "):
                key, _separator, value = text[2:].strip().partition("=")
                values[key.strip()] = value.strip()
                continue
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", text)
            if match:
                values[match.group(1)] = match.group(2).strip()
        break
    return values


def wired(block: str | None, name: str) -> bool:
    """键必须出现在该服务的 environment 里且值非空;注释里出现不算."""
    if block is None:
        return False
    return bool(compose_environment(block).get(name, "").strip())


def wired_reference(block: str | None, name: str, source: str) -> bool:
    """该服务的 `name` 必须引用宿主机变量 `source`(如 ${VAR:-})."""
    if block is None:
        return False
    return source in compose_environment(block).get(name, "")


def compose_project_name(compose_text: str) -> str:
    match = re.search(r"(?m)^name:\s*([A-Za-z0-9_.-]+)\s*$", compose_text)
    return match.group(1) if match else "speechnexus"


def _values_sql(rows: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(f"('{table}','{column}')" for table, column in rows)


def _writer_privilege_sql() -> str:
    tables = ", ".join(f"'{name}'" for name in WRITER_REQUIRED_SELECT)
    inserts = _values_sql(
        tuple(
            (table, column)
            for table, columns in WRITER_INSERT_COLUMNS.items()
            for column in columns
        )
    )
    updates = _values_sql(
        tuple(
            (table, column)
            for table, columns in WRITER_UPDATE_COLUMNS.items()
            for column in columns
        )
    )
    return (
        f"SELECT 'missing SELECT on ' || t FROM unnest(ARRAY[{tables}]::text[]) AS t "
        "WHERE NOT pg_catalog.has_table_privilege(current_user, 'speech.' || t, 'SELECT') "
        "UNION ALL "
        f"SELECT 'missing INSERT on ' || v.t || '.' || v.c FROM (VALUES {inserts}) AS v(t, c) "
        "WHERE NOT pg_catalog.has_column_privilege("
        "current_user, 'speech.' || v.t, v.c, 'INSERT') "
        "UNION ALL "
        f"SELECT 'missing UPDATE on ' || v.t || '.' || v.c FROM (VALUES {updates}) AS v(t, c) "
        "WHERE NOT pg_catalog.has_column_privilege("
        "current_user, 'speech.' || v.t, v.c, 'UPDATE') "
        "ORDER BY 1"
    )


def _dispatch_write_sql() -> str:
    inserts = _values_sql(
        tuple(
            (table, column)
            for table, privilege, columns in DISPATCH_WRITE_PROBES
            if privilege == "INSERT"
            for column in columns
        )
    )
    updates = _values_sql(
        tuple(
            (table, column)
            for table, privilege, columns in DISPATCH_WRITE_PROBES
            if privilege == "UPDATE"
            for column in columns
        )
    )
    return (
        f"SELECT 'missing INSERT on ' || v.t || '.' || v.c FROM (VALUES {inserts}) AS v(t, c) "
        "WHERE NOT pg_catalog.has_column_privilege("
        "current_user, 'speech.' || v.t, v.c, 'INSERT') "
        "UNION ALL "
        f"SELECT 'missing UPDATE on ' || v.t || '.' || v.c FROM (VALUES {updates}) AS v(t, c) "
        "WHERE NOT pg_catalog.has_column_privilege("
        "current_user, 'speech.' || v.t, v.c, 'UPDATE') "
        "ORDER BY 1"
    )


def _summarize(lines: list[str], limit: int = 6) -> str:
    head = ";".join(lines[:limit])
    if len(lines) > limit:
        return f"{head};…共 {len(lines)} 项"
    return head


def check_api_job_dsns(
    env: dict[str, str], compose_text: str, offline: bool, phase: Phase
) -> Prereq:
    check = Prereq(1, "API 侧 Job writer / admission DSN 与 writer 权限", phase=phase)
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
    api_block = compose_service_block(compose_text, "api")
    if api_block is None:
        check.fail("deploy/compose.yaml 缺少 api 服务")
    for name in (
        "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
        "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
    ):
        if not wired(api_block, name):
            check.fail(f"api 的 environment 没有把 {name} 传入容器;env 中的值不会被读取")
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
    schema_create = psql_select(
        writer, env, "SELECT pg_catalog.has_schema_privilege(current_user, 'speech', 'CREATE')"
    )
    if not schema_create.ok:
        check.fail(f"writer 的 schema 权限探测失败:{schema_create.error}")
        return check
    if schema_create.stdout.strip() == "t":
        check.fail("writer 角色持有 speech 的 CREATE;API 启动会按 DDL 权限拒绝")
    privileges = psql_select(writer, env, _writer_privilege_sql())
    if not privileges.ok:
        check.fail(f"writer 的读写权限探测失败:{privileges.error}")
        return check
    if privileges.stdout:
        missing = [line for line in privileges.stdout.splitlines() if line.strip()]
        check.fail(
            "writer 缺少启动/写入所需的权限(API 会在启动时拒绝,"
            "Job 创建返回 503):" + _summarize(missing)
        )
        return check
    check.probe(
        "运行时: OK — writer/admission 同库、非 owner;"
        f"{len(WRITER_REQUIRED_SELECT)} 张表可读,INSERT/UPDATE 列权限齐全"
    )
    return check


def migration_graph(
    versions_dir: Path,
) -> tuple[dict[str, tuple[str, ...]], set[str]] | None:
    """解析 Alembic 版本脚本的 revision/down_revision;目录缺失时返回 None."""
    if not versions_dir.is_dir():
        return None
    parents: dict[str, tuple[str, ...]] = {}
    revisions: set[str] = set()
    for path in sorted(versions_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
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
        return None
    return parents, revisions


def migration_heads(versions_dir: Path) -> tuple[set[str], dict[str, tuple[str, ...]]] | None:
    graph = migration_graph(versions_dir)
    if graph is None:
        return None
    parents, revisions = graph
    heads = {revision for revision in revisions if revision not in _all_parents(parents)}
    return heads, parents


def _all_parents(parents: dict[str, tuple[str, ...]]) -> set[str]:
    return {parent for items in parents.values() for parent in items}


def _ancestors(revision: str, parents: dict[str, tuple[str, ...]]) -> set[str]:
    seen: set[str] = set()
    pending = [revision]
    while pending:
        current = pending.pop()
        for parent in parents.get(current, ()):
            if parent not in seen:
                seen.add(parent)
                pending.append(parent)
    return seen


def check_migration(env: dict[str, str], offline: bool, phase: Phase) -> Prereq:
    check = Prereq(2, "迁移到唯一 head", phase=phase)
    raw = env.get("SPEECHNEXUS_MIGRATION_DATABASE_URL", "").strip()
    dsn = parse_dsn(raw) if raw else None
    if not raw:
        check.fail(
            "SPEECHNEXUS_MIGRATION_DATABASE_URL 为空;migration profile 无法执行,Job 表不存在"
        )
    elif dsn is None:
        check.fail("SPEECHNEXUS_MIGRATION_DATABASE_URL 不是可解析的 postgresql:// 连接串")
    heads_result = migration_heads(VERSIONS_DIR)
    if heads_result is None:
        check.note(f"未找到迁移脚本目录 {VERSIONS_DIR};无法比对 head,只能核对表存在性")
        heads: set[str] | None = None
        parents: dict[str, tuple[str, ...]] = {}
    else:
        heads, parents = heads_result
        if len(heads) != 1:
            check.fail(
                "迁移脚本存在多个 head:"
                + ", ".join(sorted(heads))
                + ";单库只能有一个 head,先合并分支"
            )
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
    version = psql_select(
        dsn,
        env,
        "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM speech.alembic_version",
    )
    if not version.ok:
        check.fail("speech.alembic_version 不存在或不可读;迁移未执行")
        return check
    version_count = psql_select(dsn, env, "SELECT count(*)::text FROM speech.alembic_version")
    if not version_count.ok:
        check.fail(f"迁移版本表查询失败:{version_count.error}")
        return check
    if version_count.stdout.strip() not in {"", "1"}:
        check.fail(
            f"speech.alembic_version 有 {version_count.stdout.strip()} 行;"
            "多个版本行说明多个 head 已应用,先按对账流程收敛"
        )
        return check
    if not version.stdout:
        check.fail("speech.alembic_version 没有版本行;迁移未提交")
        return check
    database_version = version.stdout.strip()
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
    missing_tables = [name for name in REQUIRED_JOB_TABLES if name not in present]
    if missing_tables:
        check.fail("缺少 Job/准入关系:" + ", ".join(missing_tables))
        return check
    if heads is not None and len(heads) == 1:
        head = next(iter(heads))
        if database_version != head:
            known = set(parents) | heads
            if database_version not in known:
                check.fail(
                    f"数据库版本 {database_version} 不是本仓库已知 revision;"
                    f"发布制品 head 是 {head},迁移与制品不匹配"
                )
                return check
            missing = sorted(
                (_ancestors(head, parents) | {head})
                - (_ancestors(database_version, parents) | {database_version})
            )
            check.fail(
                f"数据库停在 {database_version},落后 head {head};"
                f"缺 {len(missing)} 个 revision:{_summarize(missing)}"
            )
            return check
    check.note(f"迁移版本 {database_version}" + (f"(head {next(iter(heads))})" if heads else ""))
    check.probe(f"运行时: OK — alembic_version={database_version},{len(present)} 张关系齐全")
    return check


def check_dispatch_role(
    env: dict[str, str], compose_text: str, offline: bool, phase: Phase
) -> Prereq:
    check = Prereq(3, "派发角色与五组 grant(读 + 写)", phase=phase)
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
        worker_block = compose_service_block(compose_text, "job-dispatch-worker")
        if worker_block is None:
            check.fail("deploy/compose.yaml 缺少 job-dispatch-worker 服务")
        elif not wired_reference(
            worker_block, "SPEECHNEXUS_DATABASE_URL", "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"
        ):
            check.fail(
                "job-dispatch-worker 的 SPEECHNEXUS_DATABASE_URL 没有引用 "
                "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL;派发角色不会被使用"
            )
    kinds_raw = env.get("SPEECHNEXUS_JOB_DISPATCH_KINDS", "asr,tts").strip() or "asr,tts"
    kinds = [item.strip() for item in kinds_raw.split(",") if item.strip()]
    unknown = [kind for kind in kinds if kind not in DISPATCHABLE_KINDS]
    if unknown:
        check.fail("SPEECHNEXUS_JOB_DISPATCH_KINDS 含不可派发 kind:" + ", ".join(unknown))
    unassembled = [kind for kind in kinds if kind not in ASSEMBLED_KINDS]
    if unassembled:
        check.fail(
            "SPEECHNEXUS_JOB_DISPATCH_KINDS 含本参考部署未装配的车道:"
            + ", ".join(unassembled)
            + ";deploy/compose.yaml 只把 asr/tts 的 gateway 配置传给 gateway,"
            "worker 会认领该车道的 Job 后在提交阶段失败。要么从 kinds 里去掉,"
            "要么先装配车道(私有 gateway 配置、SPEECHNEXUS_GATEWAY_FILE_*_CONFIG_FILE "
            "与闸门的 GATEWAY_CONFIGS)"
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
    identities = psql_select(dsn, env, "SELECT SESSION_USER || '|' || CURRENT_USER")
    if not identities.ok or "|" not in identities.stdout:
        check.fail(f"派发角色身份探测失败:{identities.error or identities.stdout}")
        return check
    guard_failure = ""
    for role in dict.fromkeys(part.strip() for part in identities.stdout.split("|")):
        guard = psql_select(
            dsn,
            env,
            DISPATCH_ROLE_GUARD.replace("%(role)s", f"'{role}'").replace("%(schema)s", "'speech'"),
        )
        if not guard.ok:
            check.fail(f"角色守卫查询失败:{guard.error}")
            return check
        if guard.stdout.strip() != "f":
            guard_failure = role
    if guard_failure:
        check.fail(
            f"派发角色 {guard_failure} 命中 owner/DDL 守卫"
            "(superuser、schema CREATE 或表 owner 成员)"
        )
    probes = DISPATCH_PROBES + (VOICE_VALIDATION_PROBES if "voice_validation" in kinds else ())
    rejected: list[str] = []
    for table, statement in probes:
        outcome = psql_select(dsn, env, statement)
        if not outcome.ok:
            rejected.append(f"{table}({outcome.error})")
    if rejected:
        check.fail("派发角色的列权限探测失败:" + ";".join(rejected[:4]))
        return check
    writes = psql_select(dsn, env, _dispatch_write_sql())
    if not writes.ok:
        check.fail(f"派发角色的写权限探测失败:{writes.error}")
        return check
    if writes.stdout:
        missing_writes = [line for line in writes.stdout.splitlines() if line.strip()]
        check.fail(
            "派发角色缺少派发/终止写入所需的列权限(worker 会在运行时失败):"
            + _summarize(missing_writes)
        )
        return check
    check.probe(
        f"运行时: OK — 角色守卫通过,{len(probes)} 条列权限探测与 "
        f"{sum(len(cols) for _, _, cols in DISPATCH_WRITE_PROBES)} 条写入列权限全部通过"
    )
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


def check_engine(env: dict[str, str], compose_path: Path, offline: bool, phase: Phase) -> Prereq:
    check = Prereq(
        4,
        "真实引擎与实例注册",
        required_phase="post",
        phase=phase,
    )
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
    recipe = private_dir(env, compose_path) / "transcription-recipe.json"
    if not recipe.is_file():
        check.unresolved(
            f"私有配方尚未生成:{recipe};"
            "部署准备阶段按 compose-minimal 生成后,engine-registration 才能读取"
        )
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
        check.unresolved(
            f"实例 {instance} generation {generation} 未注册;"
            "先执行 engine-registration 并取得 outcome=registered"
        )
        return check
    state = rows.stdout.split("|")[-1]
    if state != "ready":
        # 行是刚查出来的活证据:部署已经发生,实例存在却没 ready 是配置/运维错误,
        # 不是"尚未到该阶段"。只有查不到行(`unresolved` 上面那处)才算 PENDING。
        check.fail(f"实例 {instance} 的 instance_state={state};准入只接受 ready")
        return check
    check.probe(f"运行时: OK — 实例 {instance} g{generation} 已注册且 ready")
    return check


def check_result_bucket(
    env: dict[str, str], compose_text: str, compose_path: Path, phase: Phase
) -> Prereq:
    check = Prereq(
        5,
        "gateway result_bucket 存在且被创建/版本化",
        required_phase="post",
        phase=phase,
    )
    bucket = env.get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET", "").strip()
    if not bucket:
        check.fail("SPEECHNEXUS_EXECUTION_RESULT_BUCKET 为空;结果发布在 clamp 之后首次读回失败")
    artifact = env.get("SPEECHNEXUS_OBJECT_STORAGE_BUCKET", "").strip()
    if not artifact:
        check.fail("SPEECHNEXUS_OBJECT_STORAGE_BUCKET 为空;结果 Artifact 没有落点")
    directory = private_dir(env, compose_path)
    for name in ("gateway-file-asr.json", "gateway-file-tts.json"):
        path = directory / name
        if not path.is_file():
            check.unresolved(
                f"私有 gateway 配置尚未生成:{path};"
                "部署准备阶段从 deploy/private/*.example.json 复制后,才能核对 result_bucket"
            )
            continue
        document, parse_error = read_json(path)
        if parse_error is not None:
            check.fail(f"{name} {parse_error};修正该文件后才能核对 result_bucket")
            continue
        objects = object_field(document, "objects")
        if not isinstance(objects, dict):
            check.fail(f"{name} 缺少 objects 对象;无法核对 result_bucket")
            continue
        configured = object_field(objects, "result_bucket")
        if not isinstance(configured, str) or not configured.strip():
            check.fail(f"{name} 的 objects.result_bucket 缺失或不是非空字符串")
            continue
        # 桶名不一致是真实配置错误:不随阶段降级,--phase pre 也必须是 FAIL。
        if bucket and configured != bucket:
            check.fail(
                f"{name} 的 result_bucket={configured} 与 env 的 {bucket} 不一致;"
                "worker 会按 env 桶名读取,而 gateway 写入另一个桶"
            )
    minio_block = compose_service_block(compose_text, "minio-init")
    if minio_block is None:
        check.fail("deploy/compose.yaml 缺少 minio-init;结果桶不会被创建")
    elif not wired(minio_block, "SPEECHNEXUS_EXECUTION_RESULT_BUCKET"):
        check.fail("minio-init 没有接收 SPEECHNEXUS_EXECUTION_RESULT_BUCKET;该桶不会被创建")
    worker_block = compose_service_block(compose_text, "job-dispatch-worker")
    if worker_block is not None and not wired(worker_block, "SPEECHNEXUS_EXECUTION_RESULT_BUCKET"):
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
    except (subprocess.TimeoutExpired, OSError):
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
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _effective(block: str) -> str:
    """去掉整行注释后再做结构匹配,避免注释里的字样被当成接线."""
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def check_network_lifecycle(
    env: dict[str, str], compose_text: str, offline: bool, phase: Phase
) -> Prereq:
    check = Prereq(6, "gateway 网络命名空间与启动顺序", phase=phase)
    worker_block = compose_service_block(compose_text, "job-dispatch-worker")
    if worker_block is None:
        check.fail("deploy/compose.yaml 缺少 job-dispatch-worker 服务")
        check.probe("运行时: SKIP — 服务定义缺失")
        return check
    worker_effective = _effective(worker_block)
    if not re.search(r'(?m)^\s+network_mode:\s*"?service:gateway"?\s*$', worker_effective):
        check.fail("job-dispatch-worker 没有声明 network_mode: service:gateway")
    if (
        "depends_on:" not in worker_effective
        or "condition: service_healthy" not in worker_effective
    ):
        check.fail("job-dispatch-worker 没有以 gateway healthy 作为依赖条件")
    if "restart: true" not in worker_effective:
        check.fail(
            "job-dispatch-worker 的 gateway 依赖没有 restart: true;重建 gateway 不会连带重建 worker"
        )
    gateway_block = compose_service_block(compose_text, "gateway")
    if gateway_block is None or "healthcheck:" not in _effective(gateway_block):
        check.fail("gateway 没有 healthcheck;worker 的 healthy 依赖无从满足")
    if "healthcheck:" in worker_effective:
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


def _has_generated_file(directory: Path) -> bool:
    """目录下至少有一个非隐藏文件(任意深度)。

    只建空骨架不算"已部署":`compose-minimal.md` 第 2 节先
    `mkdir -p deploy/.runtime/{private,gateway-locks,gateway-spool}`,这三个目录让
    `.runtime` "非空"却没有任何产物;按非空判定会让前置 4/5 的假 FAIL 在刚建目录时复活。
    生成私有配置、spool 或探针证据后才出现非隐藏文件。
    """
    if not directory.is_dir():
        return False
    try:
        for _root, _dirs, files in os.walk(directory):
            if any(name for name in files if not name.startswith(".")):
                return True
    except OSError:
        return False
    return False


def resolve_phase(
    requested: str,
    env: dict[str, str],
    compose_path: Path,
    compose_text: str,
    offline: bool,
) -> PhaseDecision:
    """决定按部署前还是部署后判定.

    `pre`/`post` 是显式覆盖;`auto` 只认客观信号:本项目已有容器、`deploy/.runtime`
    或私有配置目录下已生成文件。出现任一信号即按部署后判定——否则"已部署但私有配置缺失"
    会被误报成 PENDING。空骨架目录不算信号,见 `_has_generated_file`。
    """
    if requested in {"pre", "post"}:
        # mypy 不会把 `str` 收窄成 `Phase`;显式收窄,保证 PhaseDecision 的类型约束。
        explicit: Phase = "pre" if requested == "pre" else "post"
        return PhaseDecision(explicit, f"--phase {explicit}(显式覆盖)")
    signals: list[str] = []
    if not offline:
        rows = docker_containers(compose_project_name(compose_text))
        if rows:
            signals.append(f"Compose 项目已有 {len(rows)} 个容器")
    runtime_dir = compose_path.parent / ".runtime"
    if _has_generated_file(runtime_dir):
        signals.append(f"{runtime_dir} 下已生成文件")
    directory = private_dir(env, compose_path)
    if _has_generated_file(directory):
        signals.append(f"私有配置目录 {directory} 下已生成文件")
    if signals:
        return PhaseDecision("post", "auto:" + ";".join(signals))
    return PhaseDecision("pre", "auto:未发现容器,也没有已生成的 .runtime/私有配置(尚未部署)")


def build_checks(
    env: dict[str, str], compose_text: str, compose_path: Path, offline: bool, phase: Phase
) -> list[Prereq]:
    return [
        check_api_job_dsns(env, compose_text, offline, phase),
        check_migration(env, offline, phase),
        check_dispatch_role(env, compose_text, offline, phase),
        check_engine(env, compose_path, offline, phase),
        check_result_bucket(env, compose_text, compose_path, phase),
        check_network_lifecycle(env, compose_text, offline, phase),
    ]


def render(
    checks: list[Prereq], env_path: Path, compose_path: Path, decision: PhaseDecision
) -> None:
    print(f"async-job 前置自检: env={env_path} compose={compose_path}")
    print("只读检查:不改文件、不写数据库、不执行迁移、不重启容器")
    print(f"阶段: {decision.phase} — {decision.reason}")
    print()
    for check in checks:
        print(f"[{check.status}] 前置 {check.number}  {check.title}")
        for note in check.notes:
            print(f"        · {note}")
        for failure in check.failures:
            print(f"        ✗ {failure}")
        for pending_item in check.pending:
            print(f"        … {pending_item}")
        for runtime in check.runtime:
            print(f"        {runtime}")
        print()
    passed = sum(1 for check in checks if check.status == "PASS")
    failed_checks = [check for check in checks if check.status == "FAIL"]
    pending_checks = [check for check in checks if check.status == "PENDING"]
    print(
        f"结果: {passed}/{len(checks)} PASS;{len(pending_checks)} PENDING;{len(failed_checks)} FAIL"
    )
    if failed_checks:
        print(
            "FAIL(配置写了但不对,任何阶段都必须修):"
            + "、".join(f"前置 {check.number}" for check in failed_checks)
        )
    if pending_checks:
        print(
            "PENDING(部署后才有意义,尚未到该阶段):"
            + "、".join(f"前置 {check.number}" for check in pending_checks)
        )
    if failed_checks:
        print("下一步:按上方 ✗ 逐条修复 FAIL——这些项与部署阶段无关;修好后重跑本脚本。")
    if pending_checks:
        print(
            "下一步:完成部署准备(生成私有配置并 up)后重跑,之前 PENDING 的项应变成 PASS;"
            "已部署仍显示 PENDING 时用 --phase post 强制按部署后判定。"
        )
    if not failed_checks and not pending_checks:
        print("6 项前置的静态判定全部满足。")
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
        "-f",
        type=Path,
        default=REPO_ROOT / "deploy/compose.yaml",
        help="Compose 定义(默认 deploy/compose.yaml;短选项 -f 与 docker compose 一致)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="跳过 psql/HTTP/Docker 探测,只做静态判定",
    )
    parser.add_argument(
        "--phase",
        choices=("auto", "pre", "post"),
        default="auto",
        help="按部署前还是部署后判定(默认 auto:按 .runtime/私有配置是否已生成文件或容器是否存在)",
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
    decision = resolve_phase(args.phase, env, compose_path, compose_text, args.offline)
    checks = build_checks(env, compose_text, compose_path, args.offline, decision.phase)
    render(checks, env_path, compose_path, decision)
    return 0 if all(check.status != "FAIL" for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
