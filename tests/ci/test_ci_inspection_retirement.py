"""The retired bwrap CI rig stays retired until a consumer needs it again.

Standard library only: this module runs in the dependency-free routing lane.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))
CI = (ROOT / ".github/workflows/ci.yml").read_text()
PYTHON_JOB = CI.split("  python_checks:\n")[1].split("  python_contracts:\n")[0]
PROBE_NAME = "Verify media decoding dependencies before complete regression"


class RetiredInspectionCase(unittest.TestCase):
    def test_no_workflow_carries_bubblewrap_or_apparmor_infrastructure(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text()
            for marker in ("bubblewrap", "bwrap", "apparmor", "ci-inspection-profile"):
                with self.subTest(workflow=path.name, marker=marker):
                    self.assertFalse(marker in text, f"{marker} returned to {path.name}")

    def test_the_policy_script_and_its_receipt_artifacts_are_gone(self) -> None:
        self.assertFalse((ROOT / "scripts/dev-env/ci-inspection-profile.py").exists())
        for marker in ("inspection-policy-", "speech-inspection-profile", "AppArmor"):
            with self.subTest(marker=marker):
                self.assertFalse(marker in CI, f"{marker} returned to ci.yml")

    def test_the_real_decode_probe_stays_unconditional_in_the_required_job(self) -> None:
        self.assertIn(PROBE_NAME, PYTHON_JOB)
        probe = PYTHON_JOB.split(PROBE_NAME, 1)[1].split("- name: ", 1)[0]
        self.assertIn(
            "tests/unit/media/test_audio_inspection.py"
            "::test_real_decode_binds_complete_source_facts_to_digest_without_pcm",
            probe,
        )
        self.assertNotIn("if:", probe)
        self.assertNotIn("continue-on-error", probe)
        self.assertNotIn("continue-on-error", PYTHON_JOB)

    def test_the_complete_coverage_gate_and_media_toolchain_remain(self) -> None:
        self.assertIn("scripts/dev-env/python-coverage.py run", PYTHON_JOB)
        self.assertIn("--workers 2", PYTHON_JOB)
        self.assertIn("uv run python scripts/dev-env/media-smoke.py", PYTHON_JOB)
        self.assertIn("uv build --all-packages", PYTHON_JOB)
        self.assertIn("runs-on: ubuntu-24.04", PYTHON_JOB)
        self.assertNotIn("strategy:", PYTHON_JOB)


if __name__ == "__main__":
    unittest.main()
