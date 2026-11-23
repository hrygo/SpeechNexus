"""`claim_batch` 的语句形状与轮询语义:轮转顺序、限额、租约归还与语句选择.

这些都只证明「发出去的语句和参数是对的、顺序是对的」。真实并发、fence 与准入锚点的行为
由 `tests/integration/test_claim_batch_pg.py` 在真库上验证;同一判据不会在两边各写一份语义。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from speechnexus_portable_sql import (
    BUCKETS,
    STATEMENTS,
    BatchClaim,
    ContractError,
    Row,
    claim_batch,
    claimed_jobs,
    reclaim_expired,
    start_bucket_for,
)
from tests.support.portable_sql_fake import FakeDatabase

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=15)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def fence_row(*, version: int = 3, owner: str | None = None, lease: datetime | None = LEASE) -> Row:
    return Row(("version", "claim_owner_id", "lease_until"), (Decimal(version), owner, lease))


def candidate_row(job_id: str, *, version: int = 2, due: datetime = NOW) -> Row:
    return Row(("job_id", "claim_version", "due_at"), (job_id, Decimal(version), due))


def rotation(start: int) -> list[Decimal]:
    return [Decimal((start + offset) % BUCKETS) for offset in range(BUCKETS)]


def test_every_bucket_is_visited_once_in_rotation_order() -> None:
    """起点之后线性扫满一圈并回绕:每个桶恰好一次,不多不少。"""

    database = FakeDatabase({STATEMENTS["bucket.fence"]: []})

    assert (
        claim_batch(
            database.connect(),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=4,
            start_bucket=250,
        )
        == ()
    )
    assert [statement.sql for statement in database.statements] == [
        STATEMENTS["bucket.fence"]
    ] * BUCKETS
    assert [statement.params[0] for statement in database.statements] == rotation(250)


def test_a_full_batch_stops_at_the_limit_and_releases_the_bucket() -> None:
    """认满 `limit` 就停:同一轮之后的桶不再读,桶租约在返回前已经还掉。"""

    database = FakeDatabase(
        {
            STATEMENTS["bucket.fence"]: [
                [fence_row(version=3, owner=None)],
                [fence_row(version=4, owner="worker-a")],
            ],
            STATEMENTS["job.candidates"]: [[candidate_row("job-1")]],
        },
        rowcounts={
            STATEMENTS["bucket.acquire"]: 1,
            STATEMENTS["job.claim"]: 1,
            STATEMENTS["bucket.release"]: 1,
        },
    )

    claims = claim_batch(
        database.connect(),
        owner_id="worker-a",
        now=NOW,
        lease_until=LEASE,
        limit=1,
        start_bucket=7,
    )

    assert claims == (BatchClaim("job-1", Decimal(3), LEASE),)
    assert [statement.sql for statement in database.statements] == [
        STATEMENTS["bucket.fence"],
        STATEMENTS["bucket.acquire"],
        STATEMENTS["bucket.fence"],
        STATEMENTS["job.reclaim"],
        STATEMENTS["job.candidates"],
        STATEMENTS["job.claim"],
        STATEMENTS["bucket.release"],
    ]
    # 拿桶用读到的版本做 CAS,并把版本推进到 4。
    assert database.statements[1].params == (
        "worker-a",
        LEASE,
        Decimal(4),
        Decimal(7),
        Decimal(3),
        NOW,
    )
    # 复核用的是 acquire 之后的版本,不是读 fence 时的版本。
    assert database.statements[2].params == (Decimal(7),)
    # 重领在取候选之前:回 pending、清 owner、只碰本桶里租约已死的 claimed 行。
    assert database.statements[3].params == (
        "pending",
        None,
        Decimal(7),
        "claimed",
        NOW,
    )
    # 候选页的 keyset 下界是纪元,不是 NULL。
    assert database.statements[4].params == (
        Decimal(7),
        "pending",
        NOW,
        EPOCH,
        EPOCH,
        "",
    )
    assert database.statements[5].params == (
        "claimed",
        "worker-a",
        LEASE,
        Decimal(3),
        "job-1",
        Decimal(2),
        "pending",
    )
    # 归还:清空 owner 与租约,版本从 4 推到 5。
    assert database.statements[6].params == (
        None,
        None,
        Decimal(5),
        Decimal(7),
        Decimal(4),
        "worker-a",
    )


def test_a_bucket_held_by_a_live_lease_is_skipped_without_a_cas() -> None:
    """别人持有且租约未过期:不抢、不读候选,只跳过该桶。"""

    database = FakeDatabase(
        {STATEMENTS["bucket.fence"]: [[fence_row(version=9, owner="worker-b")]]}
    )

    assert (
        claim_batch(
            database.connect(),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=0,
        )
        == ()
    )
    emitted = [statement.sql for statement in database.statements]
    assert emitted == [STATEMENTS["bucket.fence"]] * BUCKETS
    assert STATEMENTS["bucket.acquire"] not in emitted
    # 没拿到桶就不重领:重领是「持有该桶」时的动作,不是全局清扫。
    assert STATEMENTS["job.reclaim"] not in emitted
    assert STATEMENTS["job.claim"] not in emitted


def test_a_lost_acquire_cas_never_double_acquires_a_bucket() -> None:
    """租约已过期但 CAS 输给别人:每个桶只尝试一次 acquire,且不认领、不归还。"""

    expired = NOW - timedelta(seconds=1)
    database = FakeDatabase(
        {STATEMENTS["bucket.fence"]: [[fence_row(version=3, owner="worker-b", lease=expired)]]},
        rowcounts={STATEMENTS["bucket.acquire"]: 0},
    )

    assert (
        claim_batch(
            database.connect(),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=2,
            start_bucket=100,
        )
        == ()
    )
    acquires = [
        statement
        for statement in database.statements
        if statement.sql == STATEMENTS["bucket.acquire"]
    ]
    assert [statement.params[3] for statement in acquires] == rotation(100)
    assert len({statement.params[3] for statement in acquires}) == BUCKETS
    emitted = [statement.sql for statement in database.statements]
    assert STATEMENTS["bucket.release"] not in emitted
    assert STATEMENTS["job.reclaim"] not in emitted
    assert STATEMENTS["job.candidates"] not in emitted
    assert STATEMENTS["job.claim"] not in emitted


def test_an_inconsistent_fence_refuses_the_batch_and_still_releases() -> None:
    """复核对不上就整笔拒绝;桶租约仍然在返回前归还,不留给下一次调用。"""

    database = FakeDatabase(
        {
            STATEMENTS["bucket.fence"]: [
                [fence_row(version=3, owner=None)],
                [fence_row(version=9, owner="worker-a")],
            ]
        },
        rowcounts={STATEMENTS["bucket.acquire"]: 1, STATEMENTS["bucket.release"]: 1},
    )

    with pytest.raises(ContractError, match="version moved"):
        claim_batch(
            database.connect(),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=1,
        )

    assert [statement.sql for statement in database.statements] == [
        STATEMENTS["bucket.fence"],
        STATEMENTS["bucket.acquire"],
        STATEMENTS["bucket.fence"],
        STATEMENTS["bucket.release"],
    ]
    assert STATEMENTS["job.claim"] not in [statement.sql for statement in database.statements]


def test_a_lost_release_fails_instead_of_returning_claims() -> None:
    """归还时发现桶已不属于自己:不能把「可能还压在别人桶上」的认领当成成功。"""

    database = FakeDatabase(
        {
            STATEMENTS["bucket.fence"]: [
                [fence_row(version=3, owner=None)],
                [fence_row(version=4, owner="worker-a")],
            ],
            STATEMENTS["job.candidates"]: [[candidate_row("job-1")]],
        },
        rowcounts={
            STATEMENTS["bucket.acquire"]: 1,
            STATEMENTS["job.claim"]: 1,
            STATEMENTS["bucket.release"]: 0,
        },
    )

    with pytest.raises(ContractError, match="lease moved under the batch"):
        claim_batch(
            database.connect(),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=0,
        )


def test_the_lane_filter_switches_the_statement_and_expands_the_list() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["bucket.fence"]: [
                [fence_row(version=3, owner=None)],
                [fence_row(version=4, owner="worker-a")],
            ],
            STATEMENTS["job.candidates.lane"]: [[candidate_row("job-1")]],
        },
        rowcounts={
            STATEMENTS["bucket.acquire"]: 1,
            STATEMENTS["job.claim"]: 1,
            STATEMENTS["bucket.release"]: 1,
        },
    )

    claims = claim_batch(
        database.connect(),
        owner_id="worker-a",
        now=NOW,
        lease_until=LEASE,
        limit=1,
        start_bucket=2,
        kinds=("transcription", "speech_synthesis"),
    )

    assert [claim.job_id for claim in claims] == ["job-1"]
    assert [statement.sql for statement in database.statements] == [
        STATEMENTS["bucket.fence"],
        STATEMENTS["bucket.acquire"],
        STATEMENTS["bucket.fence"],
        STATEMENTS["job.reclaim"],
        STATEMENTS["job.candidates.lane"],
        STATEMENTS["job.claim"],
        STATEMENTS["bucket.release"],
    ]
    # 列表标记由 `check_statement` 摊平成逐值参数:语句里一个 `?`,参数里两个 kind。
    assert database.statements[4].params == (
        Decimal(2),
        "pending",
        NOW,
        EPOCH,
        EPOCH,
        "",
        "transcription",
        "speech_synthesis",
    )


def test_the_admission_anchor_switches_the_statement() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["bucket.fence"]: [
                [fence_row(version=3, owner=None)],
                [fence_row(version=4, owner="worker-a")],
            ],
            STATEMENTS["job.candidates.admitted"]: [[]],
        },
        rowcounts={
            STATEMENTS["bucket.acquire"]: 1,
            STATEMENTS["bucket.release"]: 1,
        },
    )

    assert (
        claim_batch(
            database.connect(),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=2,
            require_admission=True,
        )
        == ()
    )
    assert database.statements[4].sql == STATEMENTS["job.candidates.admitted"]
    assert database.statements[4].params == (
        Decimal(2),
        "pending",
        NOW,
        EPOCH,
        EPOCH,
        "",
    )


def test_lane_and_admission_need_both_predicates() -> None:
    database = FakeDatabase(
        {
            STATEMENTS["bucket.fence"]: [
                [fence_row(version=3, owner=None)],
                [fence_row(version=4, owner="worker-a")],
            ],
            STATEMENTS["job.candidates.lane_admitted"]: [[]],
        },
        rowcounts={
            STATEMENTS["bucket.acquire"]: 1,
            STATEMENTS["bucket.release"]: 1,
        },
    )

    claim_batch(
        database.connect(),
        owner_id="worker-a",
        now=NOW,
        lease_until=LEASE,
        limit=1,
        start_bucket=2,
        kinds=("voice_validation",),
        require_admission=True,
    )
    assert database.statements[4].sql == STATEMENTS["job.candidates.lane_admitted"]
    assert database.statements[4].params[-1] == "voice_validation"


def test_reclaim_expired_rewrites_dead_claims_in_one_bucket() -> None:
    """`job.reclaim` 的参数形状与作用域;行为判据在真库那一份里.

    这里只钉两件静态事实,因为它们是「重领」区别于「候选」的定义:作用域是一个桶(不是
    全表),并且推进 `claim_version`(旧 owner 的 CAS 因此失效)。真库上「过期可重领 /
    未过期不可抢 / 旧 owner 被 fence / 重领不越桶」四条由
    `tests/integration/test_claim_batch_pg.py` 观测。
    """

    database = FakeDatabase({}, rowcounts={STATEMENTS["job.reclaim"]: 3})

    assert reclaim_expired(database.connect(), bucket_no=Decimal(19), now=NOW) == 3
    assert [statement.sql for statement in database.statements] == [STATEMENTS["job.reclaim"]]
    assert database.statements[0].params == ("pending", None, Decimal(19), "claimed", NOW)

    sql = STATEMENTS["job.reclaim"]
    assert "WHERE bucket_no = ?" in sql
    assert "AND job_state = ?" in sql
    assert "claim_expires_at <= ?" in sql
    # 版本加一是 fencing:不涨版本,重领后旧 owner 仍能写成功。
    assert "claim_version = claim_version + 1" in sql


def test_an_empty_lane_is_refused_before_any_statement() -> None:

    database = FakeDatabase()

    with pytest.raises(ContractError, match="at least one value"):
        claim_batch(
            database.connect(),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=0,
            kinds=(),
        )
    assert database.statements == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"limit": 0}, "limit must be a positive integer"),
        ({"page_size": 0}, "page_size must be a positive integer"),
        ({"start_bucket": BUCKETS}, "start_bucket must be an integer"),
        ({"start_bucket": -1}, "start_bucket must be an integer"),
        ({"start_bucket": Decimal("2.5")}, "start_bucket must be an integer"),
        ({"lease_until": NOW}, "lease_until must be after now"),
        ({"owner_id": ""}, "owner_id must not be empty"),
    ],
)
def test_batch_bounds_are_refused_before_any_statement(
    overrides: dict[str, object], message: str
) -> None:
    arguments: dict[str, object] = {
        "owner_id": "worker-a",
        "now": NOW,
        "lease_until": LEASE,
        "limit": 1,
        "start_bucket": 0,
        **overrides,
    }
    database = FakeDatabase()

    with pytest.raises(ContractError, match=message):
        claim_batch(database.connect(), **arguments)  # type: ignore[arg-type]

    assert database.statements == []


def test_the_rotation_start_is_the_owner_plus_the_utc_epoch_minute() -> None:
    """起点规则独立复算:sha256(owner + ":" + UTC epoch 分钟) 的前 8 字节 mod 256."""

    minute = int(NOW.timestamp() // 60)

    def expected(owner: str, minute_number: int) -> Decimal:
        material = f"{owner}:{minute_number}".encode()
        return Decimal(int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % BUCKETS)

    assert start_bucket_for("worker-a", NOW) == expected("worker-a", minute)
    # 同一 UTC 分钟内不变:起点按分钟轮转,不是按调用时刻。
    assert start_bucket_for("worker-a", NOW.replace(second=59)) == expected("worker-a", minute)
    # 下一分钟重新取点。
    later = NOW + timedelta(minutes=1)
    assert start_bucket_for("worker-a", later) == expected("worker-a", minute + 1)
    # 分隔符让前缀相同的 owner 不再共用一个起点。
    assert start_bucket_for("worker-1", NOW) == expected("worker-1", minute)
    assert start_bucket_for("worker-12", NOW) == expected("worker-12", minute)
    with pytest.raises(ContractError, match="owner_id must not be empty"):
        start_bucket_for("", NOW)
    # 非 UTC 的 aware 时刻按同一瞬间取分钟,不与 UTC 写法分叉。
    shifted = NOW.astimezone(timezone(timedelta(hours=8)))
    assert start_bucket_for("worker-a", shifted) == expected("worker-a", minute)


def test_the_claimed_view_maps_the_dispatcher_fields() -> None:
    row = Row(
        (
            "job_id",
            "workspace_id",
            "kind",
            "state",
            "status_version",
            "created_at_us",
            "request_digest",
            "request_document",
        ),
        (
            "job-1",
            "ws-1",
            "transcription",
            "pending",
            Decimal(4),
            Decimal(1_700_000_000_000_000),
            "sha256:" + "d" * 64,
            None,
        ),
    )
    database = FakeDatabase({STATEMENTS["job.claimed_view"]: [[row]]})

    views = claimed_jobs(database.connect(), job_ids=["job-1", "job-2"])

    assert database.statements[0].sql == STATEMENTS["job.claimed_view"]
    assert database.statements[0].params == ("job-1", "job-2")
    assert len(views) == 1
    view = views[0]
    assert (view.job_id, view.workspace_id, view.kind, view.state) == (
        "job-1",
        "ws-1",
        "transcription",
        "pending",
    )
    assert view.status_version == Decimal(4)
    assert view.created_at_us == Decimal(1_700_000_000_000_000)
    assert view.request_digest == "sha256:" + "d" * 64
    # 没有准入行时文档缺失,不用空串冒充。
    assert view.request_document is None


def test_an_empty_claimed_set_does_not_send_the_statement() -> None:
    database = FakeDatabase()

    assert claimed_jobs(database.connect(), job_ids=[]) == ()
    assert database.statements == []
