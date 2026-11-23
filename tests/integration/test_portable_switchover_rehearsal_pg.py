"""一次完整的切换演练:从空库到对账通过,全部用运维真正会跑的命令串起来.

分离的判据各自绿,不代表它们能串起来:bootstrap 用 `%s`,产品语句用 `?`,对账脚本读的是
草案表,换算函数是库函数。这条演练把顺序跑一遍,顺序本身就是要验证的部分:

    bootstrap → 换算 + 搬事件(单事务)→ 对账 → 断言可以放行

任一步的工具与实际调用方式漂移,这里会先红,而不是在切换窗口里红。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.portable_pg import connect as portable_pg_connect
from tests.support.portable_sql_pg import QmarkConnection

from speechnexus_portable_sql import (
    HistoricalEvent,
    adopt_historical_events,
    bucket_for,
    canonical_job_from_draft,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_CLI = ROOT / "scripts/dev-env/portable-schema.py"
RECONCILE_CLI = ROOT / "scripts/dev-env/portable-reconcile.py"
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "7" * 64
MOMENT = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
DUE_US = int(MOMENT.timestamp() * 1_000_000)

_DRAFT_DDL = (
    "CREATE TABLE draft_jobs ("
    "job_id VARCHAR(128) PRIMARY KEY, operation_id VARCHAR(128) NOT NULL, "
    "attempt_id VARCHAR(128) NOT NULL, request_digest VARCHAR(71) NOT NULL, "
    "job_state VARCHAR(24) NOT NULL, claim_owner_id VARCHAR(128), "
    "claim_expires_at_us BIGINT NOT NULL, claim_version BIGINT NOT NULL, "
    "due_at_us BIGINT NOT NULL, last_event_seq BIGINT NOT NULL)"
)


def _cli(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def schema(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_switch_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(database_url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
        admin.execute(f'SET search_path TO "{name}", pg_catalog')
        admin.execute(_DRAFT_DDL)
        admin.execute(
            "INSERT INTO draft_jobs VALUES (%s, %s, %s, %s, 'pending', NULL, 0, 0, %s, 0)",
            ("job-a", "op-a", "attempt-a", DIGEST, DUE_US),
        )
        admin.execute(
            "INSERT INTO draft_jobs VALUES (%s, %s, %s, %s, 'claimed', %s, %s, 2, %s, 0)",
            ("job-b", "op-b", "attempt-b", DIGEST, "worker-1", DUE_US + 300_000_000, DUE_US),
        )
        yield database_url, name
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def _migrate(url: str, name: str) -> None:
    """运维会跑的那两步:搬行、搬事件,同一个事务。"""

    connection = portable_pg_connect(url)
    wrapped = QmarkConnection(connection)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        connection.commit()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT job_id, operation_id, attempt_id, request_digest, job_state, "
            "claim_owner_id, claim_expires_at_us, claim_version, due_at_us, last_event_seq "
            "FROM draft_jobs ORDER BY job_id"
        )
        for row in cursor.fetchall():
            converted = canonical_job_from_draft(
                job_id=str(row[0]),
                operation_id=str(row[1]),
                attempt_id=str(row[2]),
                request_digest=str(row[3]),
                job_state=str(row[4]),
                claim_owner_id=None if row[5] is None else str(row[5]),
                claim_expires_at_us=int(Decimal(str(row[6]))),
                claim_version=int(Decimal(str(row[7]))),
                due_at_us=int(Decimal(str(row[8]))),
                last_event_seq=int(Decimal(str(row[9]))),
            )
            connection.execute(
                "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, "
                "request_digest, bucket_no, due_at, job_state, claim_owner_id, "
                "claim_expires_at, claim_version, last_event_seq) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    converted["job_id"],
                    converted["operation_id"],
                    converted["attempt_id"],
                    converted["request_digest"],
                    converted["bucket_no"],
                    converted["due_at"],
                    converted["job_state"],
                    converted["claim_owner_id"],
                    converted["claim_expires_at"],
                    converted["claim_version"],
                    converted["last_event_seq"],
                ),
            )
        adopt_historical_events(
            wrapped,
            job_id="job-a",
            events=[HistoricalEvent(state="pending", occurred_at_us=DUE_US)],
        )
        connection.commit()
    finally:
        connection.close()


def test_the_whole_switchover_sequence_passes_reconciliation(schema: tuple[str, str]) -> None:
    url, name = schema
    boot = _cli(SCHEMA_CLI, "--dsn", url, "--schema", name)
    assert boot.returncode == 0, boot.stderr
    _migrate(url, name)
    check = _cli(SCHEMA_CLI, "--dsn", url, "--schema", name, "--check")
    assert check.returncode == 0, check.stderr
    reconcile = _cli(RECONCILE_CLI, "--dsn", url, "--schema", name)
    assert reconcile.returncode == 0, reconcile.stderr
    assert "agree on every field" in reconcile.stdout


def test_reconciliation_blocks_the_switch_when_one_job_is_missed(
    schema: tuple[str, str],
) -> None:
    url, name = schema
    assert _cli(SCHEMA_CLI, "--dsn", url, "--schema", name).returncode == 0
    # 只搬 job-a:模拟搬迁在第二行之前被中断。
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        row = connection.execute(
            "SELECT operation_id, attempt_id, claim_expires_at, due_at FROM "
            "(SELECT 'op-a' AS operation_id, 'attempt-a' AS attempt_id, "
            " NULL::timestamp AS claim_expires_at, %s::timestamp AS due_at) AS draft",
            (MOMENT.replace(tzinfo=None),),
        ).fetchone()
        assert row is not None
        connection.execute(
            "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, request_digest, "
            "bucket_no, due_at, job_state, claim_owner_id, claim_expires_at, claim_version, "
            "last_event_seq) VALUES ('job-a', 'op-a', 'attempt-a', %s, %s, %s, 'pending', "
            "NULL, NULL, 1, 0)",
            (DIGEST, bucket_for("job-a"), MOMENT.replace(tzinfo=None)),
        )
        connection.execute(
            "INSERT INTO job_events (job_id, event_seq, event_digest) VALUES ('job-a', 1, %s)",
            ("sha256:" + "8" * 64,),
        )
    finally:
        connection.close()
    reconcile = _cli(RECONCILE_CLI, "--dsn", url, "--schema", name)
    assert reconcile.returncode == 1
    assert "job-b: missing from coordination_jobs" in reconcile.stderr
    assert "switchover must not proceed" in reconcile.stderr


def test_a_rerun_after_dropping_everything_reaches_the_same_state(
    schema: tuple[str, str],
) -> None:
    url, name = schema
    assert _cli(SCHEMA_CLI, "--dsn", url, "--schema", name).returncode == 0
    _migrate(url, name)
    first = _cli(RECONCILE_CLI, "--dsn", url, "--schema", name)
    assert first.returncode == 0

    # 回退 = 丢掉 canonical 侧重来;草案侧不动(搬迁只读它)。
    admin = portable_pg_connect(url, autocommit=True)
    try:
        admin.execute(f'SET search_path TO "{name}", pg_catalog')
        # 回退 = 撤掉 canonical 侧的全部表,草案表保留。按目录清点,不手写表名。
        rows = admin.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s AND tablename <> 'draft_jobs'",
            (name,),
        ).fetchall()
        for row in rows:
            assert row is not None
            admin.execute(f'DROP TABLE IF EXISTS "{name}"."{row[0]}" CASCADE')
        draft_rows = admin.execute("SELECT COUNT(*) FROM draft_jobs").fetchone()
        assert draft_rows is not None
        assert int(Decimal(str(draft_rows[0]))) == 2
    finally:
        admin.close()

    assert _cli(SCHEMA_CLI, "--dsn", url, "--schema", name).returncode == 0
    _migrate(url, name)
    second = _cli(RECONCILE_CLI, "--dsn", url, "--schema", name)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout


def test_the_rehearsal_covers_a_claimed_job_with_a_live_expiry(schema: tuple[str, str]) -> None:
    url, name = schema
    assert _cli(SCHEMA_CLI, "--dsn", url, "--schema", name).returncode == 0
    _migrate(url, name)
    connection = portable_pg_connect(url, autocommit=True)
    try:
        connection.execute(f'SET search_path TO "{name}", pg_catalog')
        row = connection.execute(
            "SELECT claim_owner_id, claim_expires_at FROM coordination_jobs WHERE job_id = 'job-b'"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    # 已领取的 job 必须保留 owner 与到期时间:搬迁把「在飞认领」抹掉会造成双跑。
    assert row[0] == "worker-1"
    assert row[1] == (MOMENT + timedelta(seconds=300)).replace(tzinfo=None)
