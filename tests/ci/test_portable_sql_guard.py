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

    def test_identifier_and_type_positions_are_judged_by_statement_structure(self) -> None:
        # 同一条规则、同一个词:`c.text` 是列名,`CAST(x AS text)` 是类型。两个方向都必须
        # 由守卫本身判出来——放开标识符时这里变红,退回全文正则时这里也变红。
        self.write(f"{BACKEND}/identifiers.py", 'SQL = "SELECT c.text FROM chapters c"\n')
        clean = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertNotIn("NEW vendor-type", clean.stdout)
        self.write(f"{BACKEND}/typed.py", 'SQL = "SELECT CAST(c.text AS text) FROM chapters c"\n')
        red = run_guard("--root", str(self.root), "--baseline", str(self.baseline))
        self.assertEqual(red.returncode, 1, red.stdout)
        self.assertIn(f"NEW vendor-type {BACKEND}/typed.py", red.stdout)


class TypePositionTests(unittest.TestCase):
    """`TEXT` 的两种用法各有一组提交的断言:类型位置的样本必须报,标识符位置的样本必须不报。"""

    def rules(self, statement: str) -> set[str]:
        return {
            finding.rule
            for finding in GUARD.scan_python(f"{BACKEND}/case.py", f'SQL = "{statement}"')
        }

    def test_type_positions_still_report(self) -> None:
        for statement in (
            "SELECT CAST(installation_id AS text) FROM speech_publication_installation",
            "SELECT CAST(NULL AS text) FROM speech_publication_installation",
            "CREATE TABLE t (job_id VARCHAR(32), payload TEXT)",
            "ALTER TABLE t ADD COLUMN observation_document TEXT",
            "SELECT created_at FROM t WHERE created_at > CAST(? AS timestamptz)",
            "SELECT CAST(node_ids AS text[]) FROM resource_pools",
        ):
            with self.subTest(statement=statement):
                self.assertIn("vendor-type", self.rules(statement))

    def test_identifier_positions_do_not_report(self) -> None:
        for statement in (
            "SELECT id, title, ordinal, text, text_revision FROM chapters WHERE id = ?",
            "INSERT INTO chapters (id, title, ordinal, text) VALUES (?, ?, ?, ?)",
            "SELECT b.id AS parent_id, c.id, c.text"
            " FROM books b JOIN chapters c ON c.book_id = b.id",
            "UPDATE chapters SET text = ? WHERE id = ?",
            "configured but SPEECHNEXUS_ASSET_INSPECTOR=text; select audio instead",
        ):
            with self.subTest(statement=statement):
                self.assertNotIn("vendor-type", self.rules(statement))


class GuardScopeTests(unittest.TestCase):
    """豁免范围必须是可核对的固定集合,不能因为新目录出现而静默扩大。"""

    def test_exemptions_are_exactly_the_documented_ones(self) -> None:
        self.assertEqual(
            GUARD.EXCLUDED_PREFIXES,
            (
                "packages/portable-sql/src/speechnexus_portable_sql/subset.py",
                # 驱动实现是唯一允许 import 厂商 SDK 的地方(部署期 driver 选择)。
                "packages/portable-sql/src/speechnexus_portable_sql/psycopg_driver.py",
                # 时钟语句按 profile 登记("现在"的写法各家不同),与驱动同属部署期选择。
                "packages/portable-sql/src/speechnexus_portable_sql/clock.py",
            ),
        )
        self.assertEqual(
            GUARD.MIGRATION_INPUT_PREFIXES,
            ("backend/src/speechnexus_backend/speech/infrastructure/migrations/",),
        )
        self.assertEqual(
            GUARD.MIGRATION_VERSION_PREFIX,
            "backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/",
        )

    def test_committed_baseline_matches_the_tree(self) -> None:
        # 已声明的物理 DDL profile 与登记表必须与基线一起读,否则声明过的文件会被当成 NEW。
        result = run_guard(
            "--baseline",
            str(BASELINE),
            "--profiles",
            str(ROOT / "scripts/dev-env/portable-sql-profiles.json"),
            "--exceptions",
            str(ROOT / "scripts/dev-env/portable-sql-exceptions.json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("new=0", result.stdout)

    def test_ci_runs_the_guard_with_the_committed_baseline(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("check-portable-sql.py --selftest", workflow)
        self.assertIn(
            "check-portable-sql.py --baseline scripts/dev-env/portable-sql-baseline.json",
            workflow,
        )
        self.assertIn("--profiles scripts/dev-env/portable-sql-profiles.json", workflow)
        self.assertIn("--exceptions scripts/dev-env/portable-sql-exceptions.json", workflow)


if __name__ == "__main__":
    unittest.main()


PROFILES = ROOT / "scripts/dev-env/portable-sql-profiles.json"


def test_declared_profiles_cover_both_artifact_categories() -> None:
    """声明文件的两类制品都必须真的生效.

    `paths` 是「唯一消费者是 alembic 迁移树」的 schema/DDL;`deploy` 是 GRANT/REVOKE 这类
    标识符无法参数化的部署期特权制品。只读其中一类,另一类就会掉回业务 DML 基线。
    """

    import json

    payload = json.loads(PROFILES.read_text(encoding="utf-8"))
    assert payload["paths"], "migration-only schema artifacts are not declared"
    assert payload["deploy"], "deploy-time privilege artifacts are not declared"
    for field in ("paths", "deploy"):
        for entry in payload[field]:
            assert (ROOT / entry).is_file(), f"{field} declares a missing file: {entry}"
    assert not (set(payload["paths"]) & set(payload["deploy"])), "a file is declared twice"
