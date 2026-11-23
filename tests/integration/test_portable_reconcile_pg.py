"""切换前的对账演练:真库上跑 `scripts/dev-env/portable-reconcile.py`.

对账的价值全在「能不能发现差异」,所以每条判据都是一次真实的错位:

1. 一致 → 0;
2. canonical 的 `due_at` 差一小时 → 非零,并指出字段;
3. canonical 缺行(搬迁截断)→ 非零,并指认 job_id;
4. `last_event_seq` 与 `MAX(event_seq)` 不符 → 非零。

第 2、4 条是这套脚本存在的理由:它们都不会让任何查询报错,只会让任务在切换后不被认领或
事件主键冲突。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.support.portable_pg import connect as portable_pg_connect
from tests.support.portable_sql_pg import QmarkConnection

from speechnexus_portable_sql import HistoricalEvent, adopt_historical_events, bucket_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/portable-reconcile.py"
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DDL_FILES = ("migrations/0002_portable_contract.sql", "migrations/0003_portable_claim_buckets.sql")
DIGEST = "sha256:" + "9" * 64
MOMENT = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
DUE_US = int(MOMENT.timestamp() * 1_000_000)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_rec_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(database_url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
        admin.execute(f'SET search_path TO "{name}", pg_catalog')
        for relative in DDL_FILES:
            source = (ROOT / relative).read_text(encoding="utf-8")
            ddl = "\n".join(
                line for line in source.splitlines() if not line.strip().startswith("--")
            )
            for statement in [part.strip() for part in ddl.split(";") if part.strip()]:
                admin.execute(statement)
        admin.execute(
            f'CREATE TABLE "{name}".draft_jobs ('
            "job_id VARCHAR(128) PRIMARY KEY, operation_id VARCHAR(128) NOT NULL, "
            "attempt_id VARCHAR(128) NOT NULL, request_digest VARCHAR(71) NOT NULL, "
            "job_state VARCHAR(24) NOT NULL, claim_owner_id VARCHAR(128), "
            "claim_expires_at_us BIGINT NOT NULL, claim_version BIGINT NOT NULL, "
            "due_at_us BIGINT NOT NULL, last_event_seq BIGINT NOT NULL)"
        )
        yield database_url, name
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def _seed(url: str, name: str, *, jobs: int = 2) -> None:
    """在草案与 canonical 两侧写入一致的 job,事件也一起对齐."""

    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        for index in range(jobs):
            job_id = f"job-{index}"
            connection.execute(
                "INSERT INTO draft_jobs VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    f"op-{index}",
                    f"attempt-{index}",
                    DIGEST,
                    "pending",
                    None,
                    0,
                    0,
                    DUE_US,
                    0,
                ),
            )
            connection.execute(
                "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, "
                "request_digest, bucket_no, due_at, job_state, claim_owner_id, "
                "claim_expires_at, claim_version, last_event_seq) "
                "SELECT %s, %s, %s, %s, %s, %s, 'pending', NULL, NULL, 0, 0",
                (
                    job_id,
                    f"op-{index}",
                    f"attempt-{index}",
                    DIGEST,
                    bucket_for(job_id),
                    MOMENT.replace(tzinfo=None),
                ),
            )
        adopt_historical_events(
            QmarkConnection(connection),
            job_id="job-0",
            events=[HistoricalEvent(state="pending", occurred_at_us=DUE_US)],
        )
    finally:
        connection.close()


def test_agreeing_tables_pass(namespace: tuple[str, str]) -> None:
    url, name = namespace
    _seed(url, name)
    result = _run("--dsn", url, "--schema", name)
    assert result.returncode == 0, result.stderr
    assert "agree on every field" in result.stdout


def test_a_shifted_due_at_is_reported(namespace: tuple[str, str]) -> None:
    url, name = namespace
    _seed(url, name)
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        connection.execute(
            "UPDATE coordination_jobs SET due_at = %s WHERE job_id = 'job-1'",
            ((MOMENT + timedelta(hours=1)).replace(tzinfo=None),),
        )
    finally:
        connection.close()
    result = _run("--dsn", url, "--schema", name)
    assert result.returncode == 1
    assert "job-1: due_at expected" in result.stderr


def test_a_missing_canonical_row_is_reported(namespace: tuple[str, str]) -> None:
    url, name = namespace
    _seed(url, name)
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        connection.execute("DELETE FROM coordination_jobs WHERE job_id = 'job-1'")
    finally:
        connection.close()
    result = _run("--dsn", url, "--schema", name)
    assert result.returncode == 1
    assert "job-1: missing from coordination_jobs" in result.stderr


def test_a_stale_event_head_is_reported(namespace: tuple[str, str]) -> None:
    url, name = namespace
    _seed(url, name)
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        # 事件搬进去了但 head 没推进:下一次 append_event 会撞主键。
        connection.execute("UPDATE coordination_jobs SET last_event_seq = 0 WHERE job_id = 'job-0'")
    finally:
        connection.close()
    result = _run("--dsn", url, "--schema", name)
    assert result.returncode == 1
    assert "job-0: last_event_seq expected Decimal('1'), found Decimal('0')" in result.stderr


def test_decimal_zero_and_null_are_the_same_absence(namespace: tuple[str, str]) -> None:
    url, name = namespace
    _seed(url, name)
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        # 草案写 0(未领取)、canonical 写 NULL:这是等价,不是差异。
        connection.execute(
            "UPDATE coordination_jobs SET claim_expires_at = NULL WHERE job_id = 'job-1'"
        )
    finally:
        connection.close()
    assert _run("--dsn", url, "--schema", name).returncode == 0


def test_a_claim_version_that_went_backwards_is_reported(namespace: tuple[str, str]) -> None:
    url, name = namespace
    _seed(url, name)
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        connection.execute("UPDATE draft_jobs SET claim_version = 5 WHERE job_id = 'job-1'")
    finally:
        connection.close()
    result = _run("--dsn", url, "--schema", name)
    assert result.returncode == 1
    assert "job-1: claim_version went backwards" in result.stderr


def test_a_nonzero_expiry_must_match_exactly(namespace: tuple[str, str]) -> None:
    url, name = namespace
    _seed(url, name)
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        connection.execute(
            "UPDATE draft_jobs SET claim_expires_at_us = %s WHERE job_id = 'job-1'",
            (DUE_US + 5_000_000,),
        )
        connection.execute(
            "UPDATE coordination_jobs SET claim_expires_at = %s WHERE job_id = 'job-1'",
            ((MOMENT + timedelta(seconds=3)).replace(tzinfo=None),),
        )
    finally:
        connection.close()
    result = _run("--dsn", url, "--schema", name)
    assert result.returncode == 1
    assert "job-1: claim_expires_at expected" in result.stderr
