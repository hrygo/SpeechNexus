"""时间只有一份定义:标准语句 + Python 换算,业务层不再出现品牌函数."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from speechnexus_portable_sql import clock
from speechnexus_portable_sql.contract import Row


def test_the_registered_clock_uses_instant_time_not_transaction_time() -> None:
    """事务时间(`CURRENT_TIMESTAMP`)在等锁之后会过期,不能当「现在」用."""

    statement = clock.now_statement("postgresql-16")
    assert "clock_timestamp()" in statement
    assert "CURRENT_TIMESTAMP" not in statement


def test_an_unregistered_profile_is_refused_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(clock.PROFILE_VARIABLE, "oracle-19")
    with pytest.raises(ValueError):
        clock.now_statement()
    monkeypatch.delenv(clock.PROFILE_VARIABLE)
    assert clock.now_statement() == clock.CLOCK_STATEMENTS[clock.DEFAULT_PROFILE]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(1970, 1, 1, tzinfo=UTC), 0),
        (datetime(1970, 1, 1, 0, 0, 1, 250_000, tzinfo=UTC), 1_250_000),
        (datetime(2026, 11, 22, 0, 0, 0, tzinfo=UTC), 1_795_305_600_000_000),
    ],
)
def test_conversion_is_exact_microseconds(value: datetime, expected: int) -> None:
    assert clock.to_epoch_us(value) == expected


def test_a_naive_time_is_read_as_utc_not_local() -> None:
    """无时区的时间按 UTC 解释:本地时区猜测会让不同主机得到不同的「现在」."""

    naive = datetime(2026, 11, 22, 0, 0, 0)
    assert clock.to_epoch_us(naive) == clock.to_epoch_us(naive.replace(tzinfo=UTC))


def test_an_offset_time_is_normalised_to_utc() -> None:
    shifted = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert clock.to_epoch_us(shifted) == clock.to_epoch_us(
        datetime(2026, 11, 22, 0, 0, 0, tzinfo=UTC)
    )


class _Connection:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def cursor(self) -> object: ...

    def execute(self, sql: str, params: Sequence[object] = ()) -> object:
        self.statements.append(sql)
        return self

    def fetchone(self) -> Row | None:
        return self.rows[0] if self.rows else None


def test_now_reads_one_row_and_converts_it() -> None:
    connection = _Connection([Row(["now"], (datetime(2026, 11, 22, tzinfo=UTC),))])
    assert clock.now_us(connection) == 1_795_305_600_000_000
    assert connection.statements == [clock.now_statement()]


def test_an_empty_clock_answer_is_an_error_not_a_zero() -> None:
    """没有行就是链路坏了;把 0 当「现在」会让租约立刻过期."""

    with pytest.raises(LookupError):
        clock.now_us(_Connection([]))
