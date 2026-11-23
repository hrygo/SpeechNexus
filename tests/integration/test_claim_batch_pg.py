"""真库上的 `claim_batch`:桶轮询、fence 复核、Job CAS、lane 与准入锚点.

六条判据都来自 `docs/plans/2026-11-21-portable-sql-delivery.md` §8.76.4,而且每条都能被注入
打红(见提交说明):两个 dispatcher 抢同一桶只有一个赢、过期 fence 被拒、Job CAS 竞争只有
一个成功、lane 过滤、准入锚点、租约当场归还。

每个用例在自己的临时 schema 里跑,并在连接上固定 `TimeZone = 'UTC'`:canonical 的时刻列是
`TIMESTAMP(6)`,会话时区不同就会把同一时刻写成不同的墙上时间,而这个错误不会有任何报错。
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_portable_sql import (
    BUCKETS,
    ContractError,
    Row,
    acquire_bucket,
    bucket_for,
    candidates,
    claim_batch,
    claim_job,
    claimed_jobs,
    read_fence,
    reclaim_expired,
    release_bucket,
    require_fence,
)
from speechnexus_portable_sql.contract import Connection

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DDL_FILES = ("migrations/0002_portable_contract.sql", "migrations/0003_portable_claim_buckets.sql")
DIGEST = "sha256:" + "e" * 64
OTHER_DIGEST = "sha256:" + "f" * 64
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=15)
DUE = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
CREATED_US = 1_763_899_200_000_000

#: 认领/派发语句读到的三张准入表。列与生产 DDL 同名,舍掉本切片用不到的约束。
ADMISSION_DDL = (
    """CREATE TABLE speech_jobs (
    job_id VARCHAR(128) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(128) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    state VARCHAR(24) NOT NULL,
    status_version DECIMAL(38,0) NOT NULL,
    created_at_us DECIMAL(38,0) NOT NULL,
    request_digest VARCHAR(71) NOT NULL
)""",
    """CREATE TABLE admission_operations (
    operation_id VARCHAR(128) NOT NULL PRIMARY KEY,
    request_digest VARCHAR(71) NOT NULL,
    request_document TEXT,
    current_attempt_id VARCHAR(128) NOT NULL
)""",
    """CREATE TABLE admission_attempts (
    operation_id VARCHAR(128) NOT NULL,
    attempt_id VARCHAR(128) NOT NULL,
    PRIMARY KEY (operation_id, attempt_id)
)""",
    """CREATE TABLE admission_leases (
    lease_id VARCHAR(128) NOT NULL PRIMARY KEY,
    operation_id VARCHAR(128) NOT NULL,
    attempt_id VARCHAR(128) NOT NULL,
    lease_state VARCHAR(24) NOT NULL,
    cleanup_state VARCHAR(24) NOT NULL
)""",
)


def _contract_rows(cursor: Any) -> Callable[[Any], Row]:
    """psycopg row_factory:按游标描述符把一行折成契约 `Row`.

    claims 的原语按 `row.at(i)` 读,所以测试连接必须交出 `Row`,不能是驱动原生元组。
    """

    names = tuple(str(column.name) for column in (cursor.description or ()))
    return lambda values: Row(names, tuple(values))


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"set {VARIABLE} to a project-isolated database; not a qualification pass")
    return url


@pytest.fixture
def namespace(database_url: str) -> Iterator[tuple[str, str]]:
    name = "sn_batch_" + uuid.uuid4().hex[:12]
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
        for statement in ADMISSION_DDL:
            admin.execute(statement)
        yield database_url, name
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


def _connect(url: str, namespace: str) -> Connection:
    connection = portable_pg_connect(url, row_factory=_contract_rows)
    connection.execute(f'SET search_path TO "{namespace}", pg_catalog')
    connection.execute("SET TimeZone TO 'UTC'")
    connection.commit()
    return cast(Connection, connection)


def _seed_bucket(connection: Connection, bucket: int) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO coordination_claim_buckets (bucket_no, claim_owner_id, lease_until, "
            "version) VALUES (?, ?, ?, ?)",
            (Decimal(bucket), None, None, Decimal(0)),
        )
    finally:
        cursor.close()


def _seed_job(
    connection: Connection,
    job_id: str,
    *,
    bucket: int,
    kind: str = "transcription",
    state: str = "pending",
    request_digest: str = DIGEST,
    admitted: bool = True,
    document: str | None = '{"text":"hello"}',
    document_digest: str = DIGEST,
    attempt_id: str = "attempt-1",
    lease_state: str = "reserved",
    cleanup_state: str = "pending",
    with_attempt: bool = True,
    with_lease: bool = True,
    claim_owner: str | None = None,
    job_state: str = "pending",
    claim_expires_at: datetime | None = None,
    claim_version: int = 0,
) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, request_digest, "
            "bucket_no, due_at, job_state, claim_owner_id, claim_expires_at, claim_version, "
            "last_event_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                f"op-{job_id}",
                attempt_id,
                request_digest,
                Decimal(bucket),
                DUE,
                job_state,
                claim_owner,
                claim_expires_at,
                Decimal(claim_version),
                Decimal(0),
            ),
        )
        cursor.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, state, status_version, "
            "created_at_us, request_digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                f"ws-{job_id}",
                kind,
                state,
                Decimal(1),
                Decimal(CREATED_US),
                request_digest,
            ),
        )
        if admitted:
            cursor.execute(
                "INSERT INTO admission_operations (operation_id, request_digest, "
                "request_document, current_attempt_id) VALUES (?, ?, ?, ?)",
                (job_id, document_digest, document, attempt_id),
            )
            if with_attempt:
                cursor.execute(
                    "INSERT INTO admission_attempts (operation_id, attempt_id) VALUES (?, ?)",
                    (job_id, attempt_id),
                )
            if with_lease:
                cursor.execute(
                    "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, "
                    "lease_state, cleanup_state) VALUES (?, ?, ?, ?, ?)",
                    (f"lease-{job_id}", job_id, attempt_id, lease_state, cleanup_state),
                )
    finally:
        cursor.close()


def _job_state(connection: Connection, job_id: str) -> tuple[str, str | None]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT job_state, claim_owner_id FROM coordination_jobs WHERE job_id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return str(row.at(0)), None if row.at(1) is None else str(row.at(1))


def _claim_row(connection: Connection, job_id: str) -> tuple[str, str | None, Decimal]:
    """`(job_state, claim_owner_id, claim_version)`:重领判据看的就是这三列。"""

    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT job_state, claim_owner_id, claim_version FROM coordination_jobs "
            "WHERE job_id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return str(row.at(0)), (None if row.at(1) is None else str(row.at(1))), Decimal(str(row.at(2)))


def _bucket_row(connection: Connection, bucket: int) -> tuple[object, object, Decimal]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT claim_owner_id, lease_until, version FROM coordination_claim_buckets "
            "WHERE bucket_no = ?",
            (Decimal(bucket),),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return row.at(0), row.at(1), Decimal(str(row.at(2)))


def test_two_claimers_on_one_bucket_produce_exactly_one_winner(
    namespace: tuple[str, str],
) -> None:
    """§8.76.4 判据一:同一桶、同一个 Job,两个 dispatcher 并发,只有一个拿到。"""

    url, name = namespace
    setup = _connect(url, name)
    try:
        _seed_bucket(setup, 0)
        _seed_job(setup, "job-1", bucket=0)
        setup.commit()
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, tuple[str, ...]] = {}

    def contend(worker: str) -> None:
        connection = _connect(url, name)
        try:
            barrier.wait()
            claims = claim_batch(
                connection,
                owner_id=worker,
                now=NOW,
                lease_until=LEASE,
                limit=1,
                start_bucket=0,
            )
            connection.commit()
            results[worker] = tuple(claim.job_id for claim in claims)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=contend, args=(worker,)) for worker in ("worker-a", "worker-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    # 只有一个 dispatcher 认领成功,另一个本轮为空 —— 不是「两个都拿到同一 Job」。
    assert sorted(len(claimed) for claimed in results.values()) == [0, 1], results
    winner = next(worker for worker, claimed in results.items() if claimed)
    loser = next(worker for worker, claimed in results.items() if not claimed)
    reader = _connect(url, name)
    try:
        assert _job_state(reader, "job-1") == ("claimed", winner)
    finally:
        reader.close()
    assert results[winner] == ("job-1",)
    assert results[loser] == ()


def test_a_live_bucket_lease_makes_the_other_owner_skip_the_bucket(
    namespace: tuple[str, str],
) -> None:
    """§8.76.4 判据一(已提交形态):桶被别人持有且租约未过期时,该桶的 Job 不动。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 3)
        _seed_job(connection, "job-1", bucket=3)
        # worker-a 已经持有该桶,租约还没到期。
        assert acquire_bucket(
            connection,
            bucket_no=Decimal(3),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            expected_version=Decimal(0),
        )
        connection.commit()

        assert (
            claim_batch(
                connection,
                owner_id="worker-b",
                now=NOW,
                lease_until=LEASE,
                limit=4,
                start_bucket=3,
            )
            == ()
        )
        connection.commit()
        assert _job_state(connection, "job-1") == ("pending", None)
        assert _bucket_row(connection, 3)[0] == "worker-a"
    finally:
        connection.close()


