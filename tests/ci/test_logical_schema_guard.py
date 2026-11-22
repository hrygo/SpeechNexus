"""逻辑 schema guard:物理 DDL 与逻辑定义等价,删列 / 改 NULL 性 / 改种子即红."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-logical-schema.py"
DDL = ROOT / "migrations/0002_portable_contract.sql"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


class LogicalSchemaGuardTest(unittest.TestCase):
    def test_selftest_proves_every_logical_drift_is_rejected(self) -> None:
        result = run("--selftest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selftest OK", result.stdout)

    def test_committed_profile_equals_the_logical_schema(self) -> None:
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT OK", result.stdout)

    def _run_with(self, source: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(directory) / "migrations"
            migrations.mkdir()
            (migrations / "0002_portable_contract.sql").write_text(source, encoding="utf-8")
            return run("--root", directory)

    def test_dropping_a_column_is_red(self) -> None:
        result = self._run_with(
            DDL.read_text(encoding="utf-8").replace("    schema_rev VARCHAR(64) NOT NULL,\n", "")
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing-column schema_rev", result.stderr)

    def test_relaxing_nullability_is_red(self) -> None:
        result = self._run_with(
            DDL.read_text(encoding="utf-8").replace(
                "schema_rev VARCHAR(64) NOT NULL", "schema_rev VARCHAR(64)"
            )
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("nullable", result.stderr)

    def test_dropping_the_primary_key_is_red(self) -> None:
        source = DDL.read_text(encoding="utf-8").replace(
            "    authority_epoch DECIMAL(38,0) NOT NULL,\n    PRIMARY KEY (meta_id)",
            "    authority_epoch DECIMAL(38,0) NOT NULL",
        )
        result = self._run_with(source)
        self.assertEqual(result.returncode, 1)
        self.assertIn("primary key", result.stderr)

    def test_a_stale_seed_fingerprint_is_red(self) -> None:
        source = DDL.read_text(encoding="utf-8")
        fingerprint = source.split("'0002', '")[1].split("'")[0]
        result = self._run_with(source.replace(fingerprint, "0" * 64))
        self.assertEqual(result.returncode, 1)
        self.assertIn("seed-fingerprint", result.stderr)

    def test_extra_index_coverage_is_red(self) -> None:
        result = self._run_with(
            DDL.read_text(encoding="utf-8")
            + "\nCREATE INDEX idx_schema_meta_rev ON schema_meta (schema_rev);\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("index coverage", result.stderr)

    def test_alter_table_is_not_part_of_the_canonical_profile(self) -> None:
        result = self._run_with(
            DDL.read_text(encoding="utf-8") + "\nALTER TABLE schema_meta ADD x VARCHAR(8);\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("ALTER TABLE", result.stderr)


if __name__ == "__main__":
    unittest.main()
