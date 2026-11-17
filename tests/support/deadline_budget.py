"""Budget real-socket total-deadline tests above this machine's fast path.

A total deadline that covers the fast path (dial, request, slow-stage entry) as
well as the slow stage races the machine, not the peer. One stalled runner, or a
coverage-traced cold first request, and the deadline ends the read before the
slow stage is entered at all, so `assert started.is_set()` fails for a reason
that has nothing to do with the behaviour under test (#938, #944).

The calibration reads run the same complete operation with the slow stage
disabled: they pay the one-time costs -- httpx/httpcore/anyio imports, the first
connection, the first `ThreadingHTTPServer` handler thread -- outside the budget
and measure what this machine actually needs. The budget is ten times the slowest
of them, and the peer's trickle is stretched to the same multiple so a still-clear
`trickled` event keeps meaning "cut, not drained" at any budget.

That tenfold margin holds only while `MAXIMUM_BUDGET_SECONDS` does not bind. A
ceiling-clamped budget keeps as little as twice the slowest calibration read
(measured: fast path 1.0046 s against budget 2.0 s), so `clamp_budget` prints
`BUDGET CEILING HIT` on stderr rather than promising a margin it is not keeping.

Two costs of this wiring are recorded here, not in the review that found them
(#945, report #947, closed out by #948), because they are properties of this
file.

Passing reads did not get slower. A read cut at the deadline costs about the
deadline -- 0.52-0.61 s with the floor binding, 2.03-2.08 s with the ceiling
binding -- and the peer stops trickling on `BrokenPipe` as soon as the client
closes. Failing reads did get slower: when the code under test stops honouring
the deadline, the read drains the whole trickle before it can fail. That path was
1.2 s against the old fixed 40 frames and is 11.58 s at the floor / 44.33 s at the
ceiling, so nine worst-case tests are roughly 6.6 min. The unit-test job budgets
15 min and normally spends about 4, so a genuine regression still fits, but it now
consumes an order of magnitude more of that margin -- and no retry, rerun or
timeout setting is allowed to paper over it.

The calibration also absorbs "only the first read in this process is slow"
defects instead of failing on them: with a 1.0 s one-time delay injected into the
first read, the old flat 0.3 s wiring went 6 failed and this wiring goes 6 passed,
because the first three reads are the calibration. The review judged that not a
defect -- the contract under test is "was the read cut", not "how fast is the fast
path" -- but it does silently replace an undeclared side constraint ("fast path
<= 0.3 s or red") with "fast path <= 2.0 s or red". Guarding the fast-path latency
itself needs its own explicit assertion or benchmark; do not read these tests as
one.
"""

import math
import sys
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
# this ceiling the fast path is a runner problem, not the behaviour under test,
# and `clamp_budget` announces the clamp so the failure printed next to it can be
# read that way instead of as a behaviour regression.
MAXIMUM_BUDGET_SECONDS = 2.0
# The deadline, not the peer's end of stream, must end the read, so the read
# ends at the budget. The upper bound is still an order of magnitude below the
# trickle total, so a drained read could never reach it, while a scheduler stall
# of up to one budget in the detection/cleanup path stays green.
DEADLINE_LOWER_RATIO = 0.9
DEADLINE_UPPER_RATIO = 2.0


async def calibrated_budget(complete_read: Callable[[], Awaitable[object]]) -> float:
    """Return a budget above this machine's fast path, capped by the ceiling.

    `complete_read` must run the operation end to end with the slow stage
    disabled, so the one-time costs land here instead of inside the budgeted
    read, where they used to be an unaccounted part of the fast path.

    The budget is `FAST_PATH_MULTIPLE` times the slowest calibration read unless
    the ceiling binds; a binding ceiling shrinks that margin (2x, measured) and
    `clamp_budget` says so on stderr.
    """

    samples: list[float] = []
    for _ in range(CALIBRATION_READS):
        started = time.monotonic()
        await complete_read()
        samples.append(time.monotonic() - started)
    return clamp_budget(FAST_PATH_MULTIPLE * max(samples))


def clamp_budget(calibrated: float) -> float:
    """Clamp a calibrated budget into the configured bounds and report a binding ceiling.

    The clamp answers `MAXIMUM` when the two bounds are inverted -- silently
    contradicting the value a reader would derive from the constants -- so the
    assert turns that misconfiguration into a failure at the call site instead.

    When `MAXIMUM` binds, the budget no longer keeps `FAST_PATH_MULTIPLE` over the
    measured fast path, and the shortfall is a runner fact rather than anything
    about the behaviour under test. It is therefore announced instead of hidden:
    pytest prints a failing test's captured stderr, which puts the line in the
    same report as the assertion it explains.
    """

    assert MINIMUM_BUDGET_SECONDS <= MAXIMUM_BUDGET_SECONDS, (
        f"budget bounds are inverted: MINIMUM_BUDGET_SECONDS={MINIMUM_BUDGET_SECONDS} "
        f"MAXIMUM_BUDGET_SECONDS={MAXIMUM_BUDGET_SECONDS}"
    )
    budget = min(MAXIMUM_BUDGET_SECONDS, max(MINIMUM_BUDGET_SECONDS, calibrated))
    if calibrated > MAXIMUM_BUDGET_SECONDS:
        print(
            f"BUDGET CEILING HIT: fast_path={calibrated / FAST_PATH_MULTIPLE:.4f}s "
            f"budget={budget:.3f}s — a stalled runner, not the behaviour under test",
            file=sys.stderr,
        )
    return budget


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
    # read before the behaviour under test was reached. The calibrated margin
    # (ten times the calibration read, or as little as twice when the ceiling
    # binds), not a rerun, is what keeps that from happening at random.
    assert started, observed
    assert fast_path is not None and fast_path < budget, observed
    # The peer never finished trickling: the read was cut, not drained.
    assert not trickled, observed
    # Ended at the total deadline: near the budget, an order of magnitude from
    # the trickle total a drained read would have lasted.
    assert budget * DEADLINE_LOWER_RATIO <= elapsed <= budget * DEADLINE_UPPER_RATIO, observed
