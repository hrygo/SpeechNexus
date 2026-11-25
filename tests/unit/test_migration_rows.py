"""`project_onto` 自己的判据:退役列按登记剔除,未登记的缺失列必须报错."""

from __future__ import annotations

import pytest

from tests.support.migration_rows import RETIRED_COLUMNS, project_onto

_RETIRED = RETIRED_COLUMNS["coordination_jobs"]


def test_a_registered_retirement_is_dropped_from_both_sides() -> None:
    before = [
        {"job_id": "job-1", "bucket_no": 7, "claim_expires_at_us": 0, "claim_expires_at": None},
        {
            "job_id": "job-2",
            "bucket_no": 9,
            "claim_expires_at_us": 1_800_000_000_000_000,
            "claim_expires_at": None,
        },
    ]
    after = [
        {"job_id": "job-1", "bucket_no": 7, "claim_expires_at": None},
        {"job_id": "job-2", "bucket_no": 9, "claim_expires_at": None},
    ]

    assert project_onto(after, before, retired=_RETIRED) == project_onto(
        before, before, retired=_RETIRED
    )


def test_a_column_that_is_not_registered_as_retired_fails_loudly() -> None:
    before = [{"job_id": "job-1", "bucket_no": 7, "claim_expires_at_us": 0}]
    gone = [{"job_id": "job-1"}]

    with pytest.raises(AssertionError, match="bucket_no"):
        project_onto(gone, before, retired=_RETIRED)


def test_a_changed_value_still_differs() -> None:
    before = [{"job_id": "job-1", "bucket_no": 7, "claim_expires_at_us": 0}]
    after = [{"job_id": "job-1", "bucket_no": 8}]

    assert project_onto(after, before, retired=_RETIRED) != project_onto(
        before, before, retired=_RETIRED
    )
