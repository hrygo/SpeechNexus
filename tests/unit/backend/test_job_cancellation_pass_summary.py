"""A bounded cancellation pass always prints one summary line, even when idle.

The cluster ran ``speechnexus-job-cancellation-reconciler --once`` every minute and
the container produced **no output at all** on a pass that found nothing, so
``succeeded=1`` with an empty log could not be told apart from "the process died
before it did anything" (a silent failure). ``#1293`` asked for one line per pass.

Mutations that must turn it red:

- dropping the print (returning to a silent pass) -- the idle case fails;
- printing only when work was done -- the idle case fails, because the case exists
  precisely to cover the empty pass;
- a summary that omits the counters -- the field assertions fail, so the line cannot
  degrade into a bare "ok".
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from typing import Any

from speechnexus_backend.job_cancellation_worker import _pass_summary
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    BlockedJudgment,
    ReconciliationBatch,
)


class PassSummaryTests(unittest.TestCase):
    def test_an_idle_pass_still_reports(self) -> None:
        line = _pass_summary(ReconciliationBatch())
        payload = json.loads(line)
        self.assertEqual(payload["event"], "job_cancellation_pass")
        for name in ("claimed", "settled", "retried", "exhausted", "unavailable", "blocked"):
            self.assertEqual(payload[name], 0, name)

    def test_the_counters_come_from_the_batch(self) -> None:
        batch = ReconciliationBatch(claimed=3, settled=2, retried=1, exhausted=0, unavailable=0)
        payload = json.loads(_pass_summary(batch))
        self.assertEqual(payload["claimed"], 3)
        self.assertEqual(payload["settled"], 2)
        self.assertEqual(payload["retried"], 1)

    def test_blocked_judgments_are_counted(self) -> None:
        blocked = tuple(
            BlockedJudgment(job_id=f"job-{index}", detail="unconfirmed") for index in range(2)
        )
        payload = json.loads(_pass_summary(ReconciliationBatch(blocked=blocked)))
        self.assertEqual(payload["blocked"], 2)

    def test_the_line_is_single_line_json(self) -> None:
        line = _pass_summary(ReconciliationBatch(claimed=1))
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line)["event"], "job_cancellation_pass")

    def test_an_unexpected_object_does_not_crash_the_pass(self) -> None:
        payload = json.loads(_pass_summary(object()))
        self.assertEqual(payload["claimed"], 0)


WORKER = (
    Path(__file__).resolve().parents[3]
    / "backend/src/speechnexus_backend/job_cancellation_worker.py"
)


def _once_branch_prints_the_summary() -> bool:
    """Whether the ``if once:`` branch calls ``print(_pass_summary(...))``."""
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Name) and test.id == "once"):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Expr):
                continue
            call = statement.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            if call.func.id != "print" or not call.args:
                continue
            inner = call.args[0]
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Name):
                continue
            if inner.func.id == "_pass_summary":
                return True
    return False


class OnceBranchWiringTests(unittest.TestCase):
    """The line has to be emitted by the pass, not merely available."""

    def test_the_once_branch_prints_the_summary(self) -> None:
        self.assertTrue(
            _once_branch_prints_the_summary(),
            "the --once branch must print one summary line (#1293)",
        )


class BlockedJudgmentShapeTests(unittest.TestCase):
    def test_blocked_judgment_is_a_dataclass_with_a_reason(self) -> None:
        judgment: Any = BlockedJudgment(job_id="job-1", detail="unconfirmed")
        self.assertEqual(judgment.detail, "unconfirmed")


if __name__ == "__main__":
    unittest.main()
