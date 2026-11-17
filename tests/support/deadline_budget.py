"""Budget real-socket total-deadline tests above this machine's fast path.

A total deadline that covers the fast path (dial, request, slow-stage entry) as
well as the slow stage races the machine, not the peer. One stalled runner, or a
coverage-traced cold first request, and the deadline ends the read before the
slow stage is entered at all, so `assert started.is_set()` fails for a reason
that has nothing to do with the behaviour under test (#938, #944).

The calibration reads run the same complete operation with the slow stage
disabled: they pay the one-time costs -- httpx/httpcore/anyio imports, the first
connection, the first `ThreadingHTTPServer` handler thread -- outside the budget
and measure what this machine actually needs. The budget is an order of
magnitude above the slowest of them, so the deadline is a bound instead of a
guess, and the peer's trickle is stretched to the same multiple so a still-clear
`trickled` event keeps meaning "cut, not drained" at any budget.
"""

import math
import time
from collections.abc import Awaitable, Callable

# One trickle frame every 30 ms: far faster than any socket inactivity timeout,
# so peer activity alone cannot extend the read, and far slower than the budgets
# below, which is what keeps the peer writing when the total deadline fires.
TRICKLE_INTERVAL_SECONDS = 0.03
# The peer's trickle total outlives the budget by this factor, so a still-clear
# `trickled` event proves the read was cut rather than drained at any budget.
TRICKLE_BUDGET_MULTIPLE = 20
# The budget races the fast path and nothing else. A coverage-traced cold first
# read spends 25-50 ms on one-time costs, and a stalled runner adds hundreds of
# ms on top; 0.5 s keeps the deadline away from that tail, which is where the
# merge gate used to end the read before the slow stage was entered.
MINIMUM_BUDGET_SECONDS = 0.5
# A machine whose fast path is genuinely slower gets a proportionally larger
# budget: the slowest of a few calibration reads times this. The calibration
# reads also pay the one-time costs, so they stay outside the budgeted read.
FAST_PATH_MULTIPLE = 10
CALIBRATION_READS = 3
# A single stalled calibration read must not blow the test's runtime up: past
# this ceiling the fast path is a runner problem the failure message shows.
MAXIMUM_BUDGET_SECONDS = 2.0
# The deadline, not the peer's end of stream, must end the read, so the read
# ends at the budget. The upper bound is still an order of magnitude below the
# trickle total, so a drained read could never reach it, while a scheduler stall
# of up to one budget in the detection/cleanup path stays green.
DEADLINE_LOWER_RATIO = 0.9
DEADLINE_UPPER_RATIO = 2.0


async def calibrated_budget(complete_read: Callable[[], Awaitable[object]]) -> float:
    """Return a budget an order of magnitude above this machine's fast path.

    `complete_read` must run the operation end to end with the slow stage
    disabled, so the one-time costs land here instead of inside the budgeted
    read, where they used to be an unaccounted part of the fast path.
    """

    samples: list[float] = []
    for _ in range(CALIBRATION_READS):
        started = time.monotonic()
        await complete_read()
        samples.append(time.monotonic() - started)
    calibrated = FAST_PATH_MULTIPLE * max(samples)
    return min(MAXIMUM_BUDGET_SECONDS, max(MINIMUM_BUDGET_SECONDS, calibrated))


def trickle_frame_count(budget: float) -> int:
    """Return the frames the peer must write so its trickle outlives the budget."""

    return math.ceil(TRICKLE_BUDGET_MULTIPLE * budget / TRICKLE_INTERVAL_SECONDS)


def assert_cut_at_deadline(
    *,
    budget: float,
    elapsed: float,
    fast_path: float | None,
    started: bool,
    trickled: bool,
    calls: list[str],
) -> None:
    """Assert the total deadline -- not the peer's end of stream -- ended the read.

    Four checks, in the order that makes a failure readable: the slow stage was
    reached, the fast path fits inside the budget with the calibrated margin,
    the peer never finished trickling, and the read ended at the budget rather
    than somewhere a drained read could reach.
    """

    observed = (
        f"budget={budget:.3f}s elapsed={elapsed:.3f}s fast_path={fast_path} "
        f"trickled={trickled} calls={calls}"
    )
    # The slow stage must have been entered: otherwise the deadline ended the
    # read before the behaviour under test was reached. The order-of-magnitude
    # margin, not a rerun, is what keeps that from happening at random.
    assert started, observed
    assert fast_path is not None and fast_path < budget, observed
    # The peer never finished trickling: the read was cut, not drained.
    assert not trickled, observed
    # Ended at the total deadline: near the budget, an order of magnitude from
    # the trickle total a drained read would have lasted.
    assert budget * DEADLINE_LOWER_RATIO <= elapsed <= budget * DEADLINE_UPPER_RATIO, observed
