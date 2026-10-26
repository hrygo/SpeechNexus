"""Run routing regressions with the runner's Python, without project dependencies."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/ci-routing.py"
SPEC = importlib.util.spec_from_file_location("ci_routing", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER)


class PathRoutingTests(unittest.TestCase):
    def test_only_approved_prose_is_lightweight(self) -> None:
        for paths in (
            ["AGENTS.md"],
            ["README.md", "README.en.md"],
            ["docs/architecture/design.md", "docs/含空格 的说明.md"],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(ROUTER.classify(paths), "docs")

    def test_web_source_and_prose_keep_the_web_lane(self) -> None:
        self.assertEqual(
            ROUTER.classify(["web/workbench/src/App.tsx", "web/shared/tokens.css", "README.md"]),
            "web",
        )

    def test_skill_documents_and_metadata_are_lightweight(self) -> None:
        paths = [
            ".agents/skills/delivery/SKILL.md",
            ".agents/skills/delivery/references/evidence.md",
            ".agents/skills/delivery/agents/openai.yaml",
            ".agents/skills/delivery/agents/alternate.yml",
            ".agents/skills/delivery/evals/evals.json",
            ".agents/skills/delivery/evals/fixtures.md",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "docs")
        self.assertEqual(ROUTER.classify([*paths, "README.md"]), "docs")
        self.assertEqual(ROUTER.classify([*paths, "web/admin/src/App.tsx"]), "web")
        self.assertEqual(ROUTER.classify([*paths, "backend/module.py"]), "full")

    def test_skill_executables_unknown_files_and_other_agent_config_stay_full(self) -> None:
        for path in (
            ".agents/skills/delivery/scripts/check.py",
            ".agents/skills/delivery/scripts/check.sh",
            ".agents/skills/delivery/scripts/check.mjs",
            ".agents/skills/delivery/data.bin",
            ".agents/skills/SKILL.md",
            ".agents/settings.json",
            ".agents/skills-other/delivery/SKILL.md",
            ".agents/skills/delivery/../settings.yaml",
        ):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "full")

    def test_unknown_or_shared_inputs_require_every_lane(self) -> None:
        for path in (
            ".github/workflows/ci.yml",
            "scripts/dev-env/ci-routing.py",
            "tests/ci/test_ci_routing.py",
            "contracts/speech-openapi.yaml",
            "packages/contracts/src/index.ts",
            "backend/src/module.py",
            "migrations/001.sql",
            "deploy/compose.yaml",
            "uv.lock",
            "pnpm-lock.yaml",
            ".nvmrc",
            "package.json",
            "web/admin/package.json",
            "web/admin/vite.config.ts",
            "docs/schema.json",
            "backend/README.md",
            "new-component/README.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify(["README.md", path]), "full")

    def test_standalone_engines_skip_only_database_checks(self) -> None:
        for path in (
            "deploy/engines/vllm-asr-lifecycle/src/speechnexus_vllm_asr/plugin.py",
            "deploy/engines/vllm-asr-lifecycle/pyproject.toml",
            "deploy/engines/cpu-vad/requirements.txt",
            "tests/unit/gateway/test_native_asr_scope.py",
            "tests/unit/gateway/test_worker_fence.py",
        ):
            for paths in (
                [path],
                ["README.md", path, "web/workbench/src/App.tsx"],
                ["web/workbench/src/App.tsx", path, "README.md"],
            ):
                with self.subTest(paths=paths):
                    self.assertEqual(ROUTER.classify(paths), "no-db")

    def test_engine_changes_cannot_hide_database_or_shared_inputs(self) -> None:
        engine = "deploy/engines/vllm-asr-lifecycle/worker-pin.json"
        for path in (
            "backend/src/module.py",
            "tests/unit/gateway/test_file_asr_command.py",
            "tests/support/coordination.py",
            "tests/integration/test_gateway_file_worker.py",
            "contracts/internal/execution-command.schema.json",
            "uv.lock",
            "pnpm-lock.yaml",
            ".github/workflows/ci.yml",
            "deploy/engines/../compose.yaml",
            "deploy/engines.py",
            "deploy/engines/unknown.py",
            "deploy/engines/new-engine/module.py",
        ):
            for paths in ([engine, path], [path, engine]):
                with self.subTest(paths=paths):
                    self.assertEqual(ROUTER.classify(paths), "full")

    def test_empty_and_noncanonical_paths_do_not_authorize_skips(self) -> None:
        for paths in ([], [""], ["/docs/a.md"], ["docs/../backend/a.md"], ["docs\\a.md"]):
            with self.subTest(paths=paths):
                self.assertEqual(ROUTER.classify(paths), "full")

    def test_script_is_compatible_with_runner_python_312(self) -> None:
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


class GitRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "# Initial\n")
        self.write("backend/module.py", "value = 1\n")
        self.base = self.commit()

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=CI",
                "-c",
                "user.email=ci@example.invalid",
                *args,
            ],
            cwd=self.root,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def write(self, name: str, content: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic routing fixture")
        return self.git("rev-parse", "HEAD")

    def test_real_diff_uses_nul_paths_and_includes_a_deleted_source(self) -> None:
        self.write("docs/one\ntwo.md", "# Notes\n")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "docs")
        (self.root / "backend/module.py").unlink()
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_rename_into_docs_does_not_hide_the_original_source(self) -> None:
        self.git("mv", "backend/module.py", "README.en.md")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_skill_diff_is_lightweight_but_source_renamed_into_skill_is_full(self) -> None:
        self.write(".agents/skills/delivery/SKILL.md", "# Delivery\n")
        self.write(".agents/skills/delivery/agents/openai.yaml", "interface: {}\n")
        self.write(".agents/skills/delivery/evals/evals.json", '{"evals": []}\n')
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "docs")
        self.git("mv", "backend/module.py", ".agents/skills/delivery/references.md")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_rename_shared_source_into_engine_still_requires_databases(self) -> None:
        self.write("deploy/engines/cpu-vad/README.md", "# Engine\n")
        self.git("mv", "backend/module.py", "deploy/engines/cpu-vad/module.py")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_real_engine_diff_skips_databases_but_main_stays_full(self) -> None:
        self.write("deploy/engines/cpu-vad/module.py", "value = 2\n")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "no-db")
        for event in ("push", "workflow_dispatch"):
            mode, _ = ROUTER.detect_route(event, self.base, self.root)
            self.assertEqual(mode, "full")

    def test_main_manual_unknown_events_and_empty_diff_are_full(self) -> None:
        self.write("README.md", "# Updated\n")
        self.commit()
        for event in ("push", "workflow_dispatch", "merge_group", ""):
            with self.subTest(event=event):
                mode, _ = ROUTER.detect_route(event, self.base, self.root)
                self.assertEqual(mode, "full")
        mode, _ = ROUTER.detect_route("pull_request", self.git("rev-parse", "HEAD"), self.root)
        self.assertEqual(mode, "full")

    def test_invalid_or_unavailable_base_falls_back_to_full(self) -> None:
        for base in ("", "0" * 40, "f" * 40, "--upload-pack=bad", "main; echo unsafe"):
            with self.subTest(base=base):
                mode, _ = ROUTER.detect_route("pull_request", base, self.root)
                self.assertEqual(mode, "full")

    def test_merge_result_is_compared_to_the_exact_target_commit(self) -> None:
        self.git("switch", "-qc", "topic")
        self.write("README.md", "# Topic notes\n")
        self.commit()
        self.git("switch", "-q", "main")
        self.write("backend/module.py", "value = 2\n")
        target = self.commit()
        self.git("merge", "--no-ff", "-qm", "Synthetic merge", "topic")
        mode, _ = ROUTER.detect_route("pull_request", target, self.root)
        self.assertEqual(mode, "docs")

    def test_cli_needs_no_site_packages_and_writes_a_closed_output(self) -> None:
        self.write("README.md", "# Updated\n")
        self.commit()
        output = self.root / "github-output"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(SCRIPT),
                "--event=pull_request",
                f"--base={self.base}",
                f"--output={output}",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "docs")
        self.assertEqual(output.read_text(), "mode=docs\n")

    def test_lightweight_route_still_rejects_bad_diffs(self) -> None:
        self.write("README.md", "# Updated with trailing space \n")
        self.commit()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--event=pull_request", f"--base={self.base}"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
