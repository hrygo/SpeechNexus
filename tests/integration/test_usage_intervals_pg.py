"""真实 PostgreSQL 上的 usage 区间:逆序提交下的精确重放、部分重叠与并发互斥.

判据都要求真实锁与真实提交顺序:

1. 同一身份在别人未提交时先被阻塞、对方提交后重放,返回原区间且只有一行;
2. 部分重叠即使主键不冲突也必须拒绝(唯一键不能替代重叠检查);
3. 两个不同摘要写同一区间,只有一条落库,另一条按重叠被拒。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from tests.support.portable_sql_pg import QmarkConnection

from speechnexus_portable_sql import (
    IntervalOverlapError,
    UsageAppend,
    append_usage_interval,
    read_usage_intervals,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DDL = tuple(
    f"migrations/{name}"
    for name in (
        "0002_portable_contract.sql",
        "0003_portable_claim_buckets.sql",
        "0004_portable_admission_budget.sql",
        "0005_portable_usage_intervals.sql",
    )
)
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_usage_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(database_url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
        admin.execute(f'SET search_path TO "{name}", pg_catalog')
        for relative in DDL:
            source = (ROOT / relative).read_text(encoding="utf-8")
            body = "\n".join(
                line for line in source.splitlines() if not line.strip().startswith("--")
            )
            for statement in [part.strip() for part in body.split(";") if part.strip()]:
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


def _seed_stream(connection: QmarkConnection, stream_id: str = "stream-1") -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO output_stream_heads (stream_id, meter_kind, last_end, version) "
            "VALUES (?, ?, ?, ?)",
            (stream_id, "asr_samples", Decimal(0), Decimal(0)),
        )
    finally:
        cursor.close()
    connection.commit()


def _append(
    connection: QmarkConnection,
    *,
    start: int,
    end: int,
    digest: str = DIGEST_A,
    stream_id: str = "stream-1",
) -> UsageAppend:
    return append_usage_interval(
        connection,
        stream_id=stream_id,
        meter_kind="asr_samples",
        interval_start=Decimal(start),
        interval_end=Decimal(end),
        fact_hash=digest,
    )


def _count(connection: QmarkConnection) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT interval_id FROM usage_intervals", ())
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return len(rows)


def test_a_blocked_writer_replays_the_committed_interval(namespace: tuple[str, str]) -> None:
    url, name = namespace
    first = _connect(url, name)
    second = _connect(url, name)
    try:
        _seed_stream(first)
        original = _append(first, start=0, end=100)

        outcome: dict[str, UsageAppend | BaseException] = {}

        def contend() -> None:
            try:
                outcome["result"] = _append(second, start=0, end=100)
                second.commit()
            except BaseException as error:
                outcome["error"] = error
                second.rollback()

        thread = threading.Thread(target=contend)
        thread.start()
        time.sleep(0.3)
        first.commit()
        thread.join(timeout=10)

        result = outcome.get("result")
        assert not isinstance(outcome.get("error"), BaseException), outcome
        assert isinstance(result, UsageAppend)
        assert result.appended is False
        assert result.interval.interval_id == original.interval.interval_id
        assert _count(first) == 1
    finally:
        first.close()
        second.close()


def test_partial_overlap_is_rejected_although_the_key_would_not_conflict(
    namespace: tuple[str, str],
) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_stream(connection)
        _append(connection, start=0, end=100)
        connection.commit()

        with pytest.raises(IntervalOverlapError, match=r"overlaps \[0, 100\)"):
            _append(connection, start=50, end=150, digest=DIGEST_B)
        connection.rollback()

        assert _count(connection) == 1
        head = connection.cursor()
        try:
            head.execute(
                "SELECT last_end, version FROM output_stream_heads "
                "WHERE stream_id = ? AND meter_kind = ?",
                ("stream-1", "asr_samples"),
            )
            row = head.fetchone()
        finally:
            head.close()
        assert row is not None
        assert (row.at(0), row.at(1)) == (Decimal(100), Decimal(1))
    finally:
        connection.close()


def test_two_conflicting_writers_leave_exactly_one_interval(namespace: tuple[str, str]) -> None:
    url, name = namespace
    setup = _connect(url, name)
    _seed_stream(setup)
    setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def write(tag: str, digest: str) -> None:
        connection = _connect(url, name)
        try:
            barrier.wait()
            appended = _append(connection, start=0, end=100, digest=digest)
            connection.commit()
            results[tag] = "appended" if appended.appended else "replayed"
        except IntervalOverlapError:
            connection.rollback()
            results[tag] = "overlap"
        finally:
            connection.close()

    threads = [
        threading.Thread(target=write, args=("a", DIGEST_A)),
        threading.Thread(target=write, args=("b", DIGEST_B)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results.values()) == ["appended", "overlap"], results

    check = _connect(url, name)
    try:
        assert _count(check) == 1
    finally:
        check.close()


def test_keyset_read_returns_the_committed_order(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_stream(connection)
        for start in (0, 10, 20):
            _append(connection, start=start, end=start + 10, digest=DIGEST_A)
        connection.commit()

        intervals = read_usage_intervals(
            connection, stream_id="stream-1", meter_kind="asr_samples", after_start=Decimal(-1)
        )
        assert [int(item.interval_start) for item in intervals] == [0, 10, 20]
        assert [int(item.units) for item in intervals] == [10, 10, 10]

        tail = read_usage_intervals(
            connection, stream_id="stream-1", meter_kind="asr_samples", after_start=Decimal(10)
        )
        assert [int(item.interval_start) for item in tail] == [20]
    finally:
        connection.close()


def test_a_missing_head_registration_is_refused(namespace: tuple[str, str]) -> None:
    url, name = namespace
    connection = _connect(url, name)
    try:
        with pytest.raises(BaseException) as failure:
            _append(connection, start=0, end=10, stream_id="stream-missing")
        connection.rollback()
        assert "not registered" in str(failure.value)
    finally:
        connection.close()
