"""claim bucket、Job CAS 与事件序号:顺序、fencing 与 rowcount 判定."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from speechnexus_portable_sql import (
    BUCKETS,
    STATEMENTS,
    ContractError,
    Row,
    acquire_bucket,
    append_event,
    bucket_for,
    candidates,
    claim_job,
    claim_next,
    mark_outbox_delivered,
    read_events,
    read_fence,
    read_undelivered,
    release_bucket,
    require_fence,
)
from tests.support.portable_sql_fake import FakeDatabase

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=15)
DIGEST = "sha256:" + "a" * 64


def fence_row(*, version: int = 3, owner: str | None = "worker-a", lease: datetime = LEASE) -> Row:
    return Row(("version", "claim_owner_id", "lease_until"), (Decimal(version), owner, lease))


def candidate_row(job_id: str, *, version: int = 1, expires: datetime = NOW) -> Row:
    return Row(("job_id", "claim_version", "claim_expires_at"), (job_id, Decimal(version), expires))


def test_bucket_assignment_is_stable_and_bounded() -> None:
    assert bucket_for("job-1") == bucket_for("job-1")
    assert Decimal(0) <= bucket_for("job-1") < Decimal(BUCKETS)
    assert len({bucket_for(f"job-{index}") for index in range(50)}) > 1
    with pytest.raises(ContractError):
        bucket_for("")


def test_bucket_acquire_only_wins_on_rowcount_one() -> None:
    granted = FakeDatabase(rowcounts={STATEMENTS["bucket.acquire"]: 1})
    lost = FakeDatabase(rowcounts={STATEMENTS["bucket.acquire"]: 0})

    assert acquire_bucket(
        granted.connect(),
        bucket_no=Decimal(7),
        owner_id="worker-a",
        now=NOW,
        lease_until=LEASE,
        expected_version=Decimal(3),
    )
    assert not acquire_bucket(
        lost.connect(),
        bucket_no=Decimal(7),
        owner_id="worker-a",
        now=NOW,
        lease_until=LEASE,
        expected_version=Decimal(3),
    )
    assert granted.statements[0].params == (
        "worker-a",
        LEASE,
        Decimal(4),
        Decimal(7),
        Decimal(3),
        NOW,
    )


def test_release_clears_the_owner_and_bumps_the_version() -> None:
    database = FakeDatabase(rowcounts={STATEMENTS["bucket.release"]: 1})
    assert release_bucket(
        database.connect(), bucket_no=Decimal(7), owner_id="worker-a", expected_version=Decimal(4)
    )
    assert database.statements[0].params == (
        None,
        None,
        Decimal(5),
        Decimal(7),
        Decimal(4),
        "worker-a",
    )


def test_fence_requires_the_current_owner_and_a_live_lease() -> None:
    database = FakeDatabase({STATEMENTS["bucket.fence"]: [[fence_row()]]})
    fence = read_fence(database.connect(), bucket_no=Decimal(3))
    require_fence(fence, owner_id="worker-a", now=NOW, expected_version=Decimal(3))

    with pytest.raises(ContractError, match="owned by"):
        require_fence(fence, owner_id="worker-b", now=NOW)
    with pytest.raises(ContractError, match="expired"):
        require_fence(fence, owner_id="worker-a", now=LEASE)
    with pytest.raises(ContractError, match="version moved"):
        require_fence(fence, owner_id="worker-a", now=NOW, expected_version=Decimal(2))
    with pytest.raises(ContractError, match="missing"):
        require_fence(None, owner_id="worker-a", now=NOW)


def test_candidates_page_by_keyset_and_report_has_more() -> None:
    rows = [candidate_row("job-1"), candidate_row("job-2"), candidate_row("job-3")]
    database = FakeDatabase({STATEMENTS["job.candidates"]: [rows]})

    page, has_more = candidates(database.connect(), bucket_no=Decimal(1), now=NOW, page_size=2)

    assert [item.job_id for item in page] == ["job-1", "job-2"]
    assert has_more is True
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert database.statements[0].params == (Decimal(1), "pending", NOW, epoch, epoch, "")


def test_claim_rejects_a_stale_candidate_without_running_the_job_update() -> None:
    database = FakeDatabase({STATEMENTS["bucket.fence"]: [[fence_row(version=9)]]})
    connection = database.connect()

    with pytest.raises(ContractError, match="version moved"):
        claim_next(
            connection,
            bucket_no=Decimal(1),
            owner_id="worker-a",
            now=NOW,
            expires_at=LEASE,
            bucket_version=Decimal(3),
        )
    assert [statement.sql for statement in database.statements] == [STATEMENTS["bucket.fence"]]


def test_claim_job_cas_counts_rows_not_errors() -> None:
    won = FakeDatabase(rowcounts={STATEMENTS["job.claim"]: 1})
    lost = FakeDatabase(rowcounts={STATEMENTS["job.claim"]: 0})
    assert claim_job(
        won.connect(),
        job_id="job-1",
        owner_id="worker-a",
        expires_at=LEASE,
        expected_version=Decimal(1),
    )
    assert not claim_job(
        lost.connect(),
        job_id="job-1",
        owner_id="worker-a",
        expires_at=LEASE,
        expected_version=Decimal(1),
    )
    assert won.statements[0].params == (
        "claimed",
        "worker-a",
        LEASE,
        Decimal(2),
        "job-1",
        Decimal(1),
        "pending",
    )


def test_append_event_takes_the_next_sequence_and_writes_the_outbox_together() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["job.event_head"]: [
                [Row(("last_event_seq", "claim_version"), (Decimal(41), Decimal(2)))]
            ]
        },
        rowcounts={STATEMENTS["job.event_head_update"]: 1},
    )

    append = append_event(database.connect(), job_id="job-1", event_digest=DIGEST)

    assert append.event_seq == Decimal(42)
    assert append.digest == DIGEST
    executed = [statement.sql for statement in database.statements]
    assert executed == [
        STATEMENTS["job.event_head"],
        STATEMENTS["job_event.insert"],
        STATEMENTS["job.event_head_update"],
        STATEMENTS["outbox.insert"],
    ]
    insert_params = database.statements[1].params
    assert insert_params == ("job-1", Decimal(42), DIGEST, None)


def test_append_event_requires_the_canonical_digest_form() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["job.event_head"]: [
                [Row(("last_event_seq", "claim_version"), (Decimal(0), Decimal(1)))]
            ]
        }
    )
    with pytest.raises(ContractError, match="sha256"):
        append_event(database.connect(), job_id="job-1", event_digest="a" * 64)


def test_a_moved_head_inside_the_locked_transaction_is_a_contract_error() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["job.event_head"]: [
                [Row(("last_event_seq", "claim_version"), (Decimal(5), Decimal(2)))]
            ]
        },
        rowcounts={STATEMENTS["job.event_head_update"]: 0},
    )
    with pytest.raises(ContractError, match="version moved"):
        append_event(database.connect(), job_id="job-1", event_digest=DIGEST)


def test_read_events_resumes_after_the_last_seen_sequence() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["job_event.read"]: [
                [
                    Row(("event_seq", "event_digest", "payload_ref"), (Decimal(7), DIGEST, None)),
                    Row(
                        ("event_seq", "event_digest", "payload_ref"),
                        (Decimal(8), DIGEST, "s3://b/k"),
                    ),
                ]
            ]
        }
    )
    events = read_events(database.connect(), job_id="job-1", after_seq=Decimal(6))
    assert events == [(Decimal(7), DIGEST, None), (Decimal(8), DIGEST, "s3://b/k")]
    assert database.statements[0].params == ("job-1", Decimal(6))


def test_delivery_is_idempotent_and_polled() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["outbox.undelivered"]: [
                [Row(("event_id", "job_id"), ("01H00000000000000000000001", "job-1"))]
            ]
        },
        rowcounts={STATEMENTS["outbox.mark_delivered"]: 1},
    )
    connection = database.connect()
    assert read_undelivered(connection) == [("01H00000000000000000000001", "job-1")]
    assert mark_outbox_delivered(
        connection, event_id="01H00000000000000000000001", delivered_at=NOW
    )
    assert mark_outbox_delivered(
        connection, event_id="01H00000000000000000000001", delivered_at=NOW
    )
