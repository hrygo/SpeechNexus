"""本地 credit 不变量、背压/有限 drain 与双工公平."""

from __future__ import annotations

import pytest

from speechnexus_portable_sql import (
    ContractError,
    CreditLedger,
    DualLane,
    LedgerInvariantError,
)


def ledger(*, granted: int = 0, low_water: int = 0) -> CreditLedger:
    book = CreditLedger(low_water=low_water)
    if granted:
        assert book.top_up_after_renew(lambda: True, units=granted)
    return book


def test_credit_is_only_added_after_a_committed_renewal() -> None:
    book = ledger()
    assert not book.top_up_after_renew(lambda: False, units=100)
    assert book.snapshot().available == 0
    assert book.snapshot().persisted_granted == 0


def test_reserve_accept_and_settle_keep_the_ledger_balanced() -> None:
    book = ledger(granted=10)
    assert book.reserve(6)
    book.accept(6)
    book.settle(4)

    snapshot = book.snapshot()
    assert (
        snapshot.persisted_granted,
        snapshot.settled,
        snapshot.unsettled,
        snapshot.available,
    ) == (
        10,
        4,
        2,
        4,
    )
    assert snapshot.balances()


def test_reserve_refuses_to_overspend() -> None:
    book = ledger(granted=2)
    assert book.reserve(3) is False
    assert book.snapshot().available == 2


def test_abandon_returns_in_flight_credit() -> None:
    book = ledger(granted=5)
    assert book.reserve(3)
    book.abandon(3)
    assert book.snapshot().in_flight == 0
    assert book.snapshot().available == 5


def test_settle_cannot_exceed_unsettled_consumption() -> None:
    book = ledger(granted=5)
    book.reserve(2)
    book.accept(2)
    with pytest.raises(ContractError):
        book.settle(3)


def test_unused_credit_cannot_be_returned_while_frames_are_in_flight() -> None:
    book = ledger(granted=5)
    assert book.reserve(1)
    with pytest.raises(ContractError, match="in flight"):
        book.return_unused()


def test_return_unused_only_moves_never_consumed_units() -> None:
    book = ledger(granted=5)
    assert book.return_unused() == 5
    snapshot = book.snapshot()
    assert (snapshot.returned, snapshot.available) == (5, 0)
    assert snapshot.balances()


def test_low_water_triggers_renewal_without_consuming() -> None:
    book = ledger(granted=10, low_water=4)
    assert not book.needs_renewal()
    assert book.reserve(7)
    assert book.needs_renewal()


def test_a_broken_ledger_stops_the_session() -> None:
    book = ledger(granted=1)
    book._available = -1
    with pytest.raises(LedgerInvariantError):
        book.reserve(1)


def test_drain_plan_is_bounded_by_credit_and_deadline() -> None:
    book = ledger(granted=10)
    assert book.drain_plan(frame_units=2, seconds_left=10, frames_per_second=100).frames == 5

    short = ledger(granted=100)
    decision = short.drain_plan(frame_units=1, seconds_left=1, frames_per_second=10)
    assert decision.action == "drain"
    assert (decision.frames, decision.reason) == (10, "deadline bound")

    empty = ledger()
    assert empty.drain_plan(frame_units=1, seconds_left=5, frames_per_second=10).action == "reject"


def test_dual_lanes_reserve_for_each_other_and_never_starve() -> None:
    lanes = DualLane(realtime_units=4, batch_units=2)

    for _ in range(4):
        assert lanes.admit("realtime", 1).admitted
    assert not lanes.admit("realtime", 1).admitted

    # realtime 耗尽后仍不能吃掉 batch 的保留额度。
    assert lanes.admit("batch", 2).admitted
    assert lanes.lane_balances() == (0, 0)

    lanes.replenish(realtime_units=1, batch_units=1)
    assert lanes.admit("batch", 1).admitted
    assert not lanes.batch_is_waiting()


def test_a_waiting_batch_lane_is_visible_to_the_scheduler() -> None:
    lanes = DualLane(realtime_units=1, batch_units=1)
    assert lanes.admit("batch", 2).admitted is False
    assert lanes.batch_is_waiting()
    assert lanes.admit("realtime", 1).admitted
    lanes.replenish(realtime_units=0, batch_units=1)
    assert lanes.admit("batch", 1).admitted
    assert not lanes.batch_is_waiting()
