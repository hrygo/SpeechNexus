"""真实 PostgreSQL 上的持久 grant:并发预留、续租竞争、一次回收与锁后复核.

四个判据都必须靠并发或真实锁等待才能成立:

1. 并发抢最后一份预算时,只有一笔提交,`held_units` 不超过 `limit_units`;
2. 两个续租者争同一 `version`,一个成功;失败者读回原 grant,额度不翻倍;
3. 回收在终态转换时只成功一次,`held_units` 只减一次;
4. 锁等待导致 lease 过期、或探针代次在两次读数之间改变时,最终复核失败且整笔回滚。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from tests.support.portable_pg import connect as portable_pg_connect
from tests.support.portable_sql_pg import QmarkConnection

from speechnexus_portable_sql import (
    AuthorityMismatchError,
    AuthorityProbe,
    ContractError,
    ReservationRequest,
    quarantine_grant,
    read_grant,
    reclaim_grant,
    renew_grant,
    require_final_eligibility,
    reserve,
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
    )
)
REV = "0004"


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str, str]]:
    name = "sn_budget_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(database_url, autocommit=True)
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
        yield database_url, name, fingerprint(admin)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def fingerprint(admin: psycopg.Connection) -> str:
    row = admin.execute(
        "SELECT schema_fingerprint FROM schema_meta WHERE meta_id = 'portable'"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _connect(url: str, namespace: str) -> QmarkConnection:
    connection = portable_pg_connect(url)
    connection.execute(f'SET search_path TO "{namespace}", pg_catalog')
    connection.commit()
    return QmarkConnection(connection)


def _seed(connection: QmarkConnection, *, limit: int, host_limit: int = 100) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO workspace_quotas (workspace_id, meter_kind, limit_units, held_units, "
            "committed_units, version) VALUES (?, ?, ?, ?, ?, ?)",
            ("ws-1", "attempts", Decimal(limit), Decimal(0), Decimal(0), Decimal(0)),
        )
        cursor.execute(
            "INSERT INTO host_capacity_budgets (host_id, meter_kind, limit_units, held_units, "
            "version) VALUES (?, ?, ?, ?, ?)",
            ("host-1", "attempts", Decimal(host_limit), Decimal(0), Decimal(0)),
        )
    finally:
        cursor.close()
    connection.commit()


def request(grant_id: str, *, units: int = 1, lease: datetime | None = None) -> ReservationRequest:
    now = datetime.now(UTC)
    expires = lease or now + timedelta(seconds=30)
    return ReservationRequest(
        grant_id=grant_id,
        workspace_id="ws-1",
        host_id="host-1",
        meter_kind="attempts",
        session_id="s-" + grant_id,
        lease_id="l-" + grant_id,
        units=Decimal(units),
        lease_expires_at=expires,
        grant_expires_at=expires,
    )


def held_units(connection: QmarkConnection) -> Decimal:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT held_units FROM workspace_quotas WHERE workspace_id = ? AND meter_kind = ?",
            ("ws-1", "attempts"),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return Decimal(str(row.at(0)))


def grant_count(connection: QmarkConnection) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT grant_id FROM admission_budget_grants", ())
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return len(rows)


def test_concurrent_reservations_never_oversubscribe(namespace: tuple[str, str, str]) -> None:
    url, name, _ = namespace
    setup = _connect(url, name)
    _seed(setup, limit=1)
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def attempt(tag: str) -> None:
        connection = _connect(url, name)
        try:
            barrier.wait()
            reservation = reserve(connection, request(f"g-{tag}"), epoch=Decimal(1))
            connection.commit()
            outcomes[tag] = reservation.grant.grant_id
        except ContractError as error:
            connection.rollback()
            outcomes[tag] = f"rejected: {error}"
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    accepted = [value for value in outcomes.values() if value.startswith("g-")]
    assert len(accepted) == 1, outcomes

    check = _connect(url, name)
    try:
        assert held_units(check) == Decimal(1)
        assert grant_count(check) == 1
    finally:
        check.close()


def test_two_renewers_on_the_same_version_produce_one_grant_of_credit(
    namespace: tuple[str, str, str],
) -> None:
    url, name, _ = namespace
    setup = _connect(url, name)
    _seed(setup, limit=10)
    reserve(setup, request("g-1", units=2), epoch=Decimal(1))
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def renew(tag: str) -> None:
        connection = _connect(url, name)
        try:
            barrier.wait()
            results[tag] = renew_grant(
                connection,
                grant_id="g-1",
                expected_version=Decimal(0),
                expected_epoch=Decimal(1),
                renew_seq=Decimal(1),
                additional_units=Decimal(5),
                expires_at=datetime.now(UTC) + timedelta(seconds=30),
            )
            connection.commit()
        finally:
            connection.close()

    threads = [threading.Thread(target=renew, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results.values()) == [False, True], results

    check = _connect(url, name)
    try:
        grant = read_grant(check, grant_id="g-1")
        assert grant is not None
        assert grant.granted_units == Decimal(7)
        assert grant.version == Decimal(1)
    finally:
        check.close()


def test_renew_with_a_stale_epoch_never_adds_credit(namespace: tuple[str, str, str]) -> None:
    url, name, _ = namespace
    connection = _connect(url, name)
    try:
        _seed(connection, limit=10)
        reserve(connection, request("g-1", units=1), epoch=Decimal(1))
        connection.commit()
        assert not renew_grant(
            connection,
            grant_id="g-1",
            expected_version=Decimal(0),
            expected_epoch=Decimal(2),
            renew_seq=Decimal(1),
            additional_units=Decimal(5),
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        connection.rollback()
        grant = read_grant(connection, grant_id="g-1")
        assert grant is not None and grant.granted_units == Decimal(1)
    finally:
        connection.close()


def test_reclaim_happens_once_and_returns_the_quota(namespace: tuple[str, str, str]) -> None:
    url, name, _ = namespace
    connection = _connect(url, name)
    try:
        _seed(connection, limit=10)
        reserve(connection, request("g-1", units=3), epoch=Decimal(1))
        connection.commit()
        assert held_units(connection) == Decimal(3)

        assert reclaim_grant(
            connection,
            grant_id="g-1",
            expected_version=Decimal(0),
            consumed_units=Decimal(1),
            returned_units=Decimal(2),
        )
        assert not reclaim_grant(
            connection,
            grant_id="g-1",
            expected_version=Decimal(1),
            consumed_units=Decimal(1),
            returned_units=Decimal(2),
        )
        connection.commit()

        grant = read_grant(connection, grant_id="g-1")
        assert grant is not None
        assert (grant.grant_state, grant.consumed_units, grant.returned_units) == (
            "returned",
            Decimal(1),
            Decimal(2),
        )
    finally:
        connection.close()


def test_a_lease_that_expires_during_lock_wait_is_rolled_back(
    namespace: tuple[str, str, str],
) -> None:
    url, name, fingerprint_value = namespace
    blocker = _connect(url, name)
    writer = _connect(url, name)
    try:
        _seed(blocker, limit=10)
        lease = datetime.now(UTC) + timedelta(milliseconds=300)

        blocker.cursor().execute(
            "SELECT limit_units FROM workspace_quotas WHERE workspace_id = ? AND meter_kind = ? "
            "FOR UPDATE",
            ("ws-1", "attempts"),
        )

        outcome: dict[str, object] = {}

        def attempt() -> None:
            try:
                reserve(writer, request("g-1", lease=lease), epoch=Decimal(1))
                outcome["stage"] = "reserved"
            except BaseException as error:
                outcome["error"] = error

        thread = threading.Thread(target=attempt)
        thread.start()
        time.sleep(0.5)
        blocker.commit()
        thread.join(timeout=10)

        assert outcome.get("stage") == "reserved", outcome
        # 探针必须走独立连接:它读完会回滚,不能把写事务里未提交的 grant 一起丢掉。
        probe_connection = _connect(url, name)
        probe = AuthorityProbe(probe_connection)
        with pytest.raises(ContractError, match="lease expired"):
            require_final_eligibility(
                probe.read(),
                grant=read_grant(writer, grant_id="g-1"),  # type: ignore[arg-type]
                request=request("g-1", lease=lease),
                expected_rev=REV,
                expected_fingerprint=fingerprint_value,
            )
        writer.rollback()
        assert grant_count(writer) == 0
        assert held_units(writer) == Decimal(0)
    finally:
        probe_connection.close()
        blocker.close()
        writer.close()


def test_an_authority_epoch_move_between_reads_blocks_the_grant(
    namespace: tuple[str, str, str],
) -> None:
    url, name, fingerprint_value = namespace
    writer = _connect(url, name)
    mover = _connect(url, name)
    try:
        _seed(writer, limit=10)
        probe_connection = _connect(url, name)
        probe = AuthorityProbe(probe_connection)
        before = probe.read()
        reservation = reserve(writer, request("g-1"), epoch=before.authority_epoch)

        mover.cursor().execute(
            "UPDATE schema_meta SET authority_epoch = ? WHERE meta_id = ?",
            (before.authority_epoch + 1, "portable"),
        )
        mover.commit()

        after = probe.read()
        assert after.authority_epoch != reservation.grant.authority_epoch
        with pytest.raises(AuthorityMismatchError, match="authority epoch moved"):
            require_final_eligibility(
                after,
                grant=reservation.grant,
                request=request("g-1"),
                expected_rev=REV,
                expected_fingerprint=fingerprint_value,
            )
        writer.rollback()
        assert grant_count(writer) == 0
        assert held_units(writer) == Decimal(0)
    finally:
        probe_connection.close()
        writer.close()
        mover.close()


def test_missing_recovery_evidence_quarantines_without_refunding(
    namespace: tuple[str, str, str],
) -> None:
    """进程在 ASR accepted 之后崩溃、没有恢复证据时:保留占用,不伪造零消费、不提前退款。"""

    url, name, _ = namespace
    connection = _connect(url, name)
    try:
        _seed(connection, limit=10)
        reserve(connection, request("g-1", units=3), epoch=Decimal(1))
        connection.commit()

        assert quarantine_grant(connection, grant_id="g-1", expected_version=Decimal(0))
        assert not quarantine_grant(connection, grant_id="g-1", expected_version=Decimal(1))
        connection.commit()

        grant = read_grant(connection, grant_id="g-1")
        assert grant is not None
        assert (grant.grant_state, grant.consumed_units, grant.returned_units) == (
            "unknown",
            Decimal(0),
            Decimal(0),
        )
        assert held_units(connection) == Decimal(3)
    finally:
        connection.close()
