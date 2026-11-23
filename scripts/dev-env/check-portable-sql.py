#!/usr/bin/env python3
"""单一可移植 SQL 数据路径的静态 guard:品牌 SDK、品牌 SQL、非 `?` 参数与品牌分支。

为什么需要它:ADR 0016 要求「同一应用、同一 SQL、同一 schema,运行时只替换 DSN 与 ODBC 驱动」。
这条要求只有在能被机械拒绝时才有意义——否则下一次改动会把 `SKIP LOCKED`、`%s` 或
`psycopg.errors.UniqueViolation` 悄悄带回来,而四库资格要等到很晚才暴露它。

判据来源是唯一一份定义:`packages/portable-sql/src/speechnexus_portable_sql/subset.py`。
本脚本不重复写禁止清单,只负责把产品源码里的 SQL 文本喂给同一份清单,再补两类只有静态
分析才能看到的问题:

  vendor-sdk          产品代码导入厂商数据库 SDK(psycopg、SQLAlchemy、pymysql 等)
  vendor-error-branch 业务读 SQLSTATE / pgcode / errorcode 做分支
  brand-branch        按 `database` / `dialect` / `driver` 变量比较数据库品牌
  sql-string-building 用 f-string 或拼接构造 SQL 文本
  dynamic-placeholder-list 按批次长度乘出占位符列表(`["?"] * len(rows)`),元数在运行期才定
  <subset 规则 id>    SQL 文本命中共同子集之外的构造(SKIP LOCKED、RETURNING、LIMIT、jsonb…)

棘轮:现有实现是迁移输入,不是目标。`--baseline` 只对**基线之外**的问题变红;基线内的
条目只能减少。新增条目必须由 `--write-baseline` 显式写入,那次改动在 PR diff 里可见,
不是静默放行。

只读:除 `--write-baseline` 外不改任何文件。

用法:
  scripts/dev-env/check-portable-sql.py --baseline scripts/dev-env/portable-sql-baseline.json
  scripts/dev-env/check-portable-sql.py --write-baseline scripts/dev-env/portable-sql-baseline.json
  scripts/dev-env/check-portable-sql.py --selftest
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUBSET_SOURCE = REPO_ROOT / "packages/portable-sql/src"

# 规则只有一份定义;静态扫描与运行时校验都从这里读。
if str(SUBSET_SOURCE) not in sys.path:
    sys.path.insert(0, str(SUBSET_SOURCE))

from speechnexus_portable_sql.subset import (  # noqa: E402 - 先补 sys.path 才能导入
    find_forbidden,
    find_parameter_markers,
)

#: 扫描的产品源码根。测试、脚本与生成产物不在此列。
PRODUCT_ROOTS = (
    "asset-inspector/src",
    "backend/src",
    "execution-gateway/src",
    "node-controller/src",
    "packages/contracts/src",
    "packages/media/src",
)

#: 唯一豁免:规则本身的定义点。它必须豁免,否则禁止清单会命中自己的正则字面量。
#: 唯一允许 import 厂商数据库 SDK 的地方是**驱动实现**:ADR 0016 允许部署期 driver / DSN /
#: profile 选择。业务层导入 psycopg 仍逐条基线化,直到 #1100 收敛完。
DRIVER_PREFIXES = ("packages/portable-sql/src/speechnexus_portable_sql/psycopg_driver.py",)
#: 时钟语句按 profile 登记:各家「现在」的写法不同(`clock_timestamp()` / 等价函数),
#: 这里是那份登记表的唯一载体,和驱动一样属于部署期 profile 选择。
CLOCK_PREFIXES = ("packages/portable-sql/src/speechnexus_portable_sql/clock.py",)
EXCLUDED_PREFIXES = (
    "packages/portable-sql/src/speechnexus_portable_sql/subset.py",
    *DRIVER_PREFIXES,
    *CLOCK_PREFIXES,
)

#: 整个 alembic 迁移树是**迁移输入**,不是产品运行时 SQL:版本文件按 PostgreSQL 方言写 DDL,
#: `env.py` 是迁移工具的入口(用 SQLAlchemy 的命名绑定、迁移互斥锁与 `search_path` 设置)。
#: 受限方言约束的是应用运行时的 DML,所以这里只统计版本文件数量,不逐条进门禁——
#: 把一份由部署期执行的方言 SQL 逐条基线化,只会让真正的运行时违规淹没在噪音里。
MIGRATION_INPUT_PREFIXES = ("backend/src/speechnexus_backend/speech/infrastructure/migrations/",)

#: 迁移版本文件(按文件名排序执行的那一批),只用于统计 `migration_input`。
MIGRATION_VERSION_PREFIX = (
    "backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/"
)

DDL_GLOBS = ("migrations/*.sql",)

VENDOR_SDKS = (
    "psycopg",
    "psycopg2",
    "psycopg_pool",
    "sqlalchemy",
    "pymysql",
    "MySQLdb",
    "mysql",
    "cx_Oracle",
    "oracledb",
    "asyncpg",
    "aiomysql",
    "pygresql",
)

ERROR_ATTRIBUTES = frozenset({"sqlstate", "pgcode", "pgerror", "errorcode", "errno", "diag"})

BRAND_VARIABLES = frozenset({"database", "dialect", "driver", "db_vendor", "db_kind", "backend"})

#: 品牌字面量不得出现在领域 / 应用层:profile 选择属于部署,不是业务分支。
BRAND_LITERALS = frozenset(
    {"postgresql", "postgres", "mysql", "mariadb", "oracle", "oceanbase", "sqlite"}
)
LAYER_LIMITS = ("/application/", "/domain/")

#: 已登记 profile 的 id 只允许出现在 infrastructure 与部署配置里。
PROFILE_REGISTRY = REPO_ROOT / "packages/portable-sql/statements/profiles.json"

SQL_SHAPE = re.compile(
    r"\b(?:SELECT\b|INSERT\s+INTO\b|UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b"
    r"|CREATE\s+(?:TABLE|INDEX|UNIQUE|VIEW)\b|ALTER\s+TABLE\b|DROP\s+TABLE\b)",
    re.IGNORECASE,
)

#: 单个占位符字面量。乘上运行期长度就是「元数由批次决定」的拼接。
PLACEHOLDER_LITERALS = frozenset({"?", "%s"})

#: 语句起始关键字。`sql-string-building` 只看「文本里出现 SELECT」会把提示语也算进来
#: (例如 `" no longer select a local inspection process; "`),所以判据要求字面量**以**一条
#: 语句的关键字开头 —— 这也是 f-string 拼接 SQL 的真实形状。
SQL_STATEMENT_START = re.compile(
    r"\A\s*\(?\s*(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WITH|CREATE|ALTER|DROP"
    r"|REVOKE|GRANT|TRUNCATE|COMMENT\s+ON|MERGE\s+INTO|SET\s|SAVEPOINT|ROLLBACK|RELEASE)\b",
    re.IGNORECASE,
)

DDL_FORBIDDEN_TYPES = (
    ("ddl-type", re.compile(r"\b(?:TEXT|CLOB|BLOB|BYTEA|LONGTEXT|MEDIUMTEXT)\b", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\b(?:JSON|JSONB)\b", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\b(?:BOOLEAN|BOOL)\b", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\bUUID\b", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\b(?:BIGSERIAL|SERIAL|SMALLSERIAL)\b", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\b(?:BIGINT|SMALLINT|INTEGER|INT|TINYINT)\b", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\b(?:DOUBLE|FLOAT|REAL|NUMERIC)\b", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\b(?:TIMESTAMPTZ|DATE|TIME)\b(?!STAMP)", re.IGNORECASE)),
    ("ddl-type", re.compile(r"\bWITH\s+TIME\s+ZONE\b", re.IGNORECASE)),
    ("ddl-default", re.compile(r"\bDEFAULT\b", re.IGNORECASE)),
    ("ddl-identity", re.compile(r"\b(?:IDENTITY|AUTO_INCREMENT|GENERATED)\b", re.IGNORECASE)),
    (
        "ddl-object",
        re.compile(
            r"\bCREATE\s+(?:FUNCTION|TRIGGER|PROCEDURE|EXTENSION|TYPE|SEQUENCE)\b", re.IGNORECASE
        ),
    ),
    ("ddl-object", re.compile(r"\bON\s+DELETE\s+CASCADE\b", re.IGNORECASE)),
)

IDENTIFIER = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
DDL_NAME = re.compile(
    r"\b(?:CREATE\s+(?:TABLE|INDEX|UNIQUE\s+INDEX|VIEW)\s+)([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class Finding:
    """一条违规:规则 id 与相对仓库根的路径。"""

    rule: str
    path: str


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集模块 / 类 / 函数 docstring 的常量节点 id;说明文字不是 SQL。"""

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    return docstrings


