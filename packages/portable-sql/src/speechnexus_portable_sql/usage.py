"""半开 usage 区间:先精确重放,再锁定重叠检查,最后追加.

数据库热路径只有一处:segment / utterance 边界。规则只有三条:

1. 同 `(stream_id, meter_kind, interval_start, interval_end, fact_hash)` 的重放返回原区间,
   不新增行、不增加消费;
2. 任何部分重叠都拒绝,即使主键或唯一键本来就不冲突;唯一键不能替代重叠检查;
3. 追加前锁 `output_stream_heads` 的权威行,写入与 head 推进在同一事务里,所以并发写者不会
   在检查与追加之间插入一条重叠区间。

区间是半开的 `[interval_start, interval_end)`,单位是整数样本或 codepoint,没有浮点。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from speechnexus_portable_sql.contract import Connection, ContractError
from speechnexus_portable_sql.types import new_identity, require_digest, require_text

__all__ = [
    "STATEMENTS",
    "IntervalOverlapError",
    "UsageAppend",
    "UsageInterval",
    "append_usage_interval",
    "read_usage_intervals",
]

STATEMENTS: dict[str, str] = {
    "stream.lock": (
        "SELECT last_end, version FROM output_stream_heads "
        "WHERE stream_id = ? AND meter_kind = ? FOR UPDATE"
    ),
    "interval.replay": (
        "SELECT interval_id, interval_start, interval_end, fact_hash, units FROM usage_intervals "
        "WHERE stream_id = ? AND meter_kind = ? AND interval_start = ? AND interval_end = ? "
        "AND fact_hash = ?"
    ),
    "interval.overlap": (
        "SELECT interval_id, interval_start, interval_end FROM usage_intervals "
        "WHERE stream_id = ? AND meter_kind = ? AND interval_start < ? AND interval_end > ? "
        "ORDER BY interval_start"
    ),
    "interval.insert": (
        "INSERT INTO usage_intervals (interval_id, stream_id, meter_kind, interval_start, "
        "interval_end, fact_hash, units) VALUES (?, ?, ?, ?, ?, ?, ?)"
    ),
    "stream.advance": (
        "UPDATE output_stream_heads SET last_end = ?, version = version + 1 "
        "WHERE stream_id = ? AND meter_kind = ? AND version = ?"
    ),
    "interval.read": (
        "SELECT interval_id, interval_start, interval_end, fact_hash, units FROM usage_intervals "
        "WHERE stream_id = ? AND meter_kind = ? AND interval_start > ? ORDER BY interval_start"
    ),
}


class IntervalOverlapError(ContractError):
    """区间与已提交的事实部分重叠:不是重放,必须拒绝而不是改写历史。"""


@dataclass(frozen=True, slots=True)
class UsageInterval:
    interval_id: str
    interval_start: Decimal
    interval_end: Decimal
    fact_hash: str
    units: Decimal


@dataclass(frozen=True, slots=True)
class UsageAppend:
    interval: UsageInterval
    appended: bool
    last_end: Decimal


def _lock_stream(
    connection: Connection, *, stream_id: str, meter_kind: str
) -> tuple[Decimal, Decimal]:
    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["stream.lock"], (stream_id, meter_kind))
        rows = cursor.fetchall()
    finally:
        cursor.close()
    if not rows:
        raise ContractError(f"output stream {stream_id!r}/{meter_kind} is not registered")
    row = rows[0]
    return Decimal(str(row.at(0))), Decimal(str(row.at(1)))


def _replay(
    connection: Connection,
    *,
    stream_id: str,
    meter_kind: str,
    start: Decimal,
    end: Decimal,
    fact_hash: str,
) -> UsageInterval | None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["interval.replay"],
            (stream_id, meter_kind, start, end, fact_hash),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
    if not rows:
        return None
    row = rows[0]
    return UsageInterval(
        interval_id=str(row.at(0)),
        interval_start=Decimal(str(row.at(1))),
        interval_end=Decimal(str(row.at(2))),
        fact_hash=str(row.at(3)),
        units=Decimal(str(row.at(4))),
    )


def _overlaps(
    connection: Connection, *, stream_id: str, meter_kind: str, start: Decimal, end: Decimal
) -> list[UsageInterval]:
    cursor = connection.cursor()
    try:
        cursor.execute(STATEMENTS["interval.overlap"], (stream_id, meter_kind, end, start))
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [
        UsageInterval(
            interval_id=str(row.at(0)),
            interval_start=Decimal(str(row.at(1))),
            interval_end=Decimal(str(row.at(2))),
            fact_hash="",
            units=Decimal(0),
        )
        for row in rows
    ]


def append_usage_interval(
    connection: Connection,
    *,
    stream_id: str,
    meter_kind: str,
    interval_start: Decimal,
    interval_end: Decimal,
    fact_hash: str,
    units: Decimal | None = None,
) -> UsageAppend:
    """追加一条边界区间;精确重放返回原区间,部分重叠抛 `IntervalOverlapError`。"""

    require_text(stream_id, field="stream_id", max_bytes=128)
    require_text(meter_kind, field="meter_kind", max_bytes=24)
    digest = require_digest(fact_hash, field="fact_hash")
    if interval_start < 0 or interval_end <= interval_start:
        raise ContractError("interval must be half open and ordered: 0 <= start < end")
    computed = interval_end - interval_start if units is None else units
    if computed <= 0:
        raise ContractError("units must be positive")

    last_end, version = _lock_stream(connection, stream_id=stream_id, meter_kind=meter_kind)

    replayed = _replay(
        connection,
        stream_id=stream_id,
        meter_kind=meter_kind,
        start=interval_start,
        end=interval_end,
        fact_hash=digest,
    )
    if replayed is not None:
        return UsageAppend(interval=replayed, appended=False, last_end=last_end)

    overlaps = _overlaps(
        connection,
        stream_id=stream_id,
        meter_kind=meter_kind,
        start=interval_start,
        end=interval_end,
    )
    if overlaps:
        first = overlaps[0]
        raise IntervalOverlapError(
            f"interval [{interval_start}, {interval_end}) overlaps "
            f"[{first.interval_start}, {first.interval_end})"
        )
    if interval_start < last_end:
        raise IntervalOverlapError(
            f"interval starts at {interval_start}, before the committed high water mark {last_end}"
        )

    interval = UsageInterval(
        interval_id=new_identity(),
        interval_start=interval_start,
        interval_end=interval_end,
        fact_hash=digest,
        units=computed,
    )
    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["interval.insert"],
            (
                interval.interval_id,
                stream_id,
                meter_kind,
                interval_start,
                interval_end,
                digest,
                computed,
            ),
        )
        cursor.execute(
            STATEMENTS["stream.advance"],
            (interval_end, stream_id, meter_kind, version),
        )
        if cursor.rowcount != 1:
            raise ContractError("stream head version moved inside the locked transaction")
    finally:
        cursor.close()
    return UsageAppend(interval=interval, appended=True, last_end=interval_end)


def read_usage_intervals(
    connection: Connection, *, stream_id: str, meter_kind: str, after_start: Decimal
) -> list[UsageInterval]:
    """keyset 续读;调用方按 `interval_start` 前进,不需要 `OFFSET`。"""

    cursor = connection.cursor()
    try:
        cursor.execute(
            STATEMENTS["interval.read"],
            (stream_id, meter_kind, after_start),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [
        UsageInterval(
            interval_id=str(row.at(0)),
            interval_start=Decimal(str(row.at(1))),
            interval_end=Decimal(str(row.at(2))),
            fact_hash=str(row.at(3)),
            units=Decimal(str(row.at(4))),
        )
        for row in rows
    ]