def test_a_stale_fence_version_is_refused_before_any_job_cas(
    namespace: tuple[str, str],
) -> None:
    """§8.76.4 判据二:用上一轮读到的旧版本,`require_fence` 必须拒绝这条 fence。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 5)
        _seed_job(connection, "job-1", bucket=5)
        connection.commit()

        stale = read_fence(connection, bucket_no=Decimal(5))
        assert stale is not None and stale.version == Decimal(0)
        assert acquire_bucket(
            connection,
            bucket_no=Decimal(5),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            expected_version=stale.version,
        )
        connection.commit()

        # 版本已从 0 走到 1:拿旧版本复核必须被拒,而不是「碰巧还是自己」。
        with pytest.raises(ContractError, match="version moved"):
            require_fence(
                read_fence(connection, bucket_no=Decimal(5)),
                owner_id="worker-a",
                now=NOW,
                expected_version=stale.version,
            )
        # 另一个 owner 拿旧版本做 CAS 也拿不到桶。
        assert not acquire_bucket(
            connection,
            bucket_no=Decimal(5),
            owner_id="worker-b",
            now=NOW,
            lease_until=LEASE,
            expected_version=stale.version,
        )
        connection.rollback()

        # 这一段是批次真正依赖的形态:别人的活租约在,本轮一个都不认领。
        assert (
            claim_batch(
                connection,
                owner_id="worker-b",
                now=NOW,
                lease_until=LEASE,
                limit=2,
                start_bucket=5,
            )
            == ()
        )
        connection.commit()
        assert _job_state(connection, "job-1") == ("pending", None)
    finally:
        connection.close()


def test_job_cas_contention_lets_only_one_transaction_win(namespace: tuple[str, str]) -> None:
    """§8.76.4 判据三:两条事务读同一候选,只有一个 `claim_job` 返回 true。"""

    url, name = namespace
    winner = _connect(url, name)
    loser = _connect(url, name)
    try:
        _seed_bucket(winner, 7)
        _seed_job(winner, "job-1", bucket=7)
        winner.commit()

        # 两条事务在任何人认领之前各读一次候选:输入是同一个 job_id 与同一个版本。
        first_page, _ = candidates(winner, bucket_no=Decimal(7), now=NOW)
        second_page, _ = candidates(loser, bucket_no=Decimal(7), now=NOW)
        assert [candidate.job_id for candidate in first_page] == ["job-1"]
        assert [candidate.job_id for candidate in second_page] == ["job-1"]
        assert first_page[0].claim_version == second_page[0].claim_version == Decimal(0)

        assert claim_job(
            winner,
            job_id="job-1",
            owner_id="worker-a",
            expires_at=LEASE,
            expected_version=first_page[0].claim_version,
        )
        winner.commit()

        # 同一候选、同一版本、同一状态:后到的这条必须 rowcount = 0。
        assert not claim_job(
            loser,
            job_id="job-1",
            owner_id="worker-b",
            expires_at=LEASE,
            expected_version=second_page[0].claim_version,
        )
        loser.commit()
        assert _job_state(winner, "job-1") == ("claimed", "worker-a")

        # 批次层面同一个结论:再轮询一整圈也不会重复认领这个 Job。
        assert (
            claim_batch(
                loser, owner_id="worker-b", now=NOW, lease_until=LEASE, limit=2, start_bucket=7
            )
            == ()
        )
        loser.commit()
    finally:
        winner.close()
        loser.close()


def test_the_lane_filter_only_claims_its_own_kinds(namespace: tuple[str, str]) -> None:
    """§8.76.4 判据四:队列里混入别的 kind,本轮只认领本 lane 的。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 1)
        _seed_job(connection, "job-asr", bucket=1, kind="transcription")
        _seed_job(connection, "job-tts", bucket=1, kind="speech_synthesis")
        connection.commit()

        claims = claim_batch(
            connection,
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=4,
            start_bucket=1,
            kinds=("transcription",),
        )
        connection.commit()

        assert [claim.job_id for claim in claims] == ["job-asr"]
        assert _job_state(connection, "job-asr") == ("claimed", "worker-a")
        # 别的 lane 的 Job 既没被认领,也没消耗本轮的 limit。
        assert _job_state(connection, "job-tts") == ("pending", None)

        other = claim_batch(
            connection,
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=4,
            start_bucket=1,
            kinds=("speech_synthesis",),
        )
        connection.commit()
        assert [claim.job_id for claim in other] == ["job-tts"]
    finally:
        connection.close()