def _imported_modules(node: ast.AST) -> Iterable[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif isinstance(node, ast.ImportFrom) and node.module is not None:
        yield node.module


def _is_vendor_sdk(module: str) -> bool:
    return module.split(".")[0] in VENDOR_SDKS


def _profile_ids() -> frozenset[str]:
    try:
        payload = json.loads(PROFILE_REGISTRY.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return frozenset()
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        return frozenset()
    return frozenset(str(key) for key in profiles)


def scan_python(path: str, source: str) -> list[Finding]:
    """扫描一个 Python 源文件。"""

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return [Finding("parse-error", path)]

    findings: list[Finding] = []
    docstrings = _docstring_nodes(tree)
    profiles = _profile_ids()

    for node in ast.walk(tree):
        for module in _imported_modules(node):
            if _is_vendor_sdk(module):
                findings.append(Finding("vendor-sdk", path))

        if isinstance(node, ast.Attribute) and node.attr in ERROR_ATTRIBUTES:
            findings.append(Finding("vendor-error-branch", path))
        if isinstance(node, ast.Name) and node.id in ERROR_ATTRIBUTES:
            findings.append(Finding("vendor-error-branch", path))

        if isinstance(node, ast.Compare):
            operands: list[ast.expr] = [node.left, *node.comparators]
            if any(
                isinstance(operand, ast.Name) and operand.id in BRAND_VARIABLES
                for operand in operands
            ):
                findings.append(Finding("brand-branch", path))

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            # `["?"] * len(rows)` 把占位符元数交给运行期;受限契约要求定长列表走
            # `ListMarker`,让驱动层展开,而不是在业务侧拼长度。
            for factor in (node.left, node.right):
                if (
                    isinstance(factor, ast.List)
                    and len(factor.elts) == 1
                    and isinstance(factor.elts[0], ast.Constant)
                    and factor.elts[0].value in PLACEHOLDER_LITERALS
                ):
                    findings.append(Finding("dynamic-placeholder-list", path))
                    break

        if isinstance(node, ast.JoinedStr):
            literal = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if SQL_SHAPE.search(literal) and SQL_STATEMENT_START.search(literal):
                findings.append(Finding("sql-string-building", path))

        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            text = node.value
            lowered = text.strip().lower()
            if lowered in BRAND_LITERALS and any(marker in path for marker in LAYER_LIMITS):
                findings.append(Finding("brand-literal", path))
            if lowered in profiles and "/infrastructure/" not in path:
                findings.append(Finding("profile-identity", path))
            if SQL_SHAPE.search(text):
                for rule in find_forbidden(text) + find_parameter_markers(text):
                    findings.append(Finding(rule, path))

    return findings


STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _strip_sql_comments(source: str) -> str:
    return re.sub(r"--[^\n]*", "", source)


def scan_ddl(path: str, source: str) -> list[Finding]:
    """扫描一个 DDL 文件:类型、对象、默认值、标识符长度与字符集。"""

    findings: list[Finding] = []
    text = _strip_sql_comments(source)
    # 字符串字面量是数据,不是标识符:种子里的逻辑指纹不能被当成超长标识符。
    identifiers = STRING_LITERAL.sub("''", text)

    for rule, pattern in DDL_FORBIDDEN_TYPES:
        if pattern.search(text):
            findings.append(Finding(rule, path))

    for match in DDL_NAME.finditer(text):
        name = match.group(1)
        if len(name) > 30:
            findings.append(Finding("ddl-identifier-length", path))
        if not name.isascii():
            findings.append(Finding("ddl-identifier-charset", path))

    for match in IDENTIFIER.finditer(identifiers):
        name = match.group(1)
        if len(name) > 30:
            findings.append(Finding("ddl-identifier-length", path))

    return findings


def _iter_files(root: Path, relative: str) -> Iterable[Path]:
    base = root / relative
    if base.is_file():
        yield base
        return
    if base.is_dir():
        yield from sorted(base.rglob("*.py"))


def scan_tree(root: Path) -> list[Finding]:
    """扫描固定产品根与 DDL 目录。"""

    findings: list[Finding] = []

    for relative in PRODUCT_ROOTS:
        for path in _iter_files(root, relative):
            key = path.relative_to(root).as_posix()
            if key.startswith(EXCLUDED_PREFIXES) or key.startswith(MIGRATION_INPUT_PREFIXES):
                continue
            findings.extend(scan_python(key, path.read_text(encoding="utf-8")))

    for pattern in DDL_GLOBS:
        for path in sorted(root.glob(pattern)):
            key = path.relative_to(root).as_posix()
            findings.extend(scan_ddl(key, path.read_text(encoding="utf-8")))

    return findings


def _counts(findings: Sequence[Finding]) -> Counter[str]:
    return Counter(f"{finding.rule}::{finding.path}" for finding in findings)


def load_baseline(path: Path) -> Counter[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("findings")
    if not isinstance(entries, dict):
        raise ValueError("baseline must contain a 'findings' object")
    return Counter({str(key): int(value) for key, value in entries.items()})


def load_exceptions(path: Path) -> dict[str, dict[str, str]]:
    """读取「已归类未覆盖项」登记表:键是 `rule::path`,值是归属卡与解除条件."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit(f"{path}: expected an object with an 'entries' array")
    table: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: every entry must be an object")
        missing = [field for field in ("rule", "path", "card", "reason") if not entry.get(field)]
        if missing:
            raise SystemExit(f"{path}: entry is missing {', '.join(missing)}")
        key = f"{entry['rule']}::{entry['path']}"
        if key in table:
            raise SystemExit(f"{path}: duplicate entry for {key}")
        table[key] = entry
    return table


def audit_exceptions(
    counts: Counter[str], exceptions: dict[str, dict[str, str]]
) -> tuple[list[str], list[str]]:
    """比较基线与登记表:未登记的条目、以及已经不再存在的登记."""

    unregistered = [key for key in counts if key not in exceptions]
    stale = [key for key in exceptions if key not in counts]
    return sorted(unregistered), sorted(stale)


#: 声明文件里的分类字段。两类制品的判定依据不同,所以分开列,而不是混在一个 `paths` 里:
#: - `paths`:唯一消费者是 alembic 迁移树;
#: - `deploy`:部署期特权制品(GRANT/REVOKE),标识符无法参数化,由 `deploy/cluster/` 的
#:   供给脚本与集成用例消费,任何 DML 路径都不执行它。
DECLARED_PROFILE_FIELDS = ("paths", "deploy")


def load_profile_patterns(path: Path) -> tuple[str, ...]:
    """读取已声明的 profile 制品路径模式.

    架构文档允许「受控的物理 DDL profile」与「已登记的静态 SQL profile variant」:这些是
    部署期品牌相关的制品,不是业务 DML。守卫把它们单独计数,不和业务 DML 混在同一条基线里,
    但**必须逐条声明**——没声明的文件照旧按受限方言判红。
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    collected: list[str] = []
    for field in DECLARED_PROFILE_FIELDS:
        patterns = payload.get(field)
        if patterns is None:
            continue
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise SystemExit(f"{path}: '{field}' must be a string array")
        collected.extend(patterns)
    if not collected:
        raise SystemExit(f"{path}: expected at least one of {DECLARED_PROFILE_FIELDS}")
    return tuple(collected)


def split_declared(
    counts: Counter[str], patterns: Sequence[str]
) -> tuple[Counter[str], Counter[str]]:
    """按声明把问题拆成「profile 制品」与「业务 DML」两份计数."""

    declared: Counter[str] = Counter()
    remaining: Counter[str] = Counter()
    for key, count in counts.items():
        _, path = _describe(key)
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
            declared[key] += count
        else:
            remaining[key] += count
    return remaining, declared


def baseline_payload(counts: Counter[str]) -> dict[str, object]:
    return {
        "version": 1,
        "note": "由 scripts/dev-env/check-portable-sql.py --write-baseline 生成;条目只能减少。",
        "findings": dict(sorted(counts.items())),
    }


def _describe(key: str) -> tuple[str, str]:
    rule, _, path = key.partition("::")
    return rule, path


def _selftest_exception_audit() -> list[str]:
    """登记表的两半都要会红:漏登记与登记了已不存在的条目."""

    problems: list[str] = []
    counts = Counter({"cte::backend/src/a.py": 2, "upsert::backend/src/b.py": 1})
    complete = {
        "cte::backend/src/a.py": {"card": "#1100", "reason": "拆派生表"},
        "upsert::backend/src/b.py": {"card": "#1100", "reason": "保存点"},
    }
    unregistered, stale = audit_exceptions(counts, complete)
    if unregistered or stale:
        problems.append("a complete exception ledger was rejected")
    missing = dict(complete)
    missing.pop("cte::backend/src/a.py")
    unregistered, stale = audit_exceptions(counts, missing)
    if unregistered != ["cte::backend/src/a.py"] or stale:
        problems.append("an unregistered baseline entry was not detected")
    extra = dict(complete)
    extra["cte::backend/src/gone.py"] = {"card": "#1100", "reason": "x"}
    unregistered, stale = audit_exceptions(counts, extra)
    if unregistered or stale != ["cte::backend/src/gone.py"]:
        problems.append("a stale exception entry was not detected")
    return problems


def selftest() -> list[str]:
    """注入式自检:每条规则都必须能红,干净样本必须绿。"""

    problems: list[str] = _selftest_exception_audit()

    clean = "SELECT job_id FROM coordination_jobs WHERE job_id = ?"
    if scan_python("backend/src/clean.py", f'SQL = "{clean}"' + chr(10)):
        problems.append("clean statement was rejected")

    # 提示语里出现 `select` 不是 SQL:f-string 必须以语句关键字开头才算拼接 SQL。
    prose = 'MESSAGE = f" no longer select a local process; select audio instead"'
    if any(
        finding.rule == "sql-string-building"
        for finding in scan_python("backend/src/prose.py", prose)
    ):
        problems.append("a prose f-string that merely mentions select was treated as SQL")

    cases: tuple[tuple[str, str, str], ...] = (
        ("vendor-sdk", "backend/src/bad.py", "import psycopg\n"),
        ("vendor-error-branch", "backend/src/bad.py", "code = error.sqlstate\n"),
        ("brand-branch", "backend/src/bad.py", "if database == 'mysql':\n    pass\n"),
        (
            "sql-string-building",
            "backend/src/bad.py",
            'sql = f"SELECT job_id FROM t WHERE job_id = {{value}}"',
        ),
        (
            "dynamic-placeholder-list",
            "backend/src/bad.py",
            'marks = ", ".join(["?"] * len(rows))',
        ),
        (
            "skip-locked",
            "backend/src/bad.py",
            'SQL = "SELECT job_id FROM t FOR UPDATE SKIP LOCKED"\n',
        ),
        ("returning", "backend/src/bad.py", 'SQL = "INSERT INTO t (a) VALUES (?) RETURNING a"\n'),
        ("paging-limit", "backend/src/bad.py", 'SQL = "SELECT job_id FROM t LIMIT 5"\n'),
        ("format", "backend/src/bad.py", 'SQL = "SELECT job_id FROM t WHERE job_id = %s"\n'),
        (
            "vendor-time",
            "backend/src/bad.py",
            'SQL = "SELECT job_id FROM t WHERE due_at < now()"\n',
        ),
        (
            "brand-literal",
            "backend/src/speechnexus_backend/speech/domain/bad.py",
            'BRAND = "postgresql"\n',
        ),
        (
            "profile-identity",
            "backend/src/speechnexus_backend/speech/application/bad.py",
            'PROFILE = "postgresql-16"\n',
        ),
    )
    for rule, path, source in cases:
        found = {finding.rule for finding in scan_python(path, source)}
        if rule not in found:
            problems.append(f"{rule} did not fire for its injection")

    ddl_cases: tuple[tuple[str, str], ...] = (
        ("ddl-type", "CREATE TABLE t (a TEXT);\n"),
        ("ddl-type", "CREATE TABLE t (a BIGINT);\n"),
        ("ddl-default", "CREATE TABLE t (a VARCHAR(8) DEFAULT 'x');\n"),
        ("ddl-object", "CREATE TRIGGER trg AFTER INSERT ON t BEGIN END;\n"),
        (
            "ddl-identifier-length",
            "CREATE TABLE a_very_long_table_name_over_thirty_chars (a VARCHAR(8));\n",
        ),
    )
    for rule, source in ddl_cases:
        found = {finding.rule for finding in scan_ddl("migrations/0002_bad.sql", source)}
        if rule not in found:
            problems.append(f"{rule} did not fire for its DDL injection")

    sample_fingerprint = "d99bfdf39af69477376fbe5ef2f96ac752a82f2b03a3fe4c745e2e961fda4ed6"
    driver = "packages/portable-sql/src/speechnexus_portable_sql/psycopg_driver.py"
    if not any(driver.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        problems.append("the driver adapter is not exempt from the vendor SDK rule")
    elif "import psycopg" not in (REPO_ROOT / driver).read_text(encoding="utf-8"):
        problems.append("the driver exemption is not load-bearing")
    if any("backend/src/" in prefix for prefix in EXCLUDED_PREFIXES):
        problems.append("a business module is wrongly exempt from the vendor SDK rule")

    clean_ddl = (
        "CREATE TABLE schema_meta (meta_id VARCHAR(32) NOT NULL, schema_rev VARCHAR(64) NOT NULL,\n"
        "    PRIMARY KEY (meta_id));\n"
        "INSERT INTO schema_meta (meta_id, schema_rev, schema_fingerprint)\n"
        f"    VALUES ('portable', '0005', '{sample_fingerprint}');\n"
    )
    declared_sample = Counter(
        {
            "sql-string-building::backend/src/"
            "speechnexus_backend/speech/infrastructure/x_schema.py": 2,
            "paging-limit::backend/src/speechnexus_backend/speech/infrastructure/app.py": 1,
        }
    )
    remaining, declared = split_declared(declared_sample, ("backend/src/**/*_schema.py",))
    if sum(declared.values()) != 2:
        problems.append("a declared profile artifact was not split out")
    if sum(remaining.values()) != 1:
        problems.append("an undeclared module was wrongly treated as a profile artifact")

    if scan_ddl("migrations/0002_clean.sql", clean_ddl):
        problems.append("clean DDL was rejected")

    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="被扫描的仓库根")
    parser.add_argument("--baseline", type=Path, help="只对基线之外的问题变红")
    parser.add_argument("--write-baseline", type=Path, help="写入当前问题集合")
    parser.add_argument(
        "--accept-new",
        action="store_true",
        help="与 --write-baseline 一起使用时显式接受基线外的新问题",
    )
    parser.add_argument("--selftest", action="store_true", help="注入式自检规则是否仍会红")
    parser.add_argument(
        "--exceptions",
        type=Path,
        help="已归类未覆盖项登记表(JSON);基线里没有登记的条目判红",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        help="已声明的物理 DDL / 静态 SQL profile 制品清单(JSON),其条目单独计数",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        problems = selftest()
        for problem in problems:
            print(f"SELFTEST FAIL: {problem}", file=sys.stderr)
        print("selftest OK: every rule fires on its injection" if not problems else "SELFTEST FAIL")
        return 1 if problems else 0

    counts = _counts(scan_tree(args.root))
    profile_counts: Counter[str] = Counter()
    if args.profiles is not None:
        counts, profile_counts = split_declared(counts, load_profile_patterns(args.profiles))

    baseline: Counter[str] = Counter()
    if args.baseline is not None:
        baseline = load_baseline(args.baseline)

    if args.exceptions is not None:
        exceptions = load_exceptions(args.exceptions)
        unregistered, stale = audit_exceptions(counts, exceptions)
        for key in unregistered:
            rule, path = _describe(key)
            print(f"UNREGISTERED {rule} {path}")
        for key in stale:
            rule, path = _describe(key)
            print(f"STALE-EXCEPTION {rule} {path}")
        if unregistered:
            print("RESULT FAIL: the baseline carries entries with no owning card")
            return 1
        if stale:
            print("RESULT FAIL: the exception ledger lists findings that no longer exist")
            return 1
        print(f"exceptions={len(exceptions)} cards={len({e['card'] for e in exceptions.values()})}")

    new = counts - baseline
    fixed = baseline - counts

    for key, count in sorted(new.items()):
        rule, path = _describe(key)
        print(f"NEW {rule} {path} ({count})")
    for key, count in sorted(fixed.items()):
        rule, path = _describe(key)
        print(f"FIXED {rule} {path} ({count})")

    if args.write_baseline is not None:
        if new and not args.accept_new:
            print(
                "refusing to write a baseline that accepts new findings; "
                "review them and rerun with --accept-new",
                file=sys.stderr,
            )
            return 1
        args.write_baseline.write_text(
            json.dumps(baseline_payload(counts), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.write_baseline}")
        return 0

    migration_input = (
        sum(1 for path in (args.root / MIGRATION_VERSION_PREFIX).glob("*.py"))
        if (args.root / MIGRATION_VERSION_PREFIX).is_dir()
        else 0
    )
    total = sum(counts.values())
    print(
        f"findings={total} rules={len({_describe(key)[0] for key in counts})} "
        f"files={len({_describe(key)[1] for key in counts})} migration_input={migration_input}"
    )
    if args.baseline is not None:
        print(
            f"baseline={sum(baseline.values())} new={sum(new.values())} fixed={sum(fixed.values())}"
        )
    if args.profiles is not None:
        print(
            f"profile-ddl={sum(profile_counts.values())} "
            f"files={len({_describe(key)[1] for key in profile_counts})}"
        )
    if new:
        print("RESULT FAIL: portable SQL subset regressed")
        return 1
    print("RESULT OK: no new findings outside the portable SQL subset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
