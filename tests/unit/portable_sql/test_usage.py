"""usage 区间:精确重放、部分重叠拒绝与锁定后的 head 推进."""

from __future__ import annotations

from decimal import Decimal

import pytest

from speechnexus_portable_sql import (
    ContractError,
    IntervalOverlapError,
    Row,
    UsageAppend,
    append_usage_interval,
    read_usage_intervals,
)
from speechnexus_portable_sql.usage import STATEMENTS
from tests.support.portable_sql_fake import FakeDatabase

DIGEST = "sha256:" + "c" * 64
HEAD = Row(("last_end", "version"), (Decimal(100), Decimal(4)))
REPLAY = Row(
    ("interval_id", "interval_start", "interval_end", "fact_hash", "units"),
    ("i-0", Decimal(0), Decimal(100), DIGEST, Decimal(100)),
)


def database(
    *, replay: list[Row] | None = None, overlap: list[Row] | None = None, head: Row | None = HEAD
) -> FakeDatabase:
    return FakeDatabase(
        {
            STATEMENTS["stream.lock"]: [[head] if head is not None else []],
            STATEMENTS["interval.replay"]: [replay or []],
            STATEMENTS["interval.overlap"]: [overlap or []],
        },
        rowcounts={STATEMENTS["stream.advance"]: 1},
    )


def append(db: FakeDatabase, *, start: int = 100, end: int = 200) -> UsageAppend:
    return append_usage_interval(
        db.connect(),
        stream_id="stream-1",
        meter_kind="asr_samples",
        interval_start=Decimal(start),
        interval_end=Decimal(end),
        fact_hash=DIGEST,
    )


def test_exact_replay_returns_the_original_interval_without_writing() -> None:
    db = database(replay=[REPLAY])
    result = append(db, start=0, end=100)

    assert result.appended is False
    assert result.interval.interval_id == "i-0"
    assert [statement.sql for statement in db.statements] == [
        STATEMENTS["stream.lock"],
        STATEMENTS["interval.replay"],
    ]


def test_a_partial_overlap_is_rejected_even_without_a_key_conflict() -> None:
    db = database(
        overlap=[
            Row(
                ("interval_id", "interval_start", "interval_end"),
                ("i-1", Decimal(50), Decimal(150)),
            )
        ]
    )
    with pytest.raises(IntervalOverlapError, match=r"overlaps \[50, 150\)"):
        append(db, start=40, end=120)
    assert STATEMENTS["interval.insert"] not in [statement.sql for statement in db.statements]


def test_the_high_water_mark_rejects_a_late_interval() -> None:
    db = database()
    with pytest.raises(IntervalOverlapError, match="high water mark"):
        append(db, start=90, end=200)


def test_a_new_interval_locks_advances_the_head_and_inserts() -> None:
    db = database()
    result = append(db, start=100, end=250)

    assert result.appended is True
    assert result.last_end == Decimal(250)
    executed = [statement.sql for statement in db.statements]
    assert executed == [
        STATEMENTS["stream.lock"],
        STATEMENTS["interval.replay"],
        STATEMENTS["interval.overlap"],
        STATEMENTS["interval.insert"],
        STATEMENTS["stream.advance"],
    ]
    assert db.statements[3].params[1:] == (
        "stream-1",
        "asr_samples",
        Decimal(100),
        Decimal(250),
        DIGEST,
        Decimal(150),
    )
    assert db.statements[4].params == (Decimal(250), "stream-1", "asr_samples", Decimal(4))


def test_a_head_that_moves_inside_the_locked_transaction_is_an_error() -> None:
    db = FakeDatabase(
        {
            STATEMENTS["stream.lock"]: [[HEAD]],
            STATEMENTS["interval.replay"]: [[]],
            STATEMENTS["interval.overlap"]: [[]],
        },
        rowcounts={STATEMENTS["stream.advance"]: 0},
    )
    with pytest.raises(ContractError, match="version moved"):
        append(db)


def test_an_unregistered_stream_is_refused() -> None:
    db = database(head=None)
    with pytest.raises(ContractError, match="not registered"):
        append(db)


@pytest.mark.parametrize(("start", "end"), [(100, 100), (200, 100), (-1, 10)])
def test_half_open_intervals_must_be_ordered(start: int, end: int) -> None:
    db = database()
    with pytest.raises(ContractError, match="half open"):
        append(db, start=start, end=end)


def test_reading_uses_keyset_pagination() -> None:
    db = FakeDatabase({STATEMENTS["interval.read"]: [[REPLAY]]})
    intervals = read_usage_intervals(
        db.connect(), stream_id="stream-1", meter_kind="asr_samples", after_start=Decimal(-1)
    )
    assert [item.interval_id for item in intervals] == ["i-0"]
    assert db.statements[0].params == ("stream-1", "asr_samples", Decimal(-1))