def test_the_admission_anchor_filters_unadmitted_rows(namespace: tuple[str, str]) -> None:
    """§8.76.4 判据五:缺请求文档、摘要不符、无 attempt、lease 不在 reserved/executing 都不认领。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 2)
        _seed_job(connection, "job-ok", bucket=2)
        _seed_job(connection, "job-no-operation", bucket=2, admitted=False)
        _seed_job(connection, "job-redacted", bucket=2, document=None)
        _seed_job(connection, "job-empty-document", bucket=2, document="")
        _seed_job(connection, "job-digest-mismatch", bucket=2, document_digest=OTHER_DIGEST)
        _seed_job(connection, "job-no-attempt", bucket=2, with_attempt=False)
        _seed_job(connection, "job-no-lease", bucket=2, with_lease=False)
        _seed_job(connection, "job-released", bucket=2, lease_state="released")
        _seed_job(connection, "job-cleaned", bucket=2, cleanup_state="done")
        connection.commit()

        claims = claim_batch(
            connection,
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=16,
            start_bucket=2,
            require_admission=True,
        )
        connection.commit()

        assert [claim.job_id for claim in claims] == ["job-ok"]
        for blocked in (
            "job-no-operation",
            "job-redacted",
            "job-empty-document",
            "job-digest-mismatch",
            "job-no-attempt",
            "job-no-lease",
            "job-released",
            "job-cleaned",
        ):
            assert _job_state(connection, blocked) == ("pending", None), blocked
    finally:
        connection.close()


def test_the_bucket_lease_is_gone_before_the_batch_returns(namespace: tuple[str, str]) -> None:
    """§8.76.4 判据六:认领后桶可被另一个 owner 立即拿到,不残留租约。"""

    url, name = namespace
    first = _connect(url, name)
    second = _connect(url, name)
    try:
        _seed_bucket(first, 9)
        _seed_job(first, "job-1", bucket=9)
        _seed_job(first, "job-2", bucket=9)
        first.commit()

        assert [
            claim.job_id
            for claim in claim_batch(
                first, owner_id="worker-a", now=NOW, lease_until=LEASE, limit=1, start_bucket=9
            )
        ] == ["job-1"]
        first.commit()

        # 桶在提交后立即是自由的:owner 清空、租约清空,版本走到 acquire+release。
        owner, lease_until, version = _bucket_row(first, 9)
        assert owner is None
        assert lease_until is None
        assert version == Decimal(2)

        # 另一个 owner 不需要等待租约过期就能拿到同一个桶并认领下一个 Job。
        assert [
            claim.job_id
            for claim in claim_batch(
                second,
                owner_id="worker-b",
                now=NOW + timedelta(seconds=1),
                lease_until=LEASE + timedelta(seconds=1),
                limit=1,
                start_bucket=9,
            )
        ] == ["job-2"]
        second.commit()
        assert _job_state(first, "job-2") == ("claimed", "worker-b")
    finally:
        first.close()
        second.close()


def test_the_claimed_view_returns_the_dispatcher_fields(namespace: tuple[str, str]) -> None:
    """`job.claimed_view` 在真表上返回派发需要的列,并在没有准入行时留空文档."""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 11)
        _seed_job(connection, "job-1", bucket=11, kind="transcription")
        _seed_job(connection, "job-2", bucket=11, admitted=False, kind="speech_synthesis")
        connection.commit()

        views = claimed_jobs(connection, job_ids=["job-1", "job-2"])

        assert [view.job_id for view in views] == ["job-1", "job-2"]
        first, second = views
        assert first.workspace_id == "ws-job-1"
        assert first.kind == "transcription"
        assert first.state == "pending"
        assert first.status_version == Decimal(1)
        assert first.created_at_us == Decimal(CREATED_US)
        assert first.request_digest == DIGEST
        assert first.request_document == '{"text":"hello"}'
        # 没有准入行时摘要回落到 Job 自己的摘要,文档是缺失而不是空串。
        assert second.request_digest == DIGEST
        assert second.request_document is None
    finally:
        connection.close()


def test_the_rotation_covers_every_bucket_once(namespace: tuple[str, str]) -> None:
    """一个 job 落在 `bucket_for` 派生的桶里:从任意起点出发的整圈轮询都会看到它一次。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        job_id = "job-rotated"
        bucket = int(bucket_for(job_id) % BUCKETS)
        _seed_bucket(connection, bucket)
        _seed_job(connection, job_id, bucket=bucket)
        connection.commit()

        claims = claim_batch(
            connection,
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=(bucket + 1) % BUCKETS,
        )
        connection.commit()

        # 起点在 job 的桶之后,必须回绕才能看到它;这正是「全桶轮询」区别于固定分片的地方。
        assert [claim.job_id for claim in claims] == [job_id]
    finally:
        connection.close()


