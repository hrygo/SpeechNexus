"""Pin the `reference_acceptance` job's private-input preparation step.

The inline Python in `.github/workflows/ci.yml` derives the private mount names from
`deploy/compose.yaml` and satisfies each one from `deploy/private/<name>.example.json`.
A mount with no committed example used to be downgraded to a `::warning::` plus a
synthesized `{}` placeholder, which let the stack step's private input scan (JSON
parse, result-bucket agreement, VAD digest) pass on an empty document: the file was
readable, therefore accepted. That is a gate that opened a hole for itself, so the
step must fail instead - and these cases keep it failing.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
COMPOSE = ROOT / "deploy/compose.yaml"
EXAMPLES = ROOT / "deploy/private"

STEP_NAME = "Prepare the acceptance environment from the committed example"
PRIVATE_MOUNT = re.compile(r"/run/speechnexus-private/([A-Za-z0-9._-]+)")


def _prepare_script() -> str:
    """The inline Python of the preparation step, dedented out of the YAML block."""
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = f"      - name: {STEP_NAME}\n"
    assert marker in text, f"ci.yml 里找不到步骤 `{STEP_NAME}`"
    after = text[text.index(marker) + len(marker) :]
    end = after.index("\n      - name: ")
    body = after[:end]
    opener = "python3 - <<'PY'\n"
    assert opener in body, "准备步骤里找不到 inline Python 的 heredoc"
    script = body[body.index(opener) + len(opener) :]
    return textwrap.dedent(script[: script.index("\n          PY\n")])


def _write_workspace(root: Path, mounts: tuple[str, ...], examples: tuple[str, ...]) -> None:
    deploy = root / "deploy"
    (deploy / "private").mkdir(parents=True)
    (deploy / "compose.env.example").write_text(
        "SPEECHNEXUS_PRIVATE_CONFIG_DIR=./.runtime/private\n", encoding="utf-8"
    )
    lines = ["services:", "  gateway:", "    volumes:"]
    for mount in mounts:
        lines.append(f"      - /run/speechnexus-private/{mount}:/run/x/{mount}:ro")
    (deploy / "compose.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for example in examples:
        (deploy / "private" / example).write_text(
            '{"vad_model_file": "/run/speechnexus-private/vad.onnx", "vad_model_sha256": "00"}\n',
            encoding="utf-8",
        )


class PrepareStepTests(unittest.TestCase):
    def _run(
        self, mounts: tuple[str, ...], examples: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        script = _prepare_script()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, mounts, examples)
            (root / "prepare.py").write_text(script, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(root / "prepare.py")],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_a_mount_with_a_committed_example_is_satisfied(self) -> None:
        result = self._run(("gateway.json",), ("gateway.example.json",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("::warning::", result.stdout)

    def test_a_mount_without_a_committed_example_fails_the_job(self) -> None:
        result = self._run(("orphan.json",), ())
        self.assertNotEqual(result.returncode, 0, "缺 committed 示例必须判红，不能只警告")
        message = result.stdout + result.stderr
        self.assertIn("orphan.json", message)
        self.assertIn("orphan.example.json", message)
        self.assertNotIn("::warning::", message, "降级警告不是通过条件")

    def test_a_placeholder_is_never_synthesized(self) -> None:
        script = _prepare_script()
        self.assertNotIn("::warning::", script, "缺示例不许降级成警告")
        self.assertIn("raise SystemExit", script, "缺示例必须让步骤失败")

    def test_a_compose_without_private_mounts_is_red(self) -> None:
        result = self._run((), ())
        self.assertNotEqual(result.returncode, 0, "没有私有挂载时无法构造输入，不能静默通过")
        self.assertIn("speechnexus-private", result.stdout + result.stderr)

    def test_every_checked_in_mount_has_a_committed_example(self) -> None:
        code_lines = [
            line
            for line in COMPOSE.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        targets = set(PRIVATE_MOUNT.findall("\n".join(code_lines)))
        self.assertTrue(targets, "deploy/compose.yaml 应当至少挂载一个私有输入文件")
        missing = [
            name
            for name in sorted(targets)
            if not (EXAMPLES / f"{name.removesuffix('.json')}.example.json").is_file()
        ]
        self.assertEqual(missing, [], f"这些私有挂载缺少 committed 示例: {missing}")


if __name__ == "__main__":
    unittest.main()
