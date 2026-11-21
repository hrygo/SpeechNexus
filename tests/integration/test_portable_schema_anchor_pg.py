"""首发 PostgreSQL 上的两类 oracle:权威探针的新鲜度,以及 DB-API 的边界往返.

第一部分证明设计里的顺序不是形式主义:PostgreSQL 的 `CURRENT_TIMESTAMP` 取事务开始时间,
所以持有行锁的写事务在锁等待之后读到的仍是旧时刻;只有**先取得、与写事务分离**的探针连接
才能给出新鲜时间。探针在写事务开启前就应在手,锁之后才读——用例按这个顺序部署。

第二部分把契约最容易出现方言分叉的边界放到真实 PG 上往返:空串、NULL、UTF-8、`Decimal(38,0)`、
`TIMESTAMP(6)` 精度和 `rowcount`。应用层的字节长度与空串拒绝由单元测试覆盖,这里只证明驱动
往返不丢语义。
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
DDL = ROOT / "migrations/0002_portable_contract.sql"
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
LOCK_WAIT_SECONDS = 0.4
BOUNDS_DDL = """
CREATE TABLE oracle_bounds (
    bound_id VARCHAR(32) NOT NULL,
    label VARCHAR(128) NOT NULL,
    amount DECIMAL(38,0) NOT NULL,
    at TIMESTAMP(6) NOT NULL,
    note VARCHAR(128),
    PRIMARY KEY (bound_id)
)
"""


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_schema_meta_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(database_url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
        admin.execute(f'SET search_path TO "{name}", pg_catalog')
        source = DDL.read_text(encoding="utf-8")
        ddl = "\n".join(line for line in source.splitlines() if not line.strip().startswith("--"))
        for statement in [part.strip() for part in ddl.split(";") if part.strip()]:
            admin.execute(statement)
        admin.execute(BOUNDS_DDL)
        yield database_url, name
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def _connect(database_url: str, namespace: str) -> psycopg.Connection[tuple[object, ...]]:
    connection: psycopg.Connection[tuple[object, ...]] = psycopg.connect(database_url)
    connection.execute(f'SET search_path TO "{namespace}", pg_catalog')
    connection.commit()
    return connection


def test_schema_meta_seeds_revision_and_fingerprint(namespace: tuple[str, str]) -> None:
    url, name = namespace
    with _connect(url, name) as connection:
        row = connection.execute(
            "SELECT schema_rev, schema_fingerprint, authority_epoch FROM schema_meta "
            "WHERE meta_id = %s",
            ("portable",),
        ).fetchone()
    assert row is not None
    assert row[0] == "0002"
    assert len(str(row[1])) == 64
    assert row[2] == Decimal(1)


def test_dedicated_probe_stays_fresh_after_the_write_transaction_waited(
    namespace: tuple[str, str],
) -> None:
    url, name = namespace
    writer = _connect(url, name)
    # 1. 写事务之外先取得探针连接;容量不足时应在此之前失败。
    probe = _connect(url, name)
    try:
        # 2. 写事务取得权威行锁。
        writer.execute(
            "SELECT meta_id FROM schema_meta WHERE meta_id = %s FOR UPDATE", ("portable",)
        )
        transaction_start = writer.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]  # type: ignore[index]

        # 3. 另一条连接真实阻塞,证明锁确实被持有。
        waiter = _connect(url, name)
        try:
            started = time.monotonic()
            waiter.execute("SET LOCAL lock_timeout = '200ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                waiter.execute(
                    "SELECT meta_id FROM schema_meta WHERE meta_id = %s FOR UPDATE",
                    ("portable",),
                )
            waiter.rollback()
            assert time.monotonic() - started < LOCK_WAIT_SECONDS
        finally:
            waiter.close()

        time.sleep(LOCK_WAIT_SECONDS)

        # 4. 写事务内的 CURRENT_TIMESTAMP 仍是事务开始时间——不能用作新鲜时刻。
        stale = writer.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]  # type: ignore[index]
        assert stale == transaction_start

        # 5. 专用探针读到的是锁等待之后的时间。
        fresh = probe.execute(
            "SELECT CURRENT_TIMESTAMP FROM schema_meta WHERE meta_id = %s", ("portable",)
        ).fetchone()[0]  # type: ignore[index]
        assert isinstance(stale, datetime) and isinstance(fresh, datetime)
        assert fresh > stale
        assert (fresh - stale).total_seconds() >= LOCK_WAIT_SECONDS * 0.5
    finally:
        writer.rollback()
        probe.rollback()
        writer.close()
        probe.close()


def test_bound_values_round_trip_without_semantic_loss(namespace: tuple[str, str]) -> None:
    url, name = namespace
    amount = Decimal("9" * 38)
    moment = datetime(2026, 11, 21, 11, 30, 45, 123456)
    label = "语音🎙中文"
    with _connect(url, name) as connection:
        connection.execute(
            "INSERT INTO oracle_bounds (bound_id, label, amount, at, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("bound-1", label, amount, moment, None),
        )
        connection.commit()

        row = connection.execute(
            "SELECT label, amount, at, note FROM oracle_bounds WHERE bound_id = %s", ("bound-1",)
        ).fetchone()
        assert row is not None
        assert row[0] == label
        assert isinstance(row[1], Decimal)
        assert row[1] == amount
        assert row[2] == moment
        assert row[3] is None

        # 空串在应用层被拒绝;数据库里不应出现空串事实。
        empty = connection.execute(
            "SELECT COUNT(*) FROM oracle_bounds WHERE note = %s", ("",)
        ).fetchone()
        assert empty is not None and empty[0] == 0

        # CAS 判定只看影响行数:版本相同为 1,陈旧为 0。
        first = connection.execute(
            "UPDATE oracle_bounds SET note = %s WHERE bound_id = %s", ("settled", "bound-1")
        )
        assert first.rowcount == 1
        stale = connection.execute(
            "UPDATE oracle_bounds SET note = %s WHERE bound_id = %s AND note = %s",
            ("again", "bound-1", "missing"),
        )
        assert stale.rowcount == 0
        connection.rollback()


def test_utc_normalization_of_a_naive_timestamp_is_explicit(namespace: tuple[str, str]) -> None:
    url, name = namespace
    moment = datetime(2026, 11, 21, 11, 30, 45, 123456)
    with _connect(url, name) as connection:
        connection.execute(
            "INSERT INTO oracle_bounds (bound_id, label, amount, at, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("bound-2", "utc", Decimal(0), moment, None),
        )
        connection.commit()
        row = connection.execute(
            "SELECT at FROM oracle_bounds WHERE bound_id = %s", ("bound-2",)
        ).fetchone()
    assert row is not None
    stored = row[0]
    assert isinstance(stored, datetime)
    assert stored.tzinfo is None, "PG TIMESTAMP without time zone stays naive"
    assert stored.replace(tzinfo=UTC) == moment.replace(tzinfo=UTC)