def test_an_expired_bucket_lease_is_taken_over(namespace: tuple[str, str]) -> None:
    """租约过期(owner 还是旧 worker)时,新 owner 可以接管该桶并认领。"""

    url, name = namespace
    setup = _connect(url, name)
    stale = _connect(url, name)
    try:
        _seed_bucket(setup, 13)
        _seed_job(setup, "job-1", bucket=13)
        assert acquire_bucket(
            setup,
            bucket_no=Decimal(13),
            owner_id="worker-a",
            now=NOW,
            lease_until=NOW + timedelta(seconds=1),
            expected_version=Decimal(0),
        )
        # worker-a 的租约 1 秒后到期,而且它没有归还。
        setup.commit()

        assert [
            claim.job_id
            for claim in claim_batch(
                stale,
                owner_id="worker-b",
                now=NOW + timedelta(seconds=2),
                lease_until=LEASE + timedelta(seconds=2),
                limit=1,
                start_bucket=13,
            )
        ] == ["job-1"]
        stale.commit()
        assert _job_state(setup, "job-1") == ("claimed", "worker-b")
    finally:
        setup.close()
        stale.close()


def test_release_after_a_batch_leaves_no_residual_lease(namespace: tuple[str, str]) -> None:
    """直接对照 `release_bucket` 的语义:归还后的 fence 是自由且版本推进过的."""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 17)
        connection.commit()
        assert acquire_bucket(
            connection,
            bucket_no=Decimal(17),
            owner_id="worker-a",
            now=NOW,
            lease_until=LEASE,
            expected_version=Decimal(0),
        )
        assert release_bucket(
            connection, bucket_no=Decimal(17), owner_id="worker-a", expected_version=Decimal(1)
        )
        connection.commit()

        fence = read_fence(connection, bucket_no=Decimal(17))
        assert fence is not None
        assert (fence.owner_id, fence.lease_until, fence.version) == (None, None, Decimal(2))
        # 旧 owner 用旧版本复核必须被拒:桶已经不是它的了。
        with pytest.raises(ContractError, match="owned by"):
            require_fence(fence, owner_id="worker-a", now=NOW, expected_version=Decimal(1))
    finally:
        connection.close()


