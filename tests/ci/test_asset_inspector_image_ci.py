"""Exercise asset-inspector image routing and its fail-closed gate without Docker."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/asset-inspector-image.yml").read_text()
CI = (ROOT / ".github/workflows/ci.yml").read_text()
RUNNERS = ["ubuntu-24.04", "ubuntu-24.04-arm"]


def step_script(workflow: str, name: str) -> str:
    step = workflow.split(f"      - name: {name}\n", 1)[1]
    body = step.split("        run: |\n", 1)[1]
    lines = []
    for line in body.splitlines(keepends=True):
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    return textwrap.dedent("".join(lines))


def execute(
    script: str, env: dict[str, str], *, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


SCOPE = step_script(WORKFLOW, "Check image inputs")
GATE = step_script(WORKFLOW, "Require a required scope and a successful matrix")


def trigger_paths() -> set[str]:
    block = WORKFLOW.split("  pull_request:\n    paths:\n", 1)[1].split("\n  push:\n", 1)[0]
    return {line.strip()[2:].strip().strip('"') for line in block.splitlines() if line.strip()}


def scope_patterns() -> set[str]:
    block = SCOPE.split('case "$path" in\n', 1)[1].split("\n            esac", 1)[0]
    arm = block.split(")", 1)[0]
    return {item.strip() for item in arm.replace("\\\n", "|").split("|") if item.strip()}


class WorkflowShapeTests(unittest.TestCase):
    def test_workflow_is_standalone_and_gates_both_native_runners(self) -> None:
        self.assertTrue(WORKFLOW.startswith("name: Asset inspector image\n"))
        self.assertNotIn("workflow_call:", WORKFLOW)
        self.assertNotIn("asset-inspector-image.yml", CI)
        self.assertIn("  pull_request:\n    paths:\n", WORKFLOW)
        self.assertIn('  push:\n    branches: ["main"]\n', WORKFLOW)
        self.assertIn("  workflow_dispatch:\n", WORKFLOW)
        self.assertIn("permissions:\n  contents: read\n", WORKFLOW)
        self.assertIn("runner: [ubuntu-24.04, ubuntu-24.04-arm]", WORKFLOW)
        self.assertIn("fail-fast: false", WORKFLOW)
        self.assertIn("timeout-minutes: 20", WORKFLOW)
        self.assertIn("if: ${{ needs.scope.outputs.required == 'true' }}", WORKFLOW)
        self.assertIn("needs: [scope, image]", WORKFLOW)
        self.assertIn("if: ${{ always() }}", WORKFLOW)
        self.assertIn("SCOPE_RESULT: ${{ needs.scope.result }}", WORKFLOW)
        self.assertIn("REQUIRED: ${{ needs.scope.outputs.required }}", WORKFLOW)
        self.assertIn("IMAGE_RESULT: ${{ needs.image.result }}", WORKFLOW)

    def test_every_action_is_pinned_to_a_commit_sha(self) -> None:
        uses = [
            line.split("uses:", 1)[1].strip() for line in WORKFLOW.splitlines() if "uses:" in line
        ]
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")


class RoutingConsistencyTests(unittest.TestCase):
    def test_trigger_paths_and_scope_patterns_describe_the_same_inputs(self) -> None:
        # `on.pull_request.paths` is the independent routing signal the gate
        # relies on, so the two declarations must not drift: every `dir/**`
        # trigger is one shell `dir/*` arm and every exact file is itself.
        expected = {
            path[: -len("/**")] + "/*" if path.endswith("/**") else path for path in trigger_paths()
        }
        self.assertEqual(expected, scope_patterns())

    def test_gate_truth_table(self) -> None:
        for scope in ("success", "failure", "cancelled", "skipped", ""):
            for required in ("true", "false", "", "TRUE", "invalid"):
                for image in ("success", "failure", "cancelled", "skipped", ""):
                    with self.subTest(scope=scope, required=required, image=image):
                        result = execute(
                            GATE,
                            {
                                "SCOPE_RESULT": scope,
                                "REQUIRED": required,
                                "IMAGE_RESULT": image,
                                "EVENT_NAME": "pull_request",
                            },
                        )
                        valid = scope == "success" and required == "true" and image == "success"
                        self.assertEqual(
                            result.returncode == 0, valid, result.stdout + result.stderr
                        )

    def test_gate_rejects_a_skipped_image_on_a_triggered_run(self) -> None:
        for event in ("pull_request", "push", "workflow_dispatch"):
            with self.subTest(event=event):
                result = execute(
                    GATE,
                    {
                        "SCOPE_RESULT": "success",
                        "REQUIRED": "false",
                        "IMAGE_RESULT": "skipped",
                        "EVENT_NAME": event,
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only starts on build inputs", result.stdout)


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="asset-inspector-image-route-")
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)
        self.repo = self.temp_path / "repo"
        self.repo.mkdir()
        self.git_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "-q")
        self.write("asset-inspector/source.py", "inspector\n")
        self.write("docs/source.md", "docs\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=CI",
                "-c",
                "user.email=ci@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=self.repo,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def write(self, name: str, value: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def commit(self, name: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", name)

    def route(self, event: str = "pull_request", base: str | None = None) -> tuple[int, str]:
        output = self.temp_path / "output.txt"
        output.write_text("")
        result = execute(
            SCOPE,
            {
                **self.git_env,
                "GITHUB_OUTPUT": str(output),
                "RUNNER_TEMP": str(self.temp_path),
                "EVENT_NAME": event,
                "BASE_SHA": self.base if base is None else base,
            },
            cwd=self.repo,
        )
        return result.returncode, output.read_text()

    def test_unrelated_docs_skip_image(self) -> None:
        self.write("docs/source.md", "changed\n")
        self.commit("docs")
        self.assertEqual(self.route(), (0, "required=false\n"))

    def test_image_inputs_require_a_build(self) -> None:
        for name in (
            "asset-inspector/Dockerfile",
            "asset-inspector/src/speechnexus_asset_inspector/app.py",
            "packages/media/src/speechnexus_media/inspection.py",
            "deploy/images/verify-asset-inspector-image.py",
            "tests/ci/test_asset_inspector_image_ci.py",
            ".dockerignore",
            "uv.lock",
            "pyproject.toml",
        ):
            with self.subTest(path=name):
                self.write(name, "change\n")
                self.commit("change")
                self.assertEqual(self.route(), (0, "required=true\n"))
                self.base = self.git("rev-parse", "HEAD").strip()

    def test_empty_diff_and_non_pr_events_require_build(self) -> None:
        self.assertEqual(self.route(), (0, "required=true\n"))
        for event in ("push", "workflow_dispatch", "unknown"):
            with self.subTest(event=event):
                self.assertEqual(self.route(event, base=""), (0, "required=true\n"))

    def test_invalid_or_unavailable_base_fails_closed(self) -> None:
        for base in ("", "0" * 40, "invalid", "a" * 40):
            with self.subTest(base=base):
                code, output = self.route(base=base)
                self.assertNotEqual(code, 0)
                self.assertNotIn("required=false", output)


if __name__ == "__main__":
    unittest.main()
