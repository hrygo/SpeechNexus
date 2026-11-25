"""真库上的「草案行 → canonical 行」换算演练.

换算规则如果只活在一个纯函数里,它仍是「看起来对」。这个测试把它放到 canonical 表的真实
约束下跑一遍:

1. 换算出的行必须能插进 canonical `coordination_jobs`(NOT NULL、主键、唯一键、类型都对);
2. `claim_expires_at_us = 0` 必须变成 NULL —— 0 是草案的「未领取」,插成 1970 会让
   `claim_expires_at <= now` 恒真,认领语义当场反转;
3. 换算出的 `bucket_no` 必须让 `claims.candidates` 真的看见这个 Job —— 分桶算错的唯一
   症状就是「任务永远不被认领」,而那不会有任何报错;
4. `last_event_seq` 必须等于该 Job 的 `MAX(event_seq)`,否则新事件会撞主键。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from tests.support.portable_pg import connect as portable_pg_connect
from tests.support.portable_sql_pg import QmarkConnection

from speechnexus_portable_sql import (
    ContractError,
    HistoricalEvent,
    adopt_historical_events,
    append_event,
    bucket_for,
    candidates,
    canonical_job_from_draft,
    event_digest,
    read_events,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DDL_FILES = ("migrations/0002_portable_contract.sql", "migrations/0003_portable_claim_buckets.sql")
DIGEST = "sha256:" + "c" * 64
MOMENT = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)


def _draft(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
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


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_conv_" + uuid.uuid4().hex[:12]
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
        yield database_url, name
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def _connect(url: str, namespace: str) -> QmarkConnection:
    connection = portable_pg_connect(url)
    connection.execute(f'SET search_path TO "{namespace}", pg_catalog')
    # 刻意用非 UTC 会话时区:canonical 列是 TIMESTAMP(6),写入若依赖会话时区,这里就会变。
    connection.execute("SET TimeZone TO 'Asia/Shanghai'")
    connection.commit()
    return QmarkConnection(connection)


_INSERT = (
    "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, request_digest, bucket_no, "
    "due_at, job_state, claim_owner_id, claim_expires_at, claim_version, last_event_seq) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _insert(connection: QmarkConnection, row: dict[str, object]) -> None:
    cursor = connection.cursor()
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


def test_a_converted_row_satisfies_the_canonical_constraints(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        row = canonical_job_from_draft(job_id="job-a", **_draft())
        _insert(connection, row)
        connection.commit()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT claim_expires_at, bucket_no, due_at, last_event_seq FROM coordination_jobs "
            "WHERE job_id = ?",
            ("job-a",),
        )
        stored = cursor.fetchone()
    finally:
        connection.close()
    assert stored is not None
    # 0 是「未领取」,不是 1970 —— 插成 1970 会让 `claim_expires_at <= now` 恒真。
    assert stored.at(0) is None
    assert Decimal(str(stored.at(1))) == bucket_for("job-a")
    assert stored.at(2) == MOMENT.replace(tzinfo=None)
    assert Decimal(str(stored.at(3))) == Decimal(0)


def test_the_converted_bucket_makes_the_job_visible_to_candidates(
    namespace: tuple[str, str],
) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        past = int((MOMENT - timedelta(minutes=1)).timestamp() * 1_000_000)
        row = canonical_job_from_draft(job_id="job-b", **_draft(due_at_us=past))
        _insert(connection, row)
        connection.commit()
        # 期望值在测试里独立算一遍:如果直接用 `row["bucket_no"]` 查询,换算函数与查询
        # 用的是同一个错的分桶,off-by-one 会同时移动两边而看不出问题。
        expected_bucket = Decimal(
            int.from_bytes(hashlib.sha256(b"job-b").digest()[:8], "big") % 256
        )
        assert Decimal(str(row["bucket_no"])) == expected_bucket
        seen, _ = candidates(
            connection,
            bucket_no=expected_bucket,
            now=MOMENT + timedelta(minutes=1),
            page_size=8,
        )
    finally:
        connection.close()
    # 分桶算错的唯一症状就是「任务永远不被认领」,而且不会有任何报错。
    assert [item.job_id for item in seen] == ["job-b"]


def test_a_job_due_at_the_epoch_is_refused() -> None:
    with pytest.raises(ContractError):
        canonical_job_from_draft(job_id="job-c", **_draft(due_at_us=0))
    with pytest.raises(ContractError):
        canonical_job_from_draft(job_id="job-d", **_draft(last_event_seq=-1))


def test_a_claimed_job_keeps_its_expiry(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        expires_us = int((MOMENT + timedelta(minutes=5)).timestamp() * 1_000_000)
        row = canonical_job_from_draft(
            job_id="job-e",
            **_draft(claim_owner_id="worker-1", claim_expires_at_us=expires_us),
        )
        _insert(connection, row)
        connection.commit()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT claim_expires_at, claim_owner_id FROM coordination_jobs WHERE job_id = ?",
            ("job-e",),
        )
        stored = cursor.fetchone()
    finally:
        connection.close()
    assert stored is not None
    assert stored.at(0) == (MOMENT + timedelta(minutes=5)).replace(tzinfo=None)
    assert stored.at(1) == "worker-1"


def test_adopted_history_then_a_new_event_keeps_the_sequence_continuous(
    namespace: tuple[str, str],
) -> None:
    """历史事件搬完再追加一条:序号必须连续,摘要在搬迁与在线两条路径上一致."""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _insert(connection, canonical_job_from_draft(job_id="job-h", **_draft()))
        connection.commit()
        history = [
            HistoricalEvent(state="pending", occurred_at_us=int(MOMENT.timestamp() * 1_000_000)),
            HistoricalEvent(
                state="running",
                occurred_at_us=int((MOMENT + timedelta(seconds=1)).timestamp() * 1_000_000),
                kind="progress",
                progress_completed=1,
                progress_total=2,
            ),
            HistoricalEvent(
                state="succeeded",
                occurred_at_us=int((MOMENT + timedelta(seconds=2)).timestamp() * 1_000_000),
            ),
        ]
        head = adopt_historical_events(connection, job_id="job-h", events=history)
        connection.commit()
        appended = append_event(
            connection, job_id="job-h", event_digest="sha256:" + "e" * 64, deliverable=False
        )
        connection.commit()
        rows = read_events(connection, job_id="job-h", after_seq=Decimal(0))
    finally:
        connection.close()
    assert head == Decimal(3)
    assert appended.event_seq == Decimal(4)
    assert [seq for seq, _, _ in rows] == [Decimal(1), Decimal(2), Decimal(3), Decimal(4)]
    # 搬迁写下的摘要必须与 `event_digest()` 对同一输入给出的值一致,否则将来任何去重都会分叉。
    for index, event in enumerate(history, start=1):
        expected = event_digest(
            job_id="job-h",
            event_seq=Decimal(index),
            state=event.state,
            occurred_at=datetime.fromtimestamp(event.occurred_at_us / 1_000_000, tz=UTC),
            kind=event.kind,
            progress_completed=event.progress_completed,
            progress_total=event.progress_total,
            payload_ref=event.payload_ref,
        )
        assert rows[index - 1][1] == expected


def test_adoption_writes_no_payload_reference_and_refuses_one(
    namespace: tuple[str, str],
) -> None:
    """产品口径:搬迁不携带 payload 引用。落库必须是 NULL,传进来一个就必须拒绝.

    两个方向都断言,因为「写 NULL」很容易,难的是没有人能在某一次搬迁里替产品发明一个引用:
    草案表根本没有这一列,任何非空值都不是搬来的。
    """

    url, name = namespace
    connection = _connect(url, name)
    try:
        _insert(
            connection,
            canonical_job_from_draft(job_id="job-payload", **_draft(operation_id="op-payload")),
        )
        connection.commit()
        adopt_historical_events(
            connection,
            job_id="job-payload",
            events=[
                HistoricalEvent(state="pending", occurred_at_us=int(MOMENT.timestamp() * 1_000_000))
            ],
        )
        connection.commit()
        rows = read_events(connection, job_id="job-payload", after_seq=Decimal(0))
        assert [reference for _, _, reference in rows] == [None]
    finally:
        connection.close()

    connection = _connect(url, name)
    try:
        _insert(
            connection,
            canonical_job_from_draft(job_id="job-payload-2", **_draft(operation_id="op-payload-2")),
        )
        connection.commit()
        with pytest.raises(ContractError, match="no payload reference"):
            adopt_historical_events(
                connection,
                job_id="job-payload-2",
                events=[
                    HistoricalEvent(
                        state="pending",
                        occurred_at_us=int(MOMENT.timestamp() * 1_000_000),
                        payload_ref="s3://speechnexus/results/payload.json",
                    )
                ],
            )
        connection.rollback()
        assert read_events(connection, job_id="job-payload-2", after_seq=Decimal(0)) == []
    finally:
        connection.close()


def test_adoption_refuses_a_job_that_already_has_events(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _insert(connection, canonical_job_from_draft(job_id="job-i", **_draft()))
        connection.commit()
        adopt_historical_events(
            connection,
            job_id="job-i",
            events=[
                HistoricalEvent(state="pending", occurred_at_us=int(MOMENT.timestamp() * 1_000_000))
            ],
        )
        connection.commit()
        with pytest.raises(ContractError):
            adopt_historical_events(
                connection,
                job_id="job-i",
                events=[
                    HistoricalEvent(
                        state="pending", occurred_at_us=int(MOMENT.timestamp() * 1_000_000)
                    )
                ],
            )
        connection.rollback()
    finally:
        connection.close()