def test_an_expired_claim_is_reclaimed_and_claimed_again(namespace: tuple[str, str]) -> None:
    """§8.76.6 判据一:租约过期的 `claimed` Job 在下一轮被重领,`claim_version` 连涨两次。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 4)
        _seed_job(
            connection,
            "job-1",
            bucket=4,
            job_state="claimed",
            claim_owner="worker-a",
            claim_expires_at=NOW - timedelta(seconds=1),
            claim_version=1,
        )
        connection.commit()

        claims = claim_batch(
            connection,
            owner_id="worker-b",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=4,
        )
        connection.commit()

        # 重领(1 → 2)之后才进候选,再被 worker-b 认领(2 → 3):版本连涨两次。
        assert [claim.job_id for claim in claims] == ["job-1"]
        assert _claim_row(connection, "job-1") == ("claimed", "worker-b", Decimal(3))
        # 重领写进的是本次认领的租约,不是重领那一刻的 now。
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT claim_expires_at FROM coordination_jobs WHERE job_id = ?", ("job-1",)
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        assert row is not None and row.at(0) is not None
        assert row.at(0) == LEASE.replace(tzinfo=None)
    finally:
        connection.close()


def test_a_live_claim_is_neither_reclaimed_nor_stolen(namespace: tuple[str, str]) -> None:
    """§8.76.6 判据二:租约仍有效的 `claimed` Job 不被重领,也不进候选。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 6)
        _seed_job(
            connection,
            "job-1",
            bucket=6,
            job_state="claimed",
            claim_owner="worker-a",
            claim_expires_at=NOW + timedelta(seconds=5),
            claim_version=1,
        )
        connection.commit()

        assert (
            claim_batch(
                connection,
                owner_id="worker-b",
                now=NOW,
                lease_until=LEASE,
                limit=4,
                start_bucket=6,
            )
            == ()
        )
        connection.commit()
        # 状态、owner、版本都没动:重领的谓词是 `claim_expires_at <= now`。
        assert _claim_row(connection, "job-1") == ("claimed", "worker-a", Decimal(1))
    finally:
        connection.close()


