"""canonical statement manifest:statement contract、受限 profile variant 与摘要.

statement manifest 是「业务默认路径」的机器可读定义:statement ID、语义版本、参数形状、
结果列与 NULL 形状、事务边界、锁序、幂等与错误语义。业务代码只按 statement ID 使用它,
不按数据库品牌选择。

目标库无法执行同一 SQL 文本时,可以在 `infrastructure` 内登记静态 profile variant;variant
只提供等价 SQL,**不能**改变 statement 的参数个数、结果列、NULL 形状、事务、锁序或幂等语义。
`digest()` 只覆盖 canonical statement(不含 variant),所以「同一 manifest 摘要」就是
「同一业务契约」的判据;variant 增删不会掩盖契约变化。

选择性校验(`?` 个数、子集、profile 已登记、摘要一致)由
`scripts/dev-env/check-statement-manifest.py` 在 CI 执行;本模块只提供定义与解析。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "COLUMN_KINDS",
    "TRANSACTION_BOUNDARIES",
    "Manifest",
    "ManifestError",
    "ProfileVariant",
    "Statement",
    "canonical_statements_json",
    "digest",
    "load_manifest",
]

#: 结果与参数的稳定类型标签。运行时的值类型由 DB-API 契约统一。
COLUMN_KINDS = frozenset({"text", "char", "decimal", "timestamp"})

#: 事务边界:只读、写(显式提交)、以及以 `rowcount` 判定的条件写。
TRANSACTION_BOUNDARIES = frozenset({"read_only", "write", "cas"})


class ManifestError(ValueError):
    """manifest 或 profile 登记不合法。"""


@dataclass(frozen=True, slots=True)
class Statement:
    statement_id: str
    version: int
    params: tuple[tuple[str, str, bool], ...]
    result: tuple[tuple[str, str, bool], ...]
    transaction: str
    lock_order: tuple[str, ...]
    idempotency: str
    error_semantics: str
    sql: str


@dataclass(frozen=True, slots=True)
class ProfileVariant:
    profile_id: str
    statement_id: str
    sql: str
    rationale: str
    evidence: str


@dataclass(frozen=True, slots=True)
class Manifest:
    revision: str
    statements: tuple[Statement, ...]
    profiles: tuple[ProfileVariant, ...]

    def statement(self, statement_id: str) -> Statement | None:
        for statement in self.statements:
            if statement.statement_id == statement_id:
                return statement
        return None

    def variants(self, statement_id: str) -> tuple[ProfileVariant, ...]:
        return tuple(variant for variant in self.profiles if variant.statement_id == statement_id)


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _columns(value: object, *, field: str) -> tuple[tuple[str, str, bool], ...]:
    if not isinstance(value, list):
        raise ManifestError(f"{field} must be a list")
    columns: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ManifestError(f"{field} entries must be objects")
        name = _require_str(raw.get("name"), field=f"{field} name")
        kind = _require_str(raw.get("kind"), field=f"{field}.{name} kind")
        if kind not in COLUMN_KINDS:
            raise ManifestError(f"{field}.{name} has unknown kind {kind!r}")
        nullable = raw.get("nullable")
        if not isinstance(nullable, bool):
            raise ManifestError(f"{field}.{name} needs an explicit nullable flag")
        if name in seen:
            raise ManifestError(f"{field} repeats {name}")
        seen.add(name)
        columns.append((name, kind, nullable))
    return tuple(columns)


def _statement(raw: object) -> Statement:
    if not isinstance(raw, dict):
        raise ManifestError("statement must be an object")
    statement_id = _require_str(raw.get("id"), field="statement id")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ManifestError(f"{statement_id}.version must be a positive integer")
    transaction = _require_str(raw.get("transaction"), field=f"{statement_id}.transaction")
    if transaction not in TRANSACTION_BOUNDARIES:
        raise ManifestError(f"{statement_id} has unknown transaction boundary {transaction!r}")
    lock_order = raw.get("lock_order", [])
    if not isinstance(lock_order, list) or not all(isinstance(item, str) for item in lock_order):
        raise ManifestError(f"{statement_id}.lock_order must be a list of table names")
    return Statement(
        statement_id=statement_id,
        version=version,
        params=_columns(raw.get("params", []), field=f"{statement_id}.params"),
        result=_columns(raw.get("result", []), field=f"{statement_id}.result"),
        transaction=transaction,
        lock_order=tuple(lock_order),
        idempotency=_require_str(raw.get("idempotency"), field=f"{statement_id}.idempotency"),
        error_semantics=_require_str(
            raw.get("error_semantics"), field=f"{statement_id}.error_semantics"
        ),
        sql=_require_str(raw.get("sql"), field=f"{statement_id}.sql"),
    )


def _variant(raw: object) -> ProfileVariant:
    if not isinstance(raw, dict):
        raise ManifestError("profile variant must be an object")
    return ProfileVariant(
        profile_id=_require_str(raw.get("profile_id"), field="variant.profile_id"),
        statement_id=_require_str(raw.get("statement_id"), field="variant.statement_id"),
        sql=_require_str(raw.get("sql"), field="variant.sql"),
        rationale=_require_str(raw.get("rationale"), field="variant.rationale"),
        evidence=_require_str(raw.get("evidence"), field="variant.evidence"),
    )


def load_manifest(path: Path) -> Manifest:
    """读取并做结构校验;跨文件的一致性由 CI guard 检查。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ManifestError("manifest must be a JSON object")
    revision = _require_str(payload.get("revision"), field="revision")
    raw_statements = payload.get("statements")
    if not isinstance(raw_statements, list) or not raw_statements:
        raise ManifestError("manifest needs at least one statement")
    statements = tuple(_statement(raw) for raw in raw_statements)
    identifiers = [statement.statement_id for statement in statements]
    if len(set(identifiers)) != len(identifiers):
        raise ManifestError("statement ids must be unique")
    raw_profiles = payload.get("profiles", [])
    if not isinstance(raw_profiles, list):
        raise ManifestError("profiles must be a list")
    return Manifest(revision, statements, tuple(_variant(raw) for raw in raw_profiles))


def canonical_statements_json(manifest: Manifest) -> str:
    """摘要输入:只覆盖 canonical statement,显式排除 profile variant。"""

    payload = {
        "revision": manifest.revision,
        "statements": [
            {
                "id": statement.statement_id,
                "version": statement.version,
                "params": [
                    {"name": name, "kind": kind, "nullable": nullable}
                    for name, kind, nullable in statement.params
                ],
                "result": [
                    {"name": name, "kind": kind, "nullable": nullable}
                    for name, kind, nullable in statement.result
                ],
                "transaction": statement.transaction,
                "lock_order": list(statement.lock_order),
                "idempotency": statement.idempotency,
                "error_semantics": statement.error_semantics,
                "sql": statement.sql,
            }
            for statement in manifest.statements
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(manifest: Manifest) -> str:
    """canonical statement manifest 的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(canonical_statements_json(manifest).encode("utf-8")).hexdigest()


def load_profile_registry(path: Path) -> dict[str, Any]:
    """已登记 profile 的唯一来源:`profile_id -> {family, mode, targets, evidence}`."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ManifestError("profile registry must be a JSON object")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ManifestError("profile registry needs a profiles object")
    return {str(key): value for key, value in profiles.items()}
