"""权威探针:先取连接、后开写事务,读数校验与池容量反例.

顺序是契约本体:池满必须在开启写事务之前失败,不能持有写锁等待第二条连接。这里的 fake 连接
逐条记录语句,所以「先取探针、再读时间」的次序可以被断言。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from speechnexus_portable_sql import (
    AUTHORITY_SQL,
    SCHEMA_META_ID,
    AuthorityMismatchError,
    AuthorityProbePool,
    ContractError,
    ProbeUnavailableError,
    Row,
    SchemaMetaMissingError,
    require_authority,
)
from tests.support.portable_sql_fake import FakeDatabase

FINGERPRINT = "a" * 64


def probe_row(
    *,
    at: object = datetime(2026, 11, 21, 11, 30),
    revision: object = "0002",
    fingerprint: object = FINGERPRINT,
    epoch: object = Decimal(1),
) -> Row:
    return Row(
        ("current_timestamp", "schema_rev", "schema_fingerprint", "authority_epoch"),
        (at, revision, fingerprint, epoch),
    )


def database_with_row(row: Row | None = None) -> FakeDatabase:
    return FakeDatabase({AUTHORITY_SQL: [[row if row is not None else probe_row()]]})


def test_probe_reads_authority_and_normalizes_naive_utc() -> None:
    database = database_with_row()
    pool = AuthorityProbePool(database.connect, capacity=2)

    with pool.probe() as probe:
        snapshot = probe.read()

    assert snapshot.at == datetime(2026, 11, 21, 11, 30, tzinfo=UTC)
    assert snapshot.schema_rev == "0002"
    assert snapshot.schema_fingerprint == FINGERPRINT
    assert snapshot.authority_epoch == Decimal(1)
    assert database.statements[0].params == (SCHEMA_META_ID,)
    # 探针不提交:只读读数之后立刻回滚,不留下悬挂事务。
    assert database.connections[0].commits == 0
    assert database.connections[0].rollbacks == 1
    assert database.connections[0].closed is True


def test_offset_aware_probe_time_is_converted_to_utc() -> None:
    local = datetime(2026, 11, 21, 19, 30, tzinfo=timezone(timedelta(hours=8)))
    pool = AuthorityProbePool(database_with_row(probe_row(at=local)).connect, capacity=1)
    with pool.probe() as probe:
        assert probe.read().at == datetime(2026, 11, 21, 11, 30, tzinfo=UTC)


def test_missing_schema_meta_row_never_falls_back_to_the_application_clock() -> None:
    pool = AuthorityProbePool(FakeDatabase({AUTHORITY_SQL: [[]]}).connect, capacity=1)
    with pool.probe() as probe, pytest.raises(SchemaMetaMissingError):
        probe.read()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("at", "2026-11-21T11:30:00Z"),
        ("revision", ""),
        ("fingerprint", "short"),
        ("epoch", 1),
    ],
)
def test_malformed_probe_values_are_contract_violations(field: str, value: object) -> None:
    row = probe_row(**{field: value})
    pool = AuthorityProbePool(database_with_row(row).connect, capacity=1)
    with pool.probe() as probe, pytest.raises(ContractError):
        probe.read()


def test_pool_rejects_before_a_write_transaction_instead_of_waiting() -> None:
    database = database_with_row()
    pool = AuthorityProbePool(database.connect, capacity=1)
    first = pool.acquire()
    try:
        assert pool.capacity == 1
        assert pool.in_use == 1
        assert pool.available == 0
        started = time.monotonic()
        with pytest.raises(ProbeUnavailableError):
            pool.acquire()
        # 立即失败,不是等待:池内互锁必须在开启写事务之前暴露。
        assert time.monotonic() - started < 0.2
    finally:
        pool.release(first)

    assert pool.in_use == 0
    assert pool.available == 1


def test_deadline_expiry_fails_before_acquiring() -> None:
    pool = AuthorityProbePool(database_with_row().connect, capacity=1)
    with pytest.raises(ProbeUnavailableError):
        pool.acquire(deadline=time.monotonic() - 1)


def test_probe_context_releases_on_failure() -> None:
    database = database_with_row(probe_row(at="not a datetime"))
    pool = AuthorityProbePool(database.connect, capacity=1)
    with pytest.raises(ContractError), pool.probe() as probe:
        probe.read()
    assert pool.in_use == 0
    assert database.connections[0].closed is True


def test_require_authority_rejects_a_replica_or_old_generation() -> None:
    pool = AuthorityProbePool(database_with_row().connect, capacity=1)
    with pool.probe() as probe:
        snapshot = probe.read()

    require_authority(
        snapshot,
        schema_rev="0002",
        schema_fingerprint=FINGERPRINT,
        authority_epoch=Decimal(1),
    )
    with pytest.raises(AuthorityMismatchError):
        require_authority(
            snapshot,
            schema_rev="0003",
            schema_fingerprint=FINGERPRINT,
            authority_epoch=Decimal(1),
        )
    with pytest.raises(AuthorityMismatchError):
        require_authority(
            snapshot,
            schema_rev="0002",
            schema_fingerprint="b" * 64,
            authority_epoch=Decimal(1),
        )
    with pytest.raises(AuthorityMismatchError):
        require_authority(
            snapshot,
            schema_rev="0002",
            schema_fingerprint=FINGERPRINT,
            authority_epoch=Decimal(2),
        )
