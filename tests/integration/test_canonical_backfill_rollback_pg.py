"""搬迁的**回退演练**:一趟没提交的搬迁必须什么都不留.

切换的风险不在「搬迁成功」,而在「搬到一半失败」。三种失败面都要有证据:

1. 事务内失败 → canonical 一行都不留(原子),草案侧也没被改(搬迁只读草案);
2. 提交成功 → 行可读,且重复跑会被 `adopt_historical_events` 拒绝(一次性回填不是修补);
3. 搬迁中断后重新开始 → 从空表重跑得到与一次跑完相同的结果(可重试,且不依赖上次的残留)。

第三条尤其重要:如果「失败后重跑」会得到不同的 `event_seq` 或摘要,那回退就等于换了一套历史。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from tests.support.portable_pg import connect as portable_pg_connect
from tests.support.portable_sql_pg import QmarkConnection

from speechnexus_portable_sql import (
    ContractError,
    HistoricalEvent,
    adopt_historical_events,
    canonical_job_from_draft,
    read_events,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DDL_FILES = ("migrations/0002_portable_contract.sql", "migrations/0003_portable_claim_buckets.sql")
DIGEST = "sha256:" + "f" * 64
MOMENT = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)


def _draft(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "operation_id": "op-r",
        "attempt_id": "attempt-r",
        "request_digest": DIGEST,
        "job_state": "pending",
        "claim_owner_id": None,
        "claim_expires_at_us": 0,
        "claim_version": 0,
        "due_at_us": int(MOMENT.timestamp() * 1_000_000),
        "last_event_seq": 0,
    }
    values.update(overrides)
    return values


def _history() -> list[HistoricalEvent]:
    return [
        HistoricalEvent(state="pending", occurred_at_us=int(MOMENT.timestamp() * 1_000_000)),
        HistoricalEvent(
            state="running",
            occurred_at_us=int((MOMENT + timedelta(seconds=1)).timestamp() * 1_000_000),
        ),
    ]


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_roll_" + uuid.uuid4().hex[:12]
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
        # 草案形态的 job 表:只有搬迁会读它,never 写。
        admin.execute(
            f'CREATE TABLE "{name}".draft_jobs ('
            "job_id VARCHAR(128) PRIMARY KEY, operation_id VARCHAR(128) NOT NULL, "
            "attempt_id VARCHAR(128) NOT NULL, request_digest VARCHAR(71) NOT NULL, "
            "job_state VARCHAR(24) NOT NULL, claim_owner_id VARCHAR(128), "
            "claim_expires_at_us BIGINT NOT NULL, claim_version BIGINT NOT NULL, "
            "due_at_us BIGINT NOT NULL, last_event_seq BIGINT NOT NULL)"
        )
        admin.execute(
            f'INSERT INTO "{name}".draft_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                "job-r",
                "op-r",
                "attempt-r",
                DIGEST,
                "pending",
                None,
                0,
                0,
                int(MOMENT.timestamp() * 1_000_000),
                0,
            ),
        )
        yield database_url, name
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def _connect(url: str, namespace: str) -> QmarkConnection:
    connection = portable_pg_connect(url)
    connection.execute(f'SET search_path TO "{namespace}", pg_catalog')
    connection.commit()
    return QmarkConnection(connection)


def _count(connection: QmarkConnection, table: str) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return int(Decimal(str(row.at(0))))


_INSERT = (
    "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, request_digest, bucket_no, "
    "due_at, job_state, claim_owner_id, claim_expires_at, claim_version, last_event_seq) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _convert_and_insert(connection: QmarkConnection) -> None:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT job_id, operation_id, attempt_id, request_digest, job_state, "
        "claim_owner_id, claim_expires_at_us, claim_version, due_at_us, "
        "last_event_seq FROM draft_jobs"
    )
    draft = cursor.fetchone()
    assert draft is not None
    row = canonical_job_from_draft(
        job_id=str(draft.at(0)),
        operation_id=str(draft.at(1)),
        attempt_id=str(draft.at(2)),
        request_digest=str(draft.at(3)),
        job_state=str(draft.at(4)),
        claim_owner_id=cast("str | None", draft.at(5)),
        claim_expires_at_us=int(Decimal(str(draft.at(6)))),
        claim_version=int(Decimal(str(draft.at(7)))),
        due_at_us=int(Decimal(str(draft.at(8)))),
        last_event_seq=int(Decimal(str(draft.at(9)))),
    )
    cursor.execute(
        _INSERT,
        (
            cast(str, row["job_id"]),
            cast(str, row["operation_id"]),
            cast(str, row["attempt_id"]),
            cast(str, row["request_digest"]),
            cast(Decimal, row["bucket_no"]),
            cast(datetime, row["due_at"]),
            cast(str, row["job_state"]),
            cast("str | None", row["claim_owner_id"]),
            cast("datetime | None", row["claim_expires_at"]),
            cast(Decimal, row["claim_version"]),
            cast(Decimal, row["last_event_seq"]),
        ),
    )


def test_a_backfill_that_fails_mid_way_leaves_nothing_behind(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _convert_and_insert(connection)
        adopt_historical_events(connection, job_id="job-r", events=_history())
        # 模拟切换脚本在写完之后、提交之前失败。
        connection.rollback()
        jobs = _count(connection, "coordination_jobs")
        events = _count(connection, "job_events")
        draft = _count(connection, "draft_jobs")
    finally:
        connection.close()
    assert jobs == 0
    assert events == 0
    # 搬迁只读草案:回退不能把草案也带走。
    assert draft == 1


def test_a_retry_after_the_failed_attempt_produces_the_same_history(
    namespace: tuple[str, str],
) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _convert_and_insert(connection)
        adopt_historical_events(connection, job_id="job-r", events=_history())
        connection.rollback()
        _convert_and_insert(connection)
        head = adopt_historical_events(connection, job_id="job-r", events=_history())
        connection.commit()
        rows = read_events(connection, job_id="job-r", after_seq=Decimal(0))
    finally:
        connection.close()
    assert head == Decimal(2)
    # 重跑得到的历史必须与一次跑完完全相同:序号与摘要都不依赖上次的残留。
    assert [seq for seq, _, _ in rows] == [Decimal(1), Decimal(2)]


def test_a_committed_backfill_refuses_to_run_twice(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _convert_and_insert(connection)
        adopt_historical_events(connection, job_id="job-r", events=_history())
        connection.commit()
        with pytest.raises(ContractError):
            adopt_historical_events(connection, job_id="job-r", events=_history())
        connection.rollback()
        events = _count(connection, "job_events")
    finally:
        connection.close()
    # 拒绝之后事件数不变:一次性回填不能靠「再跑一次」修补,重跑必须显式清空 canonical。
    assert events == 2
