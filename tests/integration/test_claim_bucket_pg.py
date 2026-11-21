"""真实 PostgreSQL 上的 claim bucket、fencing 与每 Job 事件序号.

三条判据都要求真实并发,不能用顺序调用替代:

1. 两个 worker 同时抢同一 bucket,只有一个 `rowcount = 1`;
2. bucket 被 A 持有后,B 的认领在 fence 处被拒,且**没有**发出 Job CAS;
3. 多个事务并发追加事件时,`event_seq` 恰好覆盖 1..N,不重不漏——序号来自锁定后的 head,
   与提交墙钟无关,所以「先取时间后提交」不会换序。
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from tests.support.portable_sql_pg import QmarkConnection

from speechnexus_portable_sql import (
    ContractError,
    append_event,
    bucket_for,
    claim_job,
    claim_next,
    mark_outbox_delivered,
    read_events,
    read_fence,
    read_undelivered,
    require_fence,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DDL_FILES = ("migrations/0002_portable_contract.sql", "migrations/0003_portable_claim_buckets.sql")
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_claim_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(database_url, autocommit=True)
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
    connection = psycopg.connect(url)
    connection.execute(f'SET search_path TO "{namespace}", pg_catalog')
    connection.commit()
    return QmarkConnection(connection)


def _seed(connection: QmarkConnection, *, buckets: int = 4) -> None:
    cursor = connection.cursor()
    try:
        for bucket in range(buckets):
            cursor.execute(
                "INSERT INTO coordination_claim_buckets (bucket_no, claim_owner_id, lease_until, "
                "version) VALUES (?, ?, ?, ?)",
                (Decimal(bucket), None, None, Decimal(0)),
            )
    finally:
        cursor.close()
    connection.commit()


def _insert_job(connection: QmarkConnection, job_id: str, *, bucket: int = 0) -> None:
    cursor = connection.cursor()
    try:
        due = datetime.now(UTC) - timedelta(seconds=1)
        cursor.execute(
            "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, request_digest, "
            "bucket_no, due_at, job_state, claim_owner_id, claim_expires_at, claim_version, "
            "last_event_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                "op-" + job_id,
                "attempt-1",
                DIGEST,
                Decimal(bucket),
                due,
                "pending",
                None,
                None,
                Decimal(0),
                Decimal(0),
            ),
        )
    finally:
        cursor.close()
    connection.commit()


def test_only_one_worker_wins_the_same_bucket(namespace: tuple[str, str]) -> None:
    url, name = namespace
    setup = _connect(url, name)
    _seed(setup)
    setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}
    now = datetime.now(UTC)
    lease = now + timedelta(seconds=15)

    def contend(worker: str) -> None:
        connection = _connect(url, name)
        try:
            barrier.wait()
            won = claim_next_bucket(connection, worker, now, lease)
            # 赢家必须提交:回滚会让第二个等待者看到旧版本,变成两个赢家。
            connection.commit() if won else connection.rollback()
            results[worker] = won
        finally:
            connection.close()

    def claim_next_bucket(
        connection: QmarkConnection, worker: str, at: datetime, until: datetime
    ) -> bool:
        from speechnexus_portable_sql import acquire_bucket

        return acquire_bucket(
            connection,
            bucket_no=Decimal(0),
            owner_id=worker,
            now=at,
            lease_until=until,
            expected_version=Decimal(0),
        )

    threads = [threading.Thread(target=contend, args=(worker,)) for worker in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results.values()) == [False, True], results


def test_a_stale_bucket_holder_cannot_claim(namespace: tuple[str, str]) -> None:
    url, name = namespace
    owner = _connect(url, name)
    stolen = _connect(url, name)
    try:
        _seed(owner)
        _insert_job(owner, "job-1")
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=15)

        from speechnexus_portable_sql import acquire_bucket

        assert acquire_bucket(
            owner,
            bucket_no=Decimal(0),
            owner_id="worker-a",
            now=now,
            lease_until=lease,
            expected_version=Decimal(0),
        )
        owner.commit()

        # worker-b 用旧版本夺 bucket 失败,随后按 stale 版本认领必须被 fence 拒绝。
        assert not acquire_bucket(
            stolen,
            bucket_no=Decimal(0),
            owner_id="worker-b",
            now=now,
            lease_until=lease,
            expected_version=Decimal(0),
        )
        stolen.rollback()

        with pytest.raises(ContractError, match="owned by"):
            claim_next(
                stolen,
                bucket_no=Decimal(0),
                owner_id="worker-b",
                now=now,
                expires_at=lease,
                bucket_version=Decimal(1),
            )
        stolen.rollback()

        # A 仍然可以认领,并把 Job 版本推进到 1。
        outcome = claim_next(
            owner,
            bucket_no=Decimal(0),
            owner_id="worker-a",
            now=now,
            expires_at=lease,
            bucket_version=Decimal(1),
        )
        owner.commit()
        assert outcome is not None and outcome.job_id == "job-1"
    finally:
        owner.close()
        stolen.close()


def test_stale_job_version_updates_zero_rows_and_keeps_the_state(
    namespace: tuple[str, str],
) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed(connection)
        _insert_job(connection, "job-1")
        now = datetime.now(UTC)
        assert claim_job(
            connection,
            job_id="job-1",
            owner_id="worker-a",
            expires_at=now + timedelta(seconds=10),
            expected_version=Decimal(0),
        )
        # 同一版本再来一次是旧 owner 重放,必须 rowcount=0。
        assert not claim_job(
            connection,
            job_id="job-1",
            owner_id="worker-b",
            expires_at=now + timedelta(seconds=10),
            expected_version=Decimal(0),
        )
        connection.commit()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT claim_owner_id, claim_version FROM coordination_jobs WHERE job_id = ?",
                ("job-1",),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        assert row is not None
        assert row.at(0) == "worker-a"
        assert row.at(1) == Decimal(1)
    finally:
        connection.close()


def test_concurrent_appends_cover_every_sequence_exactly_once(namespace: tuple[str, str]) -> None:
    url, name = namespace
    setup = _connect(url, name)
    _seed(setup)
    _insert_job(setup, "job-1")
    setup.close()

    appenders = 6
    barrier = threading.Barrier(appenders)
    sequences: list[Decimal] = []
    lock = threading.Lock()

    def append(worker: int) -> None:
        connection = _connect(url, name)
        try:
            barrier.wait()
            appended = append_event(connection, job_id="job-1", event_digest=DIGEST)
            connection.commit()
            with lock:
                sequences.append(appended.event_seq)
        finally:
            connection.close()

    threads = [threading.Thread(target=append, args=(worker,)) for worker in range(appenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(sequences) == [Decimal(value) for value in range(1, appenders + 1)]

    reader = _connect(url, name)
    try:
        events = read_events(reader, job_id="job-1", after_seq=Decimal(0))
        assert [sequence for sequence, _, _ in events] == sorted(sequences)
        # 断线客户端用 Last-Event-ID 续读,不会重放也不会跳过。
        tail = read_events(reader, job_id="job-1", after_seq=Decimal(appenders - 2))
        assert [sequence for sequence, _, _ in tail] == [Decimal(appenders - 1), Decimal(appenders)]
    finally:
        reader.close()


def test_outbox_advances_without_any_notification(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed(connection)
        _insert_job(connection, "job-1")
        appended = append_event(connection, job_id="job-1", event_digest=DIGEST)
        connection.commit()

        pending = read_undelivered(connection)
        assert pending == [(appended.event_id, "job-1")]
        connection.rollback()

        # 没有任何通知,只靠持久轮询推进;重复标记不再生效。
        assert mark_outbox_delivered(
            connection, event_id=appended.event_id, delivered_at=datetime.now(UTC)
        )
        connection.commit()
        assert read_undelivered(connection) == []
        assert not mark_outbox_delivered(
            connection, event_id=appended.event_id, delivered_at=datetime.now(UTC)
        )
        connection.rollback()
    finally:
        connection.close()


def test_fence_read_returns_the_live_lease(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed(connection)
        now = datetime.now(UTC)
        from speechnexus_portable_sql import acquire_bucket

        assert acquire_bucket(
            connection,
            bucket_no=Decimal(bucket_for("job-1") % 4),
            owner_id="worker-a",
            now=now,
            lease_until=now + timedelta(seconds=15),
            expected_version=Decimal(0),
        )
        connection.commit()
        fence = read_fence(connection, bucket_no=Decimal(bucket_for("job-1") % 4))
        assert fence is not None
        require_fence(fence, owner_id="worker-a", now=now, expected_version=Decimal(1))
    finally:
        connection.close()
