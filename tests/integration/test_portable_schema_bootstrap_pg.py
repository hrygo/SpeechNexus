"""canonical schema 的 bootstrap 演练:真库上跑 `scripts/dev-env/portable-schema.py`.

bootstrap 是「装一次、平时不用」的东西,所以它的失效方式是**在下次部署时才被发现**。这里
把四种情况都放到真库上跑:

1. 空库 → 四个版本依次应用,`schema_meta` 停在最新版;
2. 再跑一次 → 幂等,不重复执行、不改 revision;
3. `--check` 在空库上 → 非零退出(未 bootstrap 不是「合格」);
4. 某版文件播种的 revision 与文件名不一致 → 该版整体回滚,库停在前一版。

第 4 条是这套脚本存在的理由:如果播种语句被改错,`schema_meta` 会停在一个不存在的版本,
而「应用成功」的表象会一直持续到下一次探针发现代次不符。判定点因此只有一处 —— 应用之后
回读 `schema_meta` 并与文件名比对,而不是在加载阶段预先判一次。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.portable_pg import connect as portable_pg_connect

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/portable-schema.py"
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def schema(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_boot_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(database_url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
        yield database_url, name
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def _meta(database_url: str, schema: str) -> tuple[str, str] | None:
    connection = portable_pg_connect(database_url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        try:
            row = connection.execute(
                "SELECT schema_rev, schema_fingerprint FROM schema_meta WHERE meta_id = 'portable'"
            ).fetchone()
        except Exception:
            return None
    finally:
        connection.close()
    assert row is not None
    return (str(row[0]), str(row[1]))


def test_a_fresh_database_reaches_the_latest_revision(schema: tuple[str, str]) -> None:
    url, name = schema
    result = _run("--dsn", url, "--schema", name)
    assert result.returncode == 0, result.stderr
    meta = _meta(url, name)
    assert meta is not None
    assert meta[0] == "0005"


def test_running_it_twice_changes_nothing(schema: tuple[str, str]) -> None:
    url, name = schema
    assert _run("--dsn", url, "--schema", name).returncode == 0
    first = _meta(url, name)
    again = _run("--dsn", url, "--schema", name)
    assert again.returncode == 0, again.stderr
    assert "already at 0005" in again.stdout
    assert _meta(url, name) == first


def test_check_fails_on_an_unbootstrapped_database(schema: tuple[str, str]) -> None:
    url, name = schema
    result = _run("--dsn", url, "--schema", name, "--check")
    # 「没 bootstrap」不是合格:check 必须非零退出,否则部署会把空库当成已就位。
    assert result.returncode == 1
    assert "unbootstrapped" in result.stderr


def test_a_file_whose_seed_disagrees_with_its_name_is_refused(
    tmp_path: Path, schema: tuple[str, str]
) -> None:
    url, name = schema
    tree = tmp_path / "migrations"
    shutil.copytree(ROOT / "migrations", tree)
    target = tree / "0005_portable_usage_intervals.sql"
    target.write_text(
        target.read_text(encoding="utf-8").replace("'0005'", "'0006'", 1), encoding="utf-8"
    )
    result = _run("--dsn", url, "--schema", name, "--migrations", str(tree))
    assert result.returncode == 1
    # 拒绝发生在这一版的事务内:前面的版本已经就位,坏掉的一版整体回滚。
    assert "claims (0005" in result.stderr
    assert "but schema_meta holds (0006" in result.stderr
    meta = _meta(url, name)
    assert meta is not None
    assert meta[0] == "0004"
