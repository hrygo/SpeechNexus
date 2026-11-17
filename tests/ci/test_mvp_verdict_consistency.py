"""Keep the MVP verdict table from claiming a tier its evidence does not have.

`docs/plans/mvp-release-readiness.md` §2 is the release verdict: a row whose 判定 says
「达标」 is a promise that an A-tier record exists, and the `A 档` column is where that
record is counted. #953 found rows that said 达标 while the tier column still said 无 or
C. The table is hand-written prose, so this module parses it instead of trusting a
reader to notice the contradiction:

* a 达标 row must claim A-tier evidence in `A 档` (`A…` or `有…`, not `无`/`B`/`C`);
* an `A 档` cell of 无 must not sit on a 达标 row;
* the §2 table is the 12 MVP rows, numbered 1..12 in order, seven cells wide.

The first test runs on the real document. The remaining tests run the same checks on
synthetic tables, so the red side is proven in CI and not only by a one-off mutation.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "docs/plans/mvp-release-readiness.md"
SECTION = "## §2 判定表"
SECTION_END = "## §3"
TIER_COLUMN = "A 档"
VERDICT_COLUMN = "判定"
ROW_NUMBER = re.compile(r"^\|\s*(\d{1,2})\s*\|")
PIPE = re.compile(r"(?<!\\)\|")
A_CLAIM = re.compile(r"^(?:\*\*)?(?:A|有)")
NO_A_CLAIM = re.compile(r"^(?:\*\*)?(?:无|B|C)(?![\w])")
MVP_ROWS = tuple(range(1, 13))
HEADER_WIDTH = 7


def split_cells(line: str) -> list[str]:
    """Split one Markdown table line; a ``\\|`` escape stays inside its cell."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip().replace("\\|", "|") for cell in PIPE.split(body)]


def parse(text: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    start = text.index(SECTION)
    end = text.index(SECTION_END, start)
    header: list[str] = []
    rows: list[tuple[int, list[str]]] = []
    for line in text[start:end].splitlines():
        if not line.startswith("|"):
            continue
        parsed = split_cells(line)
        if not header:
            header = parsed
            continue
        match = ROW_NUMBER.match(line)
        if match is not None:
            rows.append((int(match.group(1)), parsed))
    return header, rows


def shape_problems(rows: list[tuple[int, list[str]]], width: int) -> list[str]:
    """The table must be exactly the MVP rows, in order, with a stable column count."""
    problems: list[str] = []
    numbers = [number for number, _ in rows]
    if numbers != list(MVP_ROWS):
        problems.append(f"row numbers are {numbers}, expected {list(MVP_ROWS)} in order")
    for number, row in rows:
        if len(row) != width:
            problems.append(f"row {number} has {len(row)} cells, header has {width}")
    return problems


def is_done(verdict: str) -> bool:
    """`未达标` contains `达标` as a substring, so the negation is excluded here."""
    return "达标" in verdict and "未达标" not in verdict


def tier_claim_problems(header: list[str], rows: list[tuple[int, list[str]]]) -> list[str]:
    """A 达标 row must claim A-tier evidence in the `A 档` column."""
    index = header.index(TIER_COLUMN)
    verdict_index = header.index(VERDICT_COLUMN)
    problems: list[str] = []
    for number, row in rows:
        tier = row[index].replace("*", "").strip()
        verdict = row[verdict_index].replace("*", "").strip()
        if is_done(verdict) and not A_CLAIM.match(tier):
            problems.append(f"row {number}: 判定 says 达标 but A 档 is {tier!r}")
    return problems


def no_tier_done_problems(header: list[str], rows: list[tuple[int, list[str]]]) -> list[str]:
    """An `A 档` cell of 无 (or B/C) must not sit on a 达标 row."""
    index = header.index(TIER_COLUMN)
    verdict_index = header.index(VERDICT_COLUMN)
    problems: list[str] = []
    for number, row in rows:
        tier = row[index].replace("*", "").strip()
        verdict = row[verdict_index].replace("*", "").strip()
        if NO_A_CLAIM.match(tier) and is_done(verdict):
            problems.append(f"row {number}: A 档 is {tier!r} but 判定 says 达标")
    return problems


class MvpVerdictTableTests(unittest.TestCase):
    def test_the_real_table_is_complete_and_has_no_contradiction(self) -> None:
        header, rows = parse(DOCUMENT.read_text(encoding="utf-8"))
        self.assertEqual(shape_problems(rows, HEADER_WIDTH), [])
        self.assertEqual(tier_claim_problems(header, rows), [])
        self.assertEqual(no_tier_done_problems(header, rows), [])

    def test_a_done_row_without_a_tier_claim_is_rejected(self) -> None:
        header = ["#", TIER_COLUMN, VERDICT_COLUMN]
        rows = [
            (1, ["1", "**无**", "**达标**"]),
            (2, ["2", "**C**", "**达标**"]),
            (3, ["3", "**有（窄口径）**", "**达标**"]),
            (4, ["4", "**B + C**", "未达标"]),
        ]
        self.assertEqual(
            tier_claim_problems(header, rows),
            [
                "row 1: 判定 says 达标 but A 档 is '无'",
                "row 2: 判定 says 达标 but A 档 is 'C'",
            ],
        )
        self.assertEqual(
            no_tier_done_problems(header, rows),
            [
                "row 1: A 档 is '无' but 判定 says 达标",
                "row 2: A 档 is 'C' but 判定 says 达标",
            ],
        )

    def test_a_table_that_is_not_twelve_numbered_rows_is_rejected(self) -> None:
        rows = [(number, ["x"] * HEADER_WIDTH) for number in range(1, 12)]
        self.assertEqual(len(shape_problems(rows, HEADER_WIDTH)), 1)
        swapped = [(2, ["x"] * HEADER_WIDTH), (1, ["x"] * HEADER_WIDTH)]
        self.assertEqual(len(shape_problems(swapped, HEADER_WIDTH)), 1)
        narrow = [(number, ["x"] * 6) for number in range(1, 13)]
        self.assertEqual(len(shape_problems(narrow, HEADER_WIDTH)), 12)


if __name__ == "__main__":
    unittest.main()
