"""statement manifest guard:摘要、已登记 profile 与参数形状的注入式反例."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-statement-manifest.py"
STATEMENTS = ROOT / "packages/portable-sql/statements"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


class StatementManifestGuardTest(unittest.TestCase):
    def test_selftest_proves_every_drift_is_rejected(self) -> None:
        result = run("--selftest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selftest OK", result.stdout)

    def test_committed_manifest_matches_its_digest(self) -> None:
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT OK", result.stdout)

    def test_a_shape_change_without_a_digest_bump_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = work / "manifest.json"
            payload = json.loads((STATEMENTS / "manifest.json").read_text(encoding="utf-8"))
            payload["statements"][0]["result"] = payload["statements"][0]["result"][:2]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            digest_file = work / "manifest.digest"
            shutil.copy(STATEMENTS / "manifest.digest", digest_file)

            result = run(
                "--manifest",
                str(manifest),
                "--profiles",
                str(STATEMENTS / "profiles.json"),
                "--digest-file",
                str(digest_file),
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("digest changed", result.stderr)

    def test_an_unregistered_profile_variant_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = work / "manifest.json"
            payload = json.loads((STATEMENTS / "manifest.json").read_text(encoding="utf-8"))
            payload["profiles"] = [
                {
                    "profile_id": "oracle-19",
                    "statement_id": "authority.probe",
                    "sql": payload["statements"][0]["sql"],
                    "rationale": "test",
                    "evidence": "test",
                }
            ]
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            result = run(
                "--manifest",
                str(manifest),
                "--profiles",
                str(STATEMENTS / "profiles.json"),
                "--digest-file",
                str(STATEMENTS / "manifest.digest"),
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("unregistered profile", result.stderr)

    def test_a_variant_that_changes_the_parameter_count_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = work / "manifest.json"
            payload = json.loads((STATEMENTS / "manifest.json").read_text(encoding="utf-8"))
            payload["profiles"] = [
                {
                    "profile_id": "postgresql-16",
                    "statement_id": "authority.probe",
                    "sql": (
                        "SELECT CURRENT_TIMESTAMP FROM schema_meta "
                        "WHERE meta_id = ? AND meta_id = ?"
                    ),
                    "rationale": "test",
                    "evidence": "test",
                }
            ]
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            result = run(
                "--manifest",
                str(manifest),
                "--profiles",
                str(STATEMENTS / "profiles.json"),
                "--digest-file",
                str(STATEMENTS / "manifest.digest"),
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("parameter shape", result.stderr)

    def test_authority_probe_sql_must_equal_the_executable_statement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = work / "manifest.json"
            payload = json.loads((STATEMENTS / "manifest.json").read_text(encoding="utf-8"))
            payload["statements"][0]["sql"] = (
                "SELECT CURRENT_TIMESTAMP, schema_rev FROM schema_meta WHERE meta_id = ?"
            )
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            result = run(
                "--manifest",
                str(manifest),
                "--profiles",
                str(STATEMENTS / "profiles.json"),
                "--digest-file",
                str(STATEMENTS / "manifest.digest"),
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("drifted", result.stderr)

    def test_write_digest_updates_the_committed_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            digest_file = work / "manifest.digest"
            digest_file.write_text("0" * 64 + "\n", encoding="utf-8")
            result = run(
                "--profiles",
                str(STATEMENTS / "profiles.json"),
                "--digest-file",
                str(digest_file),
                "--write-digest",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            written = digest_file.read_text(encoding="utf-8").strip()

        self.assertEqual(len(written), 64)
        self.assertEqual(
            written,
            (STATEMENTS / "manifest.digest").read_text(encoding="utf-8").strip(),
        )


if __name__ == "__main__":
    unittest.main()
