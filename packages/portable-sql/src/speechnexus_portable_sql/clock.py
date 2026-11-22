"""数据库时钟的唯一读取点.

「现在」是**事务内即时时间**,不是事务开始时间:租约、预算释放与超时判定都要求
「最后一次等锁之后的时刻」,用事务时间会让一条等了很久的语句拿到过期的时间点。
各家的即时时间写法不同(PostgreSQL 的 `clock_timestamp()`、其他 profile 的等价函数),
所以时钟语句是**按 profile 登记的一条静态语句**,业务层只调 `now_us(...)`,
拿回一个 `BIGINT` 参数交给自己的语句。

这意味着业务语句里不会出现品牌函数,而品牌写法集中在登记表里,和驱动、DSN 一样
属于部署期 profile 选择(ADR 0016)。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol

#: 已登记 profile 的时钟语句。新增 profile 必须同时登记它的时钟写法与 oracle。
CLOCK_STATEMENTS: Final[dict[str, str]] = {
    # 事务内即时时间;`CURRENT_TIMESTAMP` 是事务开始时间,等锁之后会过期。
    "postgresql-16": "SELECT clock_timestamp() AS now",
}

DEFAULT_PROFILE: Final[str] = "postgresql-16"
PROFILE_VARIABLE: Final[str] = "SPEECHNEXUS_SQL_PROFILE"

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


class ClockReader(Protocol):
    """能执行一条语句并取回一行就够了;不要求整份连接契约.

    迁移期调用方手上有时是连接、有时已经是游标包装:两者都能执行语句,但取行的入口不同
    (连接的 `execute` 返回游标,游标自己就是取行入口)。这里按形状取用,是为了让时间
    读取可以先落地,而不必等某个模块整体迁移完。
    """

    def execute(self, sql: str, params: Sequence[object] = (), /) -> Any: ...


def active_profile() -> str:
    """部署期选择的 profile;未登记的值直接失败,不回退到别的写法."""

    profile = os.environ.get(PROFILE_VARIABLE, DEFAULT_PROFILE)
    if profile not in CLOCK_STATEMENTS:
        raise ValueError(f"Unregistered SQL profile for the database clock: {profile}")
    return profile


def now_statement(profile: str | None = None) -> str:
    chosen = profile if profile is not None else active_profile()
    try:
        return CLOCK_STATEMENTS[chosen]
    except KeyError:
        raise ValueError(f"Unregistered SQL profile for the database clock: {chosen}") from None


def to_epoch_us(value: datetime) -> int:
    """把带时区的时间换成 epoch 微秒;无时区的时间按 UTC 解释,不做本地猜测."""

    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _first_row(reader: ClockReader, statement: str) -> Any:
    """连接与游标两种形状都走一遍:执行一次,再取第一行."""

    result = reader.execute(statement)
    fetch = getattr(result, "fetchone", None)
    if fetch is not None:
        return fetch()
    fetch = getattr(reader, "fetchone", None)
    if fetch is not None:
        return fetch()
    raise TypeError("clock reader cannot fetch rows")


def now_us(connection: ClockReader) -> int:
    """读一次数据库时钟,换成 epoch 微秒交给调用方当参数用."""

    row = _first_row(connection, now_statement())
    if row is None:
        raise LookupError("database clock returned no row")
    value = row["now"]
    if not isinstance(value, datetime):
        # 驱动给了字符串说明 profile 与语句不匹配,不能猜。
        raise TypeError("database clock did not return a timestamp")
    return to_epoch_us(value)
