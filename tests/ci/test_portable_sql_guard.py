"""证明可移植 SQL guard 会红:注入即失败,移除即恢复,基线不能静默放宽。

判据纪律要求「断言必须会红」。本文件对每条判据都做一次真实注入:

- 干净文件里注入 `import psycopg` / `SKIP LOCKED` / `%s` / `TEXT` → guard 变红;
- 已基线化的文件里再注入一条 → 计数增加同样变红(基线不是免死金牌);
- 移除一条基线化问题 → 报 FIXED 但仍通过(收紧不需要开关);
- `--write-baseline` 不带 `--accept-new` 拒绝接受新问题。

临时树只复制 guard 真正读取的路径,`--root` 指向它;规则清单仍从真实包读取,
所以注入命中的是同一条规则,不是测试自己的副本。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-portable-sql.py"
BASELINE = ROOT / "scripts/dev-env/portable-sql-baseline.json"
BACKEND = "backend/src/speechnexus_backend"

_SPEC = importlib.util.spec_from_file_location("check_portable_sql", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
GUARD = importlib.util.module_from_spec(_SPEC)
# dataclass 需要模块已注册,否则 `_is_type` 取不到 module dict。
sys.modules["check_portable_sql"] = GUARD
_SPEC.loader.exec_module(GUARD)

CLEAN_SOURCE = (
    '"""clean module."""\n\nSQL = "SELECT job_id FROM coordination_jobs WHERE job_id = ?"\n'
)
INJECTIONS = {
    "vendor-sdk": "import psycopg\n",
    "vendor-error-branch": "def branch(error):\n    return error.sqlstate == '23505'\n",
    "brand-branch": "def pick(database):\n    return database == 'mysql'\n",
    "skip-locked": 'SQL = "SELECT id FROM t FOR UPDATE SKIP LOCKED"\n',
    "returning": 'SQL = "INSERT INTO t (id) VALUES (?) RETURNING id"\n',
    "format": 'SQL = "SELECT job_id FROM coordination_jobs WHERE job_id = %s"\n',
}


def run_guard(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


class PortableSqlGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="portable-sql-guard-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.root = self.directory / "repo"
        (self.root / BACKEND).mkdir(parents=True)
        (self.root / "migrations").mkdir(parents=True)
        self.baseline = self.directory / "baseline.json"
        self.baseline.write_text(json.dumps({"version": 1, "findings": {}}), encoding="utf-8")

    def write(self, relative: str, source: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def test_clean_tree_passes(self) -> None:
        self.write(f"{BACKEND}/clean.py", CLEAN_SOURCE)
        result = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT OK", result.stdout)

    def test_every_injection_turns_the_guard_red(self) -> None:
        for rule, injection in INJECTIONS.items():
            with self.subTest(rule=rule):
                self.write(f"{BACKEND}/clean.py", CLEAN_SOURCE)
                self.write(f"{BACKEND}/injected.py", injection)
                result = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
                self.assertEqual(result.returncode, 1, f"{rule}: {result.stdout}")
                self.assertIn(f"NEW {rule} {BACKEND}/injected.py", result.stdout)
                self.assertIn("RESULT FAIL", result.stdout)
                (self.root / BACKEND / "injected.py").unlink()

    def test_ddl_injection_turns_the_guard_red(self) -> None:
        self.write("migrations/0002_bad.sql", "CREATE TABLE t (a TEXT, b BIGINT);\n")
        result = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(result.returncode, 1)
        self.assertIn("NEW ddl-type migrations/0002_bad.sql", result.stdout)

    def test_baseline_does_not_exempt_a_second_violation_in_the_same_file(self) -> None:
        self.write(f"{BACKEND}/clean.py", CLEAN_SOURCE + INJECTIONS["vendor-sdk"])
        first = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(first.returncode, 1)
        self.assertIn("NEW vendor-sdk", first.stdout)
        written = run_guard(
            "--root",
            str(self.root),
            "--write-baseline",
            str(self.baseline),
            "--accept-new",
        )
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        accepted = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(accepted.returncode, 0, accepted.stdout)

        self.write(f"{BACKEND}/clean.py", CLEAN_SOURCE + INJECTIONS["skip-locked"])
        regressed = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(regressed.returncode, 1)
        self.assertIn("NEW skip-locked", regressed.stdout)

    def test_removing_a_baselined_finding_is_reported_and_stays_green(self) -> None:
        self.write(f"{BACKEND}/clean.py", CLEAN_SOURCE + INJECTIONS["vendor-sdk"])
        run_guard("--root", str(self.root), "--write-baseline", str(self.baseline), "--accept-new")
        self.write(f"{BACKEND}/clean.py", CLEAN_SOURCE)
        result = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("FIXED vendor-sdk", result.stdout)

    def test_write_baseline_refuses_new_findings_without_accept_new(self) -> None:
        self.write(f"{BACKEND}/clean.py", CLEAN_SOURCE + INJECTIONS["vendor-sdk"])
        refused = run_guard("--root", str(self.root), "--write-baseline", str(self.baseline))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--accept-new", refused.stderr)
        self.assertEqual(json.loads(self.baseline.read_text())["findings"], {})

    def test_selftest_keeps_every_rule_alive(self) -> None:
        result = run_guard("--selftest")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("selftest OK", result.stdout)


class GuardScopeTests(unittest.TestCase):
    """豁免范围必须是可核对的固定集合,不能因为新目录出现而静默扩大。"""

    def test_exemptions_are_exactly_the_documented_ones(self) -> None:
        self.assertEqual(
            GUARD.EXCLUDED_PREFIXES,
            (
                "packages/portable-sql/src/speechnexus_portable_sql/subset.py",
                # 驱动实现是唯一允许 import 厂商 SDK 的地方(部署期 driver 选择)。
                "packages/portable-sql/src/speechnexus_portable_sql/psycopg_driver.py",
            ),
        )
        self.assertEqual(
            GUARD.MIGRATION_INPUT_PREFIXES,
            ("backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/",),
        )

    def test_committed_baseline_matches_the_tree(self) -> None:
        result = run_guard("--baseline", str(BASELINE))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("new=0", result.stdout)

    def test_ci_runs_the_guard_with_the_committed_baseline(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("check-portable-sql.py --selftest", workflow)
        self.assertIn(
            "check-portable-sql.py --baseline scripts/dev-env/portable-sql-baseline.json",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
