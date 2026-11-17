"""Prove the documented-capability guard goes red from either side of a claim.

`scripts/dev-env/check-doc-statements.py` pins statements that already drifted once
(#823). A guard nobody has watched fail proves nothing, so every case below mutates a
copy of the checkout - once on the document side and once on the code side - and
requires the guard to reject it. The first case proves the real checkout passes.

The copies are made because a red-guard test must not touch the repository it runs
in; only the files the guard reads are copied.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/dev-env/check-doc-statements.py"
JOBS_ROUTER = "backend/src/speechnexus_backend/speech/interfaces/http/jobs.py"
APP = "backend/src/speechnexus_backend/app.py"
DISPATCH_RUNTIME = "backend/src/speechnexus_backend/job_dispatch_runtime.py"
FILE_WORKER = "execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py"
COMPLETION = "execution-gateway/src/speechnexus_gateway/application/execution_completion.py"
PRD = "docs/prd.md"
COMPOSE = "docs/operations/compose-minimal.md"
DEPLOY_README = "deploy/README.md"
MANIFEST = "docs/plans/delivery-issues.json"
RUNBOOK = "docs/operations/async-job-runbook.md"
CATALOG_RUNTIME = "backend/src/speechnexus_backend/model_catalog_runtime.py"
COPIED_FILES = (
    DEPLOY_README,
    PRD,
    COMPOSE,
    MANIFEST,
    RUNBOOK,
    CATALOG_RUNTIME,
    "docs/README.md",
    "docs/plans/delivery-tracking.md",
    "docs/architecture/speech-api.md",
    "docs/architecture/runtime-governance.md",
    "docs/speechnexus-product-architecture.md",
    JOBS_ROUTER,
    APP,
    DISPATCH_RUNTIME,
    FILE_WORKER,
    COMPLETION,
)
OLD_PRD_CLAIM = "当前公开 Job 路由仅装配创建与读取，取消、重试和 SSE 仍须接线验收。"
OLD_README_BULLET = (
    "- public synchronous ASR is still blocked by the missing production terminal-state\n"
    "  writer; a live request can return `504 operation_timeout` even when the gateway\n"
    "  staged a result;"
)


class DocumentedCapabilityGuardTests(unittest.TestCase):
    """Run the guard on the real checkout and on mutated copies of it."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        for relative in COPIED_FILES:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)

    def _run_guard(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / SCRIPT), "--root", str(root), "--quiet"],
            capture_output=True,
            text=True,
            check=False,
        )

    def _rewrite(self, relative: str, old: str, new: str) -> None:
        path = self.root / relative
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"mutation target is missing from {relative}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def _assert_red(self, fragment: str) -> None:
        result = self._run_guard(self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(fragment, result.stdout)

    def _assert_green(self) -> None:
        result = self._run_guard(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _rewrite_catalogue_comment(self, replacement: str | None) -> None:
        """Replace the catalogue's assembly comment, keeping its first line or not."""
        path = self.root / CATALOG_RUNTIME
        text = path.read_text(encoding="utf-8")
        first = (
            "                # Static, fail-closed projection, never a live admission "
            "answer. This\n"
        )
        construction = "                self.catalogue = PostgresModelCatalog(\n"
        start = text.index(first)
        end = text.index(construction)
        self.assertGreater(end, start, "the assembly comment moved or disappeared")
        kept = first if replacement is None else replacement
        path.write_text(text[:start] + kept + text[end:], encoding="utf-8")

    def test_the_real_checkout_passes(self) -> None:
        result = self._run_guard(ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_route_removed_from_the_router_turns_red(self) -> None:
        self._rewrite(
            JOBS_ROUTER,
            '    @router.post(PREFIX + "/{job_id}/retries", operation_id="retryJob")\n',
            "",
        )
        self._assert_red("POST /v1/nexus/jobs/{job_id}/retries")

    def test_a_route_missing_from_the_prd_turns_red(self) -> None:
        self._rewrite(
            JOBS_ROUTER,
            '    @router.get(PREFIX, operation_id="listJobs")\n',
            '    @router.delete(PREFIX, operation_id="deleteJob")\n',
        )
        self._assert_red("DELETE /v1/nexus/jobs")

    def test_an_unmounted_job_router_turns_red(self) -> None:
        self._rewrite(APP, "        create_job_router(", "        create_transcription_router(")
        self._assert_red("no longer mounts create_job_router")

    def test_the_old_prd_route_claim_turns_red(self) -> None:
        self._rewrite(
            PRD,
            (
                "公开 Job 路由已装配六个操作：创建（`POST /v1/nexus/jobs`）、"
                "列表（`GET /v1/nexus/jobs`）、读取（`GET /v1/nexus/jobs/{job_id}`）、"
                "取消（`POST /v1/nexus/jobs/{job_id}/cancellations`）、"
                "重试（`POST /v1/nexus/jobs/{job_id}/retries`）与 SSE 事件流"
                "（`GET /v1/nexus/jobs/{job_id}/events`），注册在 "
                "`backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:819` 起，"
                "并由 `backend/src/speechnexus_backend/app.py:819` 装配进应用；"
                "取消、重试与事件流另有隔离 PG 与真实 TCP 测试，这些路由尚未在参考部署上实际调用。"
            ),
            OLD_PRD_CLAIM,
        )
        self._assert_red("public-job-routes-prd")

    def test_the_old_terminal_writer_claim_turns_red(self) -> None:
        text = (self.root / DEPLOY_README).read_text(encoding="utf-8")
        start = text.index("- public synchronous ASR is assembled but not exercised")
        end = text.index("- public synchronous TTS is assembled here")
        (self.root / DEPLOY_README).write_text(
            text[:start] + OLD_README_BULLET + "\n" + text[end:], encoding="utf-8"
        )
        self._assert_red("missing production terminal-state")

    def test_a_blanket_unverified_cell_turns_red(self) -> None:
        self._rewrite(COMPOSE, "**已装配、已实现；本部署无公开 `200` 证据**", "**本部署未验证**")
        self._assert_red("**本部署未验证**")

    def test_a_moved_terminal_write_line_turns_red(self) -> None:
        indent = " " * 12
        stop = "RecordStop(evidence, ExecutionState.SUCCEEDED)"
        terminal = f"{indent}return self._ledger.record_stop({stop})\n"
        renamed = f"{indent}return self._ledger.confirm({stop})\n"
        self._rewrite(COMPLETION, terminal, renamed)
        self._assert_red("record_stop(")

    def test_the_stale_planner_claim_turns_red(self) -> None:
        self._rewrite(MANIFEST, "生产 planner 与结果发布器已装配", "生产 planner 未装配")
        self._assert_red("生产 planner 未装配")

    def test_the_live_readiness_wording_turns_red(self) -> None:
        """D12: the runbook read the readiness block as live state (#876)."""
        self._rewrite(
            RUNBOOK,
            "是**静态投影**（fail-closed），不是实时状态",
            "反映实时状态",
        )
        self._assert_red("catalogue-readiness-projection")

    def test_dropping_the_structural_mismatch_turns_red(self) -> None:
        """The "wait for live facts" reading is what shipped the contract defect (#941)."""
        self._rewrite(
            RUNBOOK,
            "**二者结构性不符，接入多少实时事实都满足不了该定义**",
            "接入实时事实之后就对上了",
        )
        self._assert_red("二者结构性不符")

    def test_a_deleted_projection_comment_turns_red(self) -> None:
        """The comment argument is what keeps the projection static; deleting it reverts."""
        self._rewrite(
            CATALOG_RUNTIME,
            "                # Static, fail-closed projection, never a live admission "
            "answer. This\n",
            "",
        )
        self._assert_red("catalogue-readiness-projection-comment")

    def test_a_comment_trimmed_to_its_first_line_turns_red(self) -> None:
        """Keeping the headline while deleting the READABLE argument is a revert (#939 M6)."""
        self._rewrite_catalogue_comment(None)
        self._assert_red("reader's grant admits published tables only (READABLE)")

    def test_a_comment_replaced_by_one_line_turns_red(self) -> None:
        """`Readiness is not wired yet.` drops the workspace dimension (#939 M7)."""
        self._rewrite_catalogue_comment("                # Readiness is not wired yet.\n")
        self._assert_red("no workspace (models.py:42)")

    def test_a_contradicting_sentence_turns_red(self) -> None:
        """A correct paragraph with one reverse assertion added (#939 M5)."""
        self._rewrite(
            RUNBOOK,
            "**不是待接入项**。",
            "**不是待接入项**。实测：实例 `ready` 后本端点会上报 `available`。",
        )
        self._assert_red("实例 `ready` 后本端点会上报 `available`")

    def test_a_contradiction_elsewhere_in_the_document_turns_red(self) -> None:
        """The same reversal parked outside the paragraph (#939 M8)."""
        self._rewrite(
            RUNBOOK,
            "**不是待接入项**。",
            "**不是待接入项**。实例变成 `ready` 后该字段会跟着变。",
        )
        self._assert_red("实例变成 `ready` 后该字段会跟着变")

    def test_moving_the_comment_down_a_line_stays_green(self) -> None:
        """A citation that shifts is not a changed claim; the anchor is READABLE (#939 M3)."""
        self._rewrite(
            CATALOG_RUNTIME,
            "                # Static, fail-closed projection, never a live admission "
            "answer. This\n",
            "                # (assembly note)\n"
            "                # Static, fail-closed projection, never a live admission "
            "answer. This\n",
        )
        self._assert_green()

    def test_a_negated_reference_to_the_stale_wording_stays_green(self) -> None:
        """Forbidding whole asserting clauses must not reject a negation (#939 M4)."""
        self._rewrite(
            RUNBOOK,
            "**不是待接入项**。",
            "**不是待接入项**。本端点不是反映实时状态的通道。",
        )
        self._assert_green()
