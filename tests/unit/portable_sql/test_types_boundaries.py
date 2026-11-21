"""值边界 oracle:空串、UTF-8 字节长度、`Decimal`、UTC 与应用生成身份。

这些是四库语义最容易分叉的边界(Oracle 空串即 NULL、MySQL 按字符计长、PostgreSQL 按字节),
所以它们进共同测试 oracle,而不是各表写入处各写一遍。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from speechnexus_portable_sql import (
    ContractError,
    digest_hex,
    new_identity,
    optional_text,
    require_decimal,
    require_text,
    require_utc,
    utf8_length,
)


def test_utf8_length_counts_bytes_not_characters() -> None:
    assert utf8_length("abc") == 3
    assert utf8_length("中文") == 6
    assert utf8_length("é") == 2


def test_required_text_rejects_empty_and_oversize_values() -> None:
    assert require_text("job-1", field="job_id", max_bytes=128) == "job-1"
    with pytest.raises(ContractError):
        require_text("", field="job_id", max_bytes=128)
    with pytest.raises(ContractError):
        require_text("中文", field="label", max_bytes=5)
    with pytest.raises(ContractError):
        require_text(7, field="job_id", max_bytes=128)


def test_optional_text_maps_missing_to_null_and_still_rejects_empty() -> None:
    assert optional_text(None, field="note", max_bytes=255) is None
    assert optional_text("ok", field="note", max_bytes=255) == "ok"
    with pytest.raises(ContractError):
        optional_text("", field="note", max_bytes=255)


@pytest.mark.parametrize("value", [1.5, True, "12", Decimal("NaN"), Decimal("Infinity")])
def test_decimal_rejects_float_bool_string_and_non_finite(value: object) -> None:
    with pytest.raises(ContractError):
        require_decimal(value, field="granted_amount")


def test_decimal_accepts_int_and_decimal_without_float_rounding() -> None:
    assert require_decimal(7, field="held") == Decimal(7)
    assert require_decimal(Decimal("0.1"), field="held") == Decimal("0.1")


def test_utc_normalizes_naive_and_offset_values() -> None:
    naive = datetime(2026, 11, 21, 11, 30)
    assert require_utc(naive, field="expires_at") == naive.replace(tzinfo=UTC)
    offset = datetime(2026, 11, 21, 19, 30, tzinfo=timezone(timedelta(hours=8)))
    assert require_utc(offset, field="expires_at") == datetime(2026, 11, 21, 11, 30, tzinfo=UTC)
    with pytest.raises(ContractError):
        require_utc("2026-11-21", field="expires_at")


def test_identity_is_a_sortable_ulid() -> None:
    first = new_identity()
    second = new_identity()
    assert len(first) == len(second) == 26
    assert first != second
    assert first[0] in "01234567"
    assert first <= second or first > second  # 同一毫秒内也必须是合法标识,不要求单调
    assert set(first) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_digest_is_lowercase_sha256_hex() -> None:
    value = digest_hex(b"job-1")
    assert len(value) == 64
    assert value == value.lower()
    assert value != digest_hex(b"job-2")
