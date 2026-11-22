"""共同 SQL 子集的唯一定义点:禁止构造、`?` 参数与语句校验。

为什么只有这一份:同一个禁止清单如果既写在静态 guard 里、又写在运行时校验里,两份就会分叉,
其中一份迟早放行另一份拒绝的 SQL.静态扫描(`scripts/dev-env/check-portable-sql.py`)、
CI 用例(`tests/ci`)与运行时 DB-API 校验都从这里读 `FORBIDDEN` / `PARAMETER_MARKERS`.

本模块只依赖标准库,并且必须保持 Python 3.12 可导入:CI 的依赖自由守卫用 runner 自带解释器
运行,不会安装工作区依赖.

规则口径见 ADR 0016 与 `docs/architecture/database-portability.md` 第 4 节.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "FORBIDDEN",
    "PARAMETER_MARKERS",
    "Forbidden",
    "SubsetViolationError",
    "find_forbidden",
    "find_parameter_markers",
    "placeholder_count",
    "validate",
]


class SubsetViolationError(ValueError):
    """语句超出四库共同子集。"""

    def __init__(self, rules: list[str], sql: str) -> None:
        self.rules = tuple(rules)
        self.sql = sql
        super().__init__("SQL outside the portable subset: " + ", ".join(rules))


@dataclass(frozen=True, slots=True)
class Forbidden:
    """一条禁止构造:规则 id、匹配式与四库内的替代做法。"""

    rule: str
    pattern: re.Pattern[str]
    replacement: str


def _rule(rule: str, pattern: str, replacement: str) -> Forbidden:
    return Forbidden(rule, re.compile(pattern, re.IGNORECASE), replacement)


# 每条 pattern 只匹配 SQL 文本里确实只有部分品牌支持的写法。共同子集内的普通
# SELECT / INSERT / UPDATE / DELETE / JOIN / GROUP BY / ORDER BY / CASE / COALESCE
# 不在表内,因为它们四库都支持。
FORBIDDEN: tuple[Forbidden, ...] = (
    _rule("skip-locked", r"\bSKIP\s+LOCKED\b", "claim bucket + 单行 version CAS"),
    _rule("nowait", r"\bNOWAIT\b", "bucket 租约到期后重试"),
    _rule("upsert", r"\bON\s+CONFLICT\b", "先 INSERT,失败回滚后按业务唯一键查询"),
    _rule("upsert", r"\bON\s+DUPLICATE\s+KEY\b", "先 INSERT,失败回滚后按业务唯一键查询"),
    _rule("upsert", r"\bMERGE\s+INTO\b", "先 SELECT 唯一键,再单行 INSERT 或条件 UPDATE"),
    _rule("upsert", r"\bREPLACE\s+INTO\b", "先 SELECT 唯一键,再单行 INSERT 或条件 UPDATE"),
    _rule("returning", r"\bRETURNING\b", "应用生成 ULID,提交后按身份查询"),
    _rule("generated-identity", r"\bGENERATED\s+\w*\s*AS\s+IDENTITY\b", "应用生成 ULID"),
    _rule("generated-identity", r"\bIDENTITY\s*\(", "应用生成 ULID"),
    _rule("generated-identity", r"\bLAST_INSERT_ID\b", "应用生成 ULID"),
    _rule("generated-identity", r"\bNEXTVAL\s*\(", "应用生成 ULID"),
    _rule("serial-column", r"\b(?:BIG|SMALL)?SERIAL\b", "VARCHAR(128) + 应用 ULID"),
    _rule("serial-column", r"\bAUTO_INCREMENT\b", "VARCHAR(128) + 应用 ULID"),
    _rule("paging-limit", r"\bLIMIT\s+(?:\d|\?)", "keyset 条件 + fetchmany(page_size + 1)"),
    _rule("paging-limit", r"\bOFFSET\s+(?:\d|\?)", "keyset 条件 + fetchmany(page_size + 1)"),
    _rule("advisory-lock", r"\bPG_ADVISORY\w*", "单行 SELECT ... FOR UPDATE 或 bucket CAS"),
    _rule("advisory-lock", r"\bGET_LOCK\s*\(", "单行 SELECT ... FOR UPDATE 或 bucket CAS"),
    _rule("database-notify", r"\bLISTEN\b", "持久 outbox + 轮询"),
    _rule("database-notify", r"\bNOTIFY\b", "持久 outbox + 轮询"),
    _rule("cte", r"\bWITH\s+RECURSIVE\b", "预计算列或普通 join"),
    _rule("cte", r"\bWITH\s+[a-z_][a-z0-9_]*\s+AS\s*\(", "普通 join 或分步查询"),
    _rule("window-function", r"\bOVER\s*\(", "预计算列或单行 head"),
    _rule("aggregate-filter", r"\bFILTER\s*\(\s*WHERE\b", "CASE 内条件聚合"),
    _rule("distinct-on", r"\bDISTINCT\s+ON\b", "单行 head 或按唯一键读取"),
    _rule("vendor-json", r"\bJSONB?\b", "列式短字符串 + 对象存储"),
    _rule("vendor-json", r"->>?", "列式短字符串 + 对象存储"),
    _rule("vendor-json", r"#>>?", "列式短字符串 + 对象存储"),
    _rule("vendor-cast", r"::", "标准 CAST(... AS ...)"),
    _rule("vendor-type", r"\bTIMESTAMPTZ\b", "TIMESTAMP(6) + 应用 UTC"),
    _rule("vendor-type", r"\bWITH\s+TIME\s+ZONE\b", "TIMESTAMP(6) + 应用 UTC"),
    _rule("vendor-type", r"\bINTERVAL\b", "DECIMAL 或 TIMESTAMP 差值"),
    _rule("vendor-type", r"\b(?:TEXT|CLOB|BLOB|BYTEA)\b", "VARCHAR + 对象存储"),
    _rule("vendor-type", r"\bBOOLEAN\b", "CHAR(1) 的 Y/N"),
    _rule("vendor-type", r"\bUUID\b", "VARCHAR(128) + 应用 ULID"),
    _rule("vendor-type", r"\bARRAY\b", "列式短字符串"),
    _rule("vendor-type", r"\bENUM\s*\(", "VARCHAR(64)"),
    _rule("vendor-time", r"\bNOW\s*\(", "锁后独立短事务读 schema_meta 的 CURRENT_TIMESTAMP"),
    _rule(
        "vendor-time",
        r"\bCLOCK_TIMESTAMP\s*\(",
        "锁后独立短事务读 schema_meta 的 CURRENT_TIMESTAMP",
    ),
    _rule(
        "vendor-time",
        r"\bSTATEMENT_TIMESTAMP\s*\(",
        "锁后独立短事务读 schema_meta 的 CURRENT_TIMESTAMP",
    ),
    _rule(
        "vendor-time",
        r"\bTRANSACTION_TIMESTAMP\s*\(",
        "锁后独立短事务读 schema_meta 的 CURRENT_TIMESTAMP",
    ),
    _rule("vendor-time", r"\bSYSDATE\b", "锁后独立短事务读 schema_meta 的 CURRENT_TIMESTAMP"),
    _rule("vendor-time", r"\bSYSTIMESTAMP\b", "锁后独立短事务读 schema_meta 的 CURRENT_TIMESTAMP"),
    _rule("vendor-time", r"\bCURRENT_DATE\b", "应用传入 UTC datetime"),
    _rule("vendor-time", r"\bCURRENT_TIME\b", "应用传入 UTC datetime"),
    _rule("session-tuning", r"\bSTATEMENT_TIMEOUT\b", "应用层 deadline 与连接超时"),
    _rule("session-tuning", r"\bLOCK_TIMEOUT\b", "应用层 deadline 与连接超时"),
    _rule("session-tuning", r"\bSEARCH_PATH\b", "目标 schema 由 DSN / 角色默认 schema 决定"),
    _rule("session-tuning", r"\bSET\s+TRANSACTION\b", "显式 FOR UPDATE 与 rowcount CAS"),
    _rule("session-tuning", r"\bSET\s+TIME\s+ZONE\b", "实例与会话统一 UTC,由部署配置"),
    _rule(
        "runtime-ddl",
        r"\bCREATE\s+(?:TABLE|INDEX|UNIQUE|VIEW|FUNCTION|TRIGGER|PROCEDURE)\b",
        "部署期 SQL migration",
    ),
    _rule("runtime-ddl", r"\bALTER\s+TABLE\b", "部署期 SQL migration"),
    _rule(
        "runtime-ddl", r"\bDROP\s+(?:TABLE|INDEX|VIEW|FUNCTION|TRIGGER)\b", "部署期 SQL migration"
    ),
    _rule("vendor-function", r"\bEXTRACT\s*\(", "应用侧转换"),
    _rule("vendor-function", r"\bDATE_TRUNC\s*\(", "应用侧转换"),
    _rule("vendor-function", r"\bTO_CHAR\s*\(", "应用侧格式化"),
    _rule("vendor-function", r"\bSTRING_AGG\s*\(", "应用侧聚合或分步读取"),
    _rule("vendor-function", r"\bARRAY_AGG\s*\(", "应用侧聚合或分步读取"),
    _rule("vendor-function", r"\bGENERATE_SERIES\s*\(", "应用侧生成"),
    _rule("vendor-function", r"\bGEN_RANDOM_UUID\s*\(", "应用生成 ULID"),
    _rule("vendor-function", r"\bROW_NUMBER\s*\(", "预计算列"),
    _rule("vendor-operator", r"\bILIKE\b", "统一大小写后用 LIKE"),
    _rule("vendor-operator", r"=\s*ANY\s*\(", "固定小集合的逐个身份读取"),
    _rule("vendor-operator", r"\bFULL\s+(?:OUTER\s+)?JOIN\b", "两次 LEFT JOIN 或应用侧合并"),
    _rule("vendor-lock", r"\bFOR\s+SHARE\b", "单行 SELECT ... FOR UPDATE"),
)

# 参数风格:只允许 ODBC qmark。这里给的是「出现在 SQL 文本里就违规」的字面量。
PARAMETER_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pyformat", re.compile(r"%\([a-z_][a-z0-9_]*\)s", re.IGNORECASE)),
    ("format", re.compile(r"%s")),
    ("numeric", re.compile(r"\$\d")),
    ("named", re.compile(r"(?<!:):[A-Za-z_][A-Za-z0-9_]*")),
)


def _mask_literals(sql: str) -> str:
    """把引号内的内容替换成空格,长度与位置不变.

    数据和标识符里的 `->`、`%s`、`::` 不是 SQL 构造,不能算违规;换成空格后正则的
    匹配位置仍对应原文本,便于报行号与列位置.
    """

    masked: list[str] = []
    quote = ""
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote:
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    masked.append(" ")
                    masked.append(" ")
                    index += 2
                    continue
                quote = ""
                masked.append(character)
            else:
                masked.append(" ")
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            masked.append(character)
            index += 1
            continue
        masked.append(character)
        index += 1
    return "".join(masked)


def find_forbidden(sql: str) -> list[str]:
    """返回命中的规则 id,按 `FORBIDDEN` 顺序去重。"""

    masked = _mask_literals(sql)
    rules: list[str] = []
    for entry in FORBIDDEN:
        if entry.rule in rules:
            continue
        if entry.pattern.search(masked):
            rules.append(entry.rule)
    return rules


def find_parameter_markers(sql: str) -> list[str]:
    """返回命中的非 qmark 参数风格名。"""

    masked = _mask_literals(sql)
    markers: list[str] = []
    for name, pattern in PARAMETER_MARKERS:
        if name in markers:
            continue
        if pattern.search(masked):
            markers.append(name)
    return markers


def placeholder_count(sql: str) -> int:
    """统计引号外的 `?` 个数,用于校验绑定参数数量。"""

    return _mask_literals(sql).count("?")


def validate(sql: str) -> None:
    """语句或参数风格超出共同子集时抛 `SubsetViolationError`。"""

    rules = find_forbidden(sql) + find_parameter_markers(sql)
    if rules:
        raise SubsetViolationError(rules, sql)