def test_the_reclaim_version_bump_fences_the_old_owner(namespace: tuple[str, str]) -> None:
    """§8.76.6 判据三:重领后旧 owner 用原版本 CAS 必须失败。

    「只靠状态」会被误读:重领后状态回到 `pending`,旧 owner 的 CAS 也会因为状态不符而失败。
    所以这里先让新 owner 重新认领,把状态推回 `claimed`,再让旧 owner 用原版本写;此时唯一
    不符的就是 `claim_version`,失败只能由版本推进解释。
    """

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 10)
        _seed_job(
            connection,
            "job-1",
            bucket=10,
            job_state="claimed",
            claim_owner="worker-a",
            claim_expires_at=NOW - timedelta(seconds=1),
            claim_version=0,
        )
        connection.commit()

        assert reclaim_expired(connection, bucket_no=Decimal(10), now=NOW) == 1
        connection.commit()
        assert _claim_row(connection, "job-1") == ("pending", None, Decimal(1))

        # 新 owner 用重领后的版本认领成功:当前是 (claimed, 2)。
        assert claim_job(
            connection,
            job_id="job-1",
            owner_id="worker-b",
            expires_at=LEASE,
            expected_version=Decimal(1),
        )
        connection.commit()
        assert _claim_row(connection, "job-1") == ("claimed", "worker-b", Decimal(2))

        # 旧 owner 的原版本:状态与它期望的一致(claimed),版本不是 —— CAS 必须失败。
        assert not claim_job(
            connection,
            job_id="job-1",
            owner_id="worker-a",
            expires_at=LEASE + timedelta(seconds=10),
            expected_version=Decimal(0),
            expected_state="claimed",
        )
        connection.commit()
        assert _claim_row(connection, "job-1") == ("claimed", "worker-b", Decimal(2))
    finally:
        connection.close()


def test_reclaim_touches_only_the_named_bucket(namespace: tuple[str, str]) -> None:
    """§8.76.6 判据四:重领只在指定桶内执行,不改别的桶的 Job,也不复活终态行。"""

    url, name = namespace
    connection = _connect(url, name)
    try:
        _seed_bucket(connection, 14)
        _seed_bucket(connection, 15)
        expired = NOW - timedelta(seconds=1)
        _seed_job(
            connection,
            "job-here",
            bucket=14,
            job_state="claimed",
            claim_owner="worker-a",
            claim_expires_at=expired,
            claim_version=0,
        )
        # 同一桶里的终态行带着一个过期的时间戳:状态谓词必须挡住它,不能复活。
        _seed_job(
            connection,
            "job-done",
            bucket=14,
            state="succeeded",
            job_state="succeeded",
            claim_owner="worker-a",
            claim_expires_at=expired,
            claim_version=0,
        )
        # 另一个桶里同样是过期租约:它不在本次重领的作用域里。
        _seed_job(
            connection,
            "job-elsewhere",
            bucket=15,
            job_state="claimed",
            claim_owner="worker-a",
            claim_expires_at=expired,
            claim_version=0,
        )
        connection.commit()

        assert reclaim_expired(connection, bucket_no=Decimal(14), now=NOW) == 1
        connection.commit()

        assert _claim_row(connection, "job-here") == ("pending", None, Decimal(1))
        assert _claim_row(connection, "job-done") == ("succeeded", "worker-a", Decimal(0))
        assert _claim_row(connection, "job-elsewhere") == ("claimed", "worker-a", Decimal(0))
    finally:
        connection.close()
