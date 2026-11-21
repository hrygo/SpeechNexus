"""值边界:UTF-8 字节长度、禁止空串、`Decimal`、UTC 时间与应用生成身份。

这些规则在 ADR 0016 第 5-6 条里各只定义一次,四库差异(Oracle 把空串当 NULL、MySQL 按字符
计长度、PostgreSQL 按字节)因此收敛到同一个更严格的入口校验,而不是散落在各表写入处。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from speechnexus_portable_sql.contract import ContractError

__all__ = [
    "digest_hex",
    "new_identity",
    "optional_text",
    "require_decimal",
    "require_text",
    "require_utc",
    "utf8_length",
]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utf8_length(text: str) -> int:
    """按 UTF-8 字节数计长:Oracle 的字节语义与 MySQL 的字符语义在这里统一。"""

    return len(text.encode("utf-8"))


def require_text(value: object, *, field: str, max_bytes: int) -> str:
    """必填短文本:拒绝非字符串、空串与超过 `max_bytes` 的值。"""

    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    if not value:
        raise ContractError(f"{field} must not be empty")
    if utf8_length(value) > max_bytes:
        raise ContractError(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return value


def optional_text(value: object, *, field: str, max_bytes: int) -> str | None:
    """可选短文本:缺失用 `None`;空串同样拒绝,避免四库语义分叉。"""

    if value is None:
        return None
    return require_text(value, field=field, max_bytes=max_bytes)


def require_decimal(value: object, *, field: str) -> Decimal:
    """整数或 `Decimal`;拒绝 `float`、`bool`、非有限值与字符串。"""

    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ContractError(f"{field} must be an int or Decimal")
    try:
        number = Decimal(value)
    except InvalidOperation as error:  # pragma: no cover - Decimal(int) cannot fail
        raise ContractError(f"{field} is not a decimal") from error
    if not number.is_finite():
        raise ContractError(f"{field} must be finite")
    return number


def require_utc(value: object, *, field: str) -> datetime:
    """接受 aware 或 naive 的 `datetime`;naive 视为 UTC,其余时区统一换算到 UTC。"""

    if not isinstance(value, datetime):
        raise ContractError(f"{field} must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def new_identity() -> str:
    """应用生成的 ULID:26 个 Crockford 字符,按时间可排序,`VARCHAR(128)` 内。"""

    millis = int(datetime.now(UTC).timestamp() * 1000)
    value = (millis << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    characters = ["0"] * 26
    for index in range(25, -1, -1):
        characters[index] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(characters)


def digest_hex(payload: bytes) -> str:
    """`CHAR(64)` 的 SHA-256 小写十六进制摘要。"""

    return hashlib.sha256(payload).hexdigest()
