"""准入预算:锁序、CAS 续租、一次回收与「锁后读探针」的执行顺序."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from speechnexus_portable_sql import (
    AuthorityMismatchError,
    AuthoritySnapshot,
    Connection,
    ContractError,
    Grant,
    ProbeUnavailableError,
    ReservationRequest,
    Row,
    TransactionError,
    deadline_after,
    guarded_reserve,
    reclaim_grant,
    renew_grant,
    require_final_eligibility,
    reserve,
)
from speechnexus_portable_sql.budget import STATEMENTS, ProbeReader
from tests.support.portable_sql_fake import FakeDatabase

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=30)
UNITS = Decimal(2)
REV = "0004"
FINGERPRINT = "f" * 64


def request() -> ReservationRequest:
    return ReservationRequest(
        grant_id="g-1",
        workspace_id="ws-1",
        host_id="host-1",
        meter_kind="attempts",
        session_id="s-1",
        lease_id="l-1",
        units=UNITS,
        lease_expires_at=LEASE,
        grant_expires_at=LEASE,
    )


def quota_row(*, limit: int = 10, held: int = 4, version: int = 3) -> Row:
    return Row(
        ("limit_units", "held_units", "committed_units", "version"),
        (Decimal(limit), Decimal(held), Decimal(0), Decimal(version)),
    )


def budget_row(*, limit: int = 100, held: int = 0, version: int = 7) -> Row:
    return Row(
        ("limit_units", "held_units", "version"),
        (Decimal(limit), Decimal(held), Decimal(version)),
    )


def reserve_database() -> FakeDatabase:
    return FakeDatabase(
        {
            STATEMENTS["quota.lock"]: [[quota_row()]],
            STATEMENTS["budget.lock"]: [[budget_row()]],
        },
        rowcounts={STATEMENTS["quota.hold"]: 1, STATEMENTS["budget.hold"]: 1},
    )


def test_reserve_locks_quota_then_budget_then_inserts_the_grant() -> None:
    database = reserve_database()
    reservation = reserve(database.connect(), request(), epoch=Decimal(5))

    executed = [statement.sql for statement in database.statements]
    assert executed == [
        STATEMENTS["quota.lock"],
        STATEMENTS["budget.lock"],
        STATEMENTS["quota.hold"],
        STATEMENTS["budget.hold"],
        STATEMENTS["grant.insert"],
    ]
    assert database.statements[2].params == (
        UNITS,
        "ws-1",
        "attempts",
        Decimal(3),
        UNITS,
    )
    assert reservation.grant.authority_epoch == Decimal(5)
    assert reservation.grant.granted_units == UNITS
    assert reservation.quota_version == Decimal(4)


def test_reserve_refuses_when_the_quota_cannot_hold() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["quota.lock"]: [[quota_row()]],
            STATEMENTS["budget.lock"]: [[budget_row()]],
        },
        rowcounts={STATEMENTS["quota.hold"]: 0},
    )
    with pytest.raises(ContractError, match="workspace quota"):
        reserve(database.connect(), request(), epoch=Decimal(5))
    assert STATEMENTS["grant.insert"] not in [statement.sql for statement in database.statements]


def test_reserve_refuses_a_missing_quota_row() -> None:
    database = FakeDatabase({STATEMENTS["quota.lock"]: [[]]})
    with pytest.raises(ContractError, match="no attempts quota"):
        reserve(database.connect(), request(), epoch=Decimal(5))


def test_renew_is_a_cas_on_version_epoch_and_state() -> None:
    won = FakeDatabase(rowcounts={STATEMENTS["grant.renew"]: 1})
    lost = FakeDatabase(rowcounts={STATEMENTS["grant.renew"]: 0})
    assert renew_grant(
        won.connect(),
        grant_id="g-1",
        expected_version=Decimal(0),
        expected_epoch=Decimal(5),
        renew_seq=Decimal(1),
        additional_units=UNITS,
        expires_at=LEASE,
    )
    assert not renew_grant(
        lost.connect(),
        grant_id="g-1",
        expected_version=Decimal(0),
        expected_epoch=Decimal(5),
        renew_seq=Decimal(1),
        additional_units=UNITS,
        expires_at=LEASE,
    )
    assert won.statements[0].params == (
        UNITS,
        Decimal(1),
        LEASE,
        "g-1",
        Decimal(0),
        Decimal(5),
        "active",
    )


def test_reclaim_succeeds_once() -> None:
    first = FakeDatabase(rowcounts={STATEMENTS["grant.reclaim"]: 1})
    second = FakeDatabase(rowcounts={STATEMENTS["grant.reclaim"]: 0})
    assert reclaim_grant(
        first.connect(),
        grant_id="g-1",
        expected_version=Decimal(1),
        consumed_units=Decimal(1),
        returned_units=Decimal(1),
    )
    assert not reclaim_grant(
        second.connect(),
        grant_id="g-1",
        expected_version=Decimal(2),
        consumed_units=Decimal(1),
        returned_units=Decimal(1),
    )
    assert first.statements[0].params == (
        Decimal(1),
        Decimal(1),
        "returned",
        "g-1",
        Decimal(1),
        "active",
    )


def snapshot(**overrides: object) -> AuthoritySnapshot:
    base: dict[str, object] = {
        "at": NOW + timedelta(seconds=1),
        "schema_rev": REV,
        "schema_fingerprint": FINGERPRINT,
        "authority_epoch": Decimal(5),
    }
    base.update(overrides)
    return AuthoritySnapshot(**base)  # type: ignore[arg-type]


def grant() -> Grant:
    return Grant(
        grant_id="g-1",
        session_id="s-1",
        lease_id="l-1",
        meter_kind="attempts",
        authority_epoch=Decimal(5),
        renew_seq=Decimal(0),
        granted_units=UNITS,
        consumed_units=Decimal(0),
        returned_units=Decimal(0),
        version=Decimal(0),
        grant_state="active",
        expires_at=LEASE,
    )


def test_final_eligibility_requires_epoch_schema_and_live_lease() -> None:
    ticket = request()
    require_final_eligibility(
        snapshot(),
        grant=grant(),
        request=ticket,
        expected_rev=REV,
        expected_fingerprint=FINGERPRINT,
    )

    with pytest.raises(AuthorityMismatchError, match="authority epoch moved"):
        require_final_eligibility(
            snapshot(authority_epoch=Decimal(6)),
            grant=grant(),
            request=ticket,
            expected_rev=REV,
            expected_fingerprint=FINGERPRINT,
        )
    with pytest.raises(AuthorityMismatchError, match="fingerprint"):
        require_final_eligibility(
            snapshot(schema_fingerprint="e" * 64),
            grant=grant(),
            request=ticket,
            expected_rev=REV,
            expected_fingerprint=FINGERPRINT,
        )
    with pytest.raises(AuthorityMismatchError, match="revision"):
        require_final_eligibility(
            snapshot(schema_rev="0003"),
            grant=grant(),
            request=ticket,
            expected_rev=REV,
            expected_fingerprint=FINGERPRINT,
        )
    with pytest.raises(ContractError, match="lease expired"):
        require_final_eligibility(
            snapshot(at=LEASE + timedelta(seconds=1)),
            grant=grant(),
            request=ticket,
            expected_rev=REV,
            expected_fingerprint=FINGERPRINT,
        )


class RecordingProbe:
    def __init__(self, snapshots: list[AuthoritySnapshot], timeline: list[str]) -> None:
        self._snapshots = snapshots
        self._timeline = timeline

    def read(self) -> AuthoritySnapshot:
        self._timeline.append("probe.read")
        return self._snapshots.pop(0)


class RecordingPool:
    def __init__(self, snapshots: list[AuthoritySnapshot], timeline: list[str]) -> None:
        self._probe = RecordingProbe(snapshots, timeline)
        self._timeline = timeline

    def acquire(self, *, deadline: float | None = None) -> ProbeReader:
        self._timeline.append("probe.acquire")
        return self._probe

    def release(self, probe: ProbeReader) -> None:
        self._timeline.append("probe.release")


class RecordingFactory:
    def __init__(self, database: FakeDatabase, timeline: list[str]) -> None:
        self._database = database
        self._timeline = timeline

    def __call__(self) -> Connection:
        self._timeline.append("connect")
        return self._database.connect()


def test_guarded_reserve_reads_the_probe_after_the_writes_and_before_commit() -> None:
    timeline: list[str] = []
    database = reserve_database()
    connections: list[object] = []

    def factory() -> Connection:
        timeline.append("connect")
        connection = database.connect()
        connections.append(connection)
        return connection

    pool = RecordingPool([snapshot(), snapshot()], timeline)
    reservation = guarded_reserve(
        pool,
        factory,
        request(),
        expected_rev=REV,
        expected_fingerprint=FINGERPRINT,
        deadline=deadline_after(5),
    )
    assert reservation.grant.grant_id == "g-1"
    assert timeline == ["probe.acquire", "probe.read", "connect", "probe.read", "probe.release"]
    # 第一次读在写事务之外,第二次读发生在三条写语句之后、commit 之前。
    writes = [statement.sql for statement in database.statements]
    assert writes[-1] == STATEMENTS["grant.insert"]


def test_guarded_reserve_rolls_back_when_the_final_check_fails() -> None:
    timeline: list[str] = []
    database = reserve_database()
    pool = RecordingPool([snapshot(), snapshot(authority_epoch=Decimal(9))], timeline)

    with pytest.raises(TransactionError) as failure:
        guarded_reserve(
            pool,
            database.connect,
            request(),
            expected_rev=REV,
            expected_fingerprint=FINGERPRINT,
            deadline=deadline_after(5),
        )
    assert isinstance(failure.value.__cause__, AuthorityMismatchError)
    # transaction() 收到异常即回滚,不再提交。
    assert database.connections[0].commits == 0
    assert database.connections[0].rollbacks == 1


class ExhaustedPool:
    def acquire(self, *, deadline: float | None = None) -> ProbeReader:
        raise ProbeUnavailableError("probe pool is full")

    def release(self, probe: ProbeReader) -> None:  # pragma: no cover - never reached
        raise AssertionError("released a probe that was never acquired")


def test_pool_exhaustion_fails_before_any_write_transaction() -> None:
    timeline: list[str] = []

    def factory() -> Connection:
        timeline.append("connect")
        return FakeDatabase().connect()

    with pytest.raises(ProbeUnavailableError):
        guarded_reserve(
            ExhaustedPool(),
            factory,
            request(),
            expected_rev=REV,
            expected_fingerprint=FINGERPRINT,
            deadline=deadline_after(5),
        )
    assert timeline == []


def test_expired_deadline_stops_before_acquiring_a_probe() -> None:
    timeline: list[str] = []
    exhausted = ExhaustedPool()
    deadline = deadline_after(0.000001)
    with pytest.raises(ContractError, match="deadline expired"):
        guarded_reserve(
            exhausted,
            lambda: pytest.fail("must not open a write transaction"),
            request(),
            expected_rev=REV,
            expected_fingerprint=FINGERPRINT,
            deadline=deadline,
        )
    assert timeline == []
