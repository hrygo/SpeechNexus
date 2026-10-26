"""Ranges select exact bytes, including decimal inputs larger than Python's int limit."""

import pytest

from speechnexus_backend.workbench.application.downloads import DownloadError, select_range


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, (200, 0, 8)),
        ("bytes=0-3", (206, 0, 4)),
        ("bytes=3-", (206, 3, 5)),
        ("bytes=-3", (206, 5, 3)),
        ("bytes=-90", (206, 0, 8)),
        ("bytes=3-90", (206, 3, 5)),
        ("bytes=0003-0005", (206, 3, 3)),
        ("bytes=-" + "9" * 5000, (206, 0, 8)),
    ],
    ids=["full", "closed", "open", "suffix", "large-suffix", "clamped-end", "zeros", "huge"],
)
def test_ranges(header: str | None, expected: tuple[int, int, int]) -> None:
    assert select_range(header, 8) == expected


@pytest.mark.parametrize(
    "header", ["bytes=-", "bytes=4-3", "bytes=0-1,3-4", "bytes=+1-2", "items=0-1"]
)
def test_malformed_range(header: str) -> None:
    with pytest.raises(DownloadError, match="invalid_request"):
        select_range(header, 8)


@pytest.mark.parametrize(
    "header",
    ["bytes=8-", "bytes=-0", "bytes=" + "9" * 5000 + "-"],
    ids=["past-end", "zero-suffix", "huge-start"],
)
def test_unsatisfiable_range(header: str) -> None:
    with pytest.raises(DownloadError, match="range_not_satisfiable"):
        select_range(header, 8)
