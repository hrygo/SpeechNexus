"""The bounded-read assertion accepts every bounded plan and names a real degradation.

CI #1026 failed this read's assertion on a plan that had not degraded: the planner drove
the fourth debt read from the held leases instead of the fence window, so the fence-age
index was absent and the lease index returned the 15 held leases rather than the 5
in-window rows. The plans below are **hand-built** to carry the parts of that shape the
assertion reads -- the node types, relation and index names, and the per-loop row counts
reports for the loops it ran -- plus the plans the assertion must reject, so the test does
not depend on which plan the planner happens to pick on a given machine.

They are not byte-for-byte EXPLAIN output: node order and the `Actual Loops` of outer
children are simplified, and a real Plan JSON carries fields these omit. That is fine for
what they pin -- the assertion's decision, not the planner's shape -- but it means a change
to how plans are *reported* can pass these while the integration assertion still needs its
own real-PostgreSQL run (which it has).

The rejected plans are built per growth relation rather than as one degraded plan: the list
is the guard's whole input, so each entry needs its own scan to be shown load-bearing, and a
sequential scan anywhere in a plan that still returns its rows is exactly the degradation
the assertion must name.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.test_permanent_debt_plans_pg import (
    _BOUNDED_LIMIT,
    _GROWTH_RELATIONS,
    _IN_WINDOW,
    _assert_bounded_read,
)


def _node(
    kind: str,
    *,
    relation: str | None = None,
    index: str | None = None,
    rows: int = 0,
    loops: int = 1,
    plans: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {"Node Type": kind, "Actual Rows": rows, "Actual Loops": loops}
    if relation is not None:
        node["Relation Name"] = relation
    if index is not None:
        node["Index Name"] = index
    if plans is not None:
        node["Plans"] = plans
    return node


def _fence_driven(index: str = "speech_job_cancellation_fence_age_scan") -> dict[str, Any]:
    """The plan the local planner picks: the window range is the driving set."""
    return _node(
        "Limit",
        rows=_IN_WINDOW,
        plans=[
            _node(
                "Nested Loop",
                rows=_IN_WINDOW,
                plans=[
                    _node(
                        "Index Only Scan",
                        relation="speech_job_cancellation_fences",
                        index=index,
                        rows=_IN_WINDOW,
                    ),
                    _node(
                        "Index Scan",
                        relation="admission_leases",
                        index="speech_job_cancellation_lease_debt_scan",
                        rows=15,
                    ),
                ],
            )
        ],
    )


def _lease_driven() -> dict[str, Any]:
    """CI #1026's plan: the held leases drive it and the fences are point lookups.

    The fence lookup reports ``Actual Rows`` 0 because a nested-loop inner node reports
    rows per loop: 5 rows over 15 loops rounds to 0. That is why the assertion counts the
    read's own output rather than an index node's rows.
    """
    return _node(
        "Limit",
        rows=_IN_WINDOW,
        plans=[
            _node(
                "Sort",
                rows=_IN_WINDOW,
                plans=[
                    _node(
                        "Nested Loop",
                        rows=_IN_WINDOW,
                        plans=[
                            _node(
                                "Nested Loop",
                                rows=_IN_WINDOW,
                                plans=[
                                    _node(
                                        "Index Scan",
                                        relation="speech_job_cancellation_fences",
                                        index="speech_job_cancellation_fences_pkey",
                                        rows=0,
                                        loops=15,
                                    ),
                                    _node(
                                        "Nested Loop",
                                        rows=15,
                                        plans=[
                                            _node(
                                                "Index Scan",
                                                relation="speech_jobs",
                                                index="speech_jobs_pkey",
                                                rows=1,
                                                loops=15,
                                            ),
                                            _node(
                                                "Index Scan",
                                                relation="admission_leases",
                                                index="speech_job_cancellation_lease_debt_scan",
                                                rows=15,
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                            # The integration seed writes one intent per lease and one
                            # evidence row per intent, so both are key descents in the plan
                            # this shape stands for.
                            _node(
                                "Index Scan",
                                relation="submit_intents",
                                index="submit_intents_lease_id_key",
                                rows=1,
                                loops=15,
                            ),
                            _node(
                                "Index Scan",
                                relation="execution_stop_evidence",
                                index="execution_stop_evidence_submit_intent_id_key",
                                rows=1,
                                loops=15,
                            ),
                        ],
                    )
                ],
            )
        ],
    )


def _degraded() -> dict[str, Any]:
    """The plan with index and bitmap scans disabled: it still returns the 5 rows."""
    return _node(
        "Limit",
        rows=_IN_WINDOW,
        plans=[
            _node(
                "Hash Join",
                rows=_IN_WINDOW,
                plans=[
                    _node(
                        "Seq Scan",
                        relation="speech_job_cancellation_fences",
                        rows=_IN_WINDOW,
                        loops=2,
                    ),
                    _node(
                        "Hash",
                        rows=50508,
                        plans=[
                            _node(
                                "Seq Scan",
                                relation="speech_jobs",
                                rows=50508,
                                loops=2,
                            )
                        ],
                    ),
                ],
            )
        ],
    )


def test_the_fence_window_plan_is_bounded() -> None:
    _assert_bounded_read(_fence_driven(), expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)


def test_a_bitmap_plan_over_the_window_is_bounded() -> None:
    plan = _node(
        "Limit",
        rows=_IN_WINDOW,
        plans=[
            _node(
                "Bitmap Heap Scan",
                relation="speech_job_cancellation_fences",
                rows=_IN_WINDOW,
                plans=[
                    _node(
                        "Bitmap Index Scan",
                        relation="speech_job_cancellation_fences",
                        index="speech_job_cancellation_fence_age_scan",
                        rows=_IN_WINDOW,
                    )
                ],
            )
        ],
    )
    _assert_bounded_read(plan, expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)


def test_a_third_index_that_answers_the_same_window_is_bounded() -> None:
    # The assertion must not care which bounded index the planner found: only that the
    # read stayed on an index and returned the window's rows.
    _assert_bounded_read(
        _fence_driven("some_later_bounded_path"), expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT
    )


def test_the_lease_driven_plan_from_ci_is_bounded() -> None:
    _assert_bounded_read(_lease_driven(), expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)


def test_a_sequential_scan_of_a_growth_relation_names_the_relation() -> None:
    with pytest.raises(
        AssertionError,
        match="returned 5 rows from a sequential scan of speech_job_cancellation_fences",
    ):
        _assert_bounded_read(_degraded(), expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)


def test_a_short_read_names_the_row_count_and_the_access_paths() -> None:
    short = _node(
        "Index Scan",
        relation="speech_job_cancellation_fences",
        index="speech_job_cancellation_fence_age_scan",
        rows=0,
    )
    with pytest.raises(AssertionError, match="returned 0 rows, not the 5 in-window rows"):
        _assert_bounded_read(short, expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)


@pytest.mark.parametrize("relation", _GROWTH_RELATIONS)
def test_a_sequential_scan_of_any_growth_relation_is_rejected(relation: str) -> None:
    """Every relation in the list is its own degradation, scanned while the read is bounded.

    The plan still returns the 5 in-window rows through the fence index, so the only thing
    that fails is the scan of ``relation``: the assertion must name that relation, or an
    entry added to the list would silently protect nothing.
    """
    plan = _node(
        "Limit",
        rows=_IN_WINDOW,
        plans=[
            _node(
                "Nested Loop",
                rows=_IN_WINDOW,
                plans=[
                    _node(
                        "Index Only Scan",
                        relation="speech_job_cancellation_fences",
                        index="speech_job_cancellation_fence_age_scan",
                        rows=_IN_WINDOW,
                    ),
                    _node("Seq Scan", relation=relation, rows=50_000, loops=1),
                ],
            )
        ],
    )
    with pytest.raises(
        AssertionError,
        match=f"sequential scan of {relation}",
    ):
        _assert_bounded_read(plan, expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)


def test_a_plan_without_index_backed_access_cannot_carry_the_rows() -> None:
    # A relation outside the growth list, so the scan guard stays silent and the missing
    # index-backed node is the fact this case is about.
    plan = _node(
        "Seq Scan",
        relation="host_nodes",
        rows=_IN_WINDOW,
    )
    with pytest.raises(AssertionError, match="no index-backed node carried the 5 in-window"):
        _assert_bounded_read(plan, expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)


def test_the_failure_message_does_not_dump_the_whole_plan() -> None:
    with pytest.raises(AssertionError) as failure:
        _assert_bounded_read(_degraded(), expected_rows=_IN_WINDOW, limit=_BOUNDED_LIMIT)
    message = str(failure.value)
    assert len(message) < 500
    assert "speech_job_cancellation_fences" in message


def test_the_row_count_needs_a_limit_that_cannot_truncate_the_window() -> None:
    """A limit at or below the window makes the row count unable to see a truncation.

    Both a window of five rows and a truncated window of fifty report the limit at the
    top of the plan, so the assertion refuses a limit that cannot separate them instead
    of passing on a number that no longer means what it says.
    """

    for limit in (_IN_WINDOW, _IN_WINDOW - 1):
        with pytest.raises(AssertionError, match="must fit strictly inside limit"):
            _assert_bounded_read(_fence_driven(), expected_rows=_IN_WINDOW, limit=limit)
