"""网关本地 credit:有界消费、低水位续租、显式背压与双工/批量公平.

数据库只在四处被访问:会话准入、低水位续租、有界 checkpoint 与边界 usage。本模块只管本地,所以
它的不变量可以在一处声明并在每次状态迁移后检查:

    persisted_granted = settled + unsettled + returned + in_flight + available

三条硬规则:

1. `available` 永远不为负;不能消费未提交的额度。
2. 只有续租**已成功提交**之后才允许增加本地 credit(`top_up_after_renew`)。
3. 余额不足以覆盖一帧时给出明确背压或有限 drain,不静默丢帧、不超发。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from speechnexus_portable_sql.contract import ContractError

__all__ = [
    "CreditLedger",
    "CreditSnapshot",
    "DrainDecision",
    "DualLane",
    "LaneAdmission",
    "LedgerInvariantError",
]


class LedgerInvariantError(ContractError):
    """本地账本不满足 grant 上限不变量:这是超发或重复结算,必须停机而不是继续跑。"""


@dataclass(frozen=True, slots=True)
class CreditSnapshot:
    persisted_granted: int
    settled: int
    unsettled: int
    returned: int
    in_flight: int
    available: int

    def balances(self) -> bool:
        return self.persisted_granted == (
            self.settled + self.unsettled + self.returned + self.in_flight + self.available
        )


@dataclass(frozen=True, slots=True)
class DrainDecision:
    action: str
    frames: int
    reason: str


class CreditLedger:
    """单进程、单会话的 credit 账本;所有方法都是同步的确定性迁移。"""

    __slots__ = (
        "_available",
        "_in_flight",
        "_low_water",
        "_persisted",
        "_returned",
        "_settled",
        "_unsettled",
    )

    def __init__(self, *, low_water: int = 0) -> None:
        if low_water < 0:
            raise ContractError("low_water must not be negative")
        self._persisted = 0
        self._settled = 0
        self._unsettled = 0
        self._returned = 0
        self._in_flight = 0
        self._available = 0
        self._low_water = low_water

    def snapshot(self) -> CreditSnapshot:
        return CreditSnapshot(
            persisted_granted=self._persisted,
            settled=self._settled,
            unsettled=self._unsettled,
            returned=self._returned,
            in_flight=self._in_flight,
            available=self._available,
        )

    def _check(self) -> None:
        if self._available < 0 or self._in_flight < 0:
            raise LedgerInvariantError("credit ledger went negative; stop the session")
        if not self.snapshot().balances():
            raise LedgerInvariantError("credit ledger does not balance against the persisted grant")

    def top_up_after_renew(self, renew: Callable[[], bool], *, units: int) -> bool:
        """**先**持久续租,**成功**后才增加本地 credit;顺序反过来就是超发。"""

        self._check()
        if units <= 0:
            raise ContractError("top-up units must be positive")
        if not renew():
            return False
        self._persisted += units
        self._available += units
        self._check()
        return True

    def needs_renewal(self) -> bool:
        return self._available <= self._low_water

    def reserve(self, units: int) -> bool:
        """为一帧预留额度;不足即背压,不排队等数据库。"""

        self._check()
        if units <= 0:
            raise ContractError("units must be positive")
        if self._available < units:
            return False
        self._available -= units
        self._in_flight += units
        self._check()
        return True

    def abandon(self, units: int) -> None:
        self._check()
        if units <= 0 or units > self._in_flight:
            raise ContractError("cannot abandon more than is in flight")
        self._in_flight -= units
        self._available += units
        self._check()

    def accept(self, units: int) -> None:
        """帧已产生:从在途转入未结算消费,总额不变。"""

        self._check()
        if units <= 0 or units > self._in_flight:
            raise ContractError("cannot accept more than is in flight")
        self._in_flight -= units
        self._unsettled += units
        self._check()

    def settle(self, units: int) -> None:
        """结算只把本地已消费量迁入持久 consumed,不重复增加总消费。"""

        self._check()
        if units <= 0 or units > self._unsettled:
            raise ContractError("cannot settle more than is unsettled")
        self._unsettled -= units
        self._settled += units
        self._check()

    def return_unused(self) -> int:
        """只归还从未用过的额度;已消费与在途都不能退。"""

        self._check()
        if self._in_flight:
            raise ContractError("cannot return credit while frames are in flight")
        units = self._available
        self._available = 0
        self._returned += units
        self._check()
        return units

    def drain_plan(
        self, *, frame_units: int, seconds_left: float, frames_per_second: int
    ) -> DrainDecision:
        """有限 drain:只承诺在 deadline 内真的能播完的帧数,剩下的明确背压。"""

        if frame_units <= 0 or frames_per_second <= 0:
            raise ContractError("frame_units and frames_per_second must be positive")
        if seconds_left <= 0:
            return DrainDecision("reject", 0, "deadline expired")
        affordable = self._available // frame_units
        if affordable <= 0:
            return DrainDecision("reject", 0, "no credit left")
        timed = int(seconds_left * frames_per_second)
        frames = min(affordable, max(timed, 0))
        if frames <= 0:
            return DrainDecision("reject", 0, "credit covers less than one schedulable frame")
        if frames < affordable:
            return DrainDecision("drain", frames, "deadline bound")
        return DrainDecision("drain", frames, "credit bound")


@dataclass(frozen=True, slots=True)
class LaneAdmission:
    admitted: bool
    lane: str
    remaining: int
    reason: str


class DualLane:
    """realtime 与 batch 各自持有保留额度;两侧都不能互相挤占或饿死。"""

    __slots__ = ("_batch", "_batch_waiting", "_realtime")

    def __init__(self, *, realtime_units: int, batch_units: int) -> None:
        if realtime_units <= 0 or batch_units <= 0:
            raise ContractError("each lane needs a positive reservation")
        self._realtime = realtime_units
        self._batch = batch_units
        self._batch_waiting = False

    def replenish(self, *, realtime_units: int, batch_units: int) -> None:
        if realtime_units < 0 or batch_units < 0:
            raise ContractError("replenishment must not be negative")
        self._realtime += realtime_units
        self._batch += batch_units

    def admit(self, lane: str, units: int) -> LaneAdmission:
        if units <= 0:
            raise ContractError("units must be positive")
        if lane == "realtime":
            if self._realtime >= units:
                self._realtime -= units
                return LaneAdmission(True, lane, self._realtime, "realtime reservation")
            return LaneAdmission(False, lane, self._realtime, "realtime reservation exhausted")
        if lane == "batch":
            if self._batch >= units:
                self._batch -= units
                self._batch_waiting = False
                return LaneAdmission(True, lane, self._batch, "batch reservation")
            self._batch_waiting = True
            return LaneAdmission(False, lane, self._batch, "batch reservation exhausted")
        raise ContractError(f"unknown lane {lane!r}")

    def lane_balances(self) -> tuple[int, int]:
        return self._realtime, self._batch

    def batch_is_waiting(self) -> bool:
        return self._batch_waiting
