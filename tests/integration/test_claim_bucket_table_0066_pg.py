"""0066 建 ``coordination_claim_buckets`` 并播种,让派发认领在生产 schema 上可用.

派发认领的第一条语句就是 `read_fence`,而这条语句读的表在 alembic 树上原本不存在:
canonical shape(`migrations/0003_portable_claim_buckets.sql`)只在 canonical schema 与
按它建表的演练里存在。缺口的表现不是「少了点性能」,而是 42P01 或 ``None`` → 零认领,
所以本文件的判据全部落在真库上:

* 迁移后表的形状与 canonical 一致,且**正好** ``BUCKETS`` 行种子,全为 ``version = 0``、
  无 owner、无租约——删掉播种循环这条立刻红;
* 迁移**之前** `read_fence` / `claim_batch` 以 42P01 失败,迁移**之后**同一条
  `claim_batch` 能从种子里拿到桶并认领到期 Job——这就是「前置 1」的判据;
* 版本戳回 0065 再 ``upgrade head``(``step_below_*`` 的形态)只补缺失的桶,不重写已存在
  的租约、不重复插入;
* ``downgrade`` 只删这张表,别的协调表与行都留着;
* 授权只落在 speech 侧:`SELECT` + 三列 `UPDATE`,没有 `INSERT`/`DELETE`,gateway 一条
  都没有——从 `SPEECH_UPDATES` 里删掉这行,这条红。

连接固定 ``TimeZone = 'UTC'``:canonical 的时刻列是 ``TIMESTAMP(6)``,会话时区不同会把同一
时刻写成不同的墙上时间,而那个错误不会报错。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_portable_sql.claims import (
    BUCKETS,
    acquire_bucket,
    bucket_for,
    claim_batch,
    read_fence,
    release_bucket,
)
from speechnexus_portable_sql.contract import Row

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PREVIOUS = "0065_canonical_jobs_reconcile"
REVISION = "0066_claim_bucket_table"
TABLE = "coordination_claim_buckets"
DIGEST = "sha256:" + "d" * 64
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=15)
DUE = NOW - timedelta(minutes=1)

#: 迁移后 ``information_schema.columns`` 的逐列期望:canonical shape 本身,
#: 不是「至少包含这几列」。列类型写错(例如 BIGINT 的 bucket_no)会让 CAS 谓词仍然可跑,
#: 只是域变小,所以形状必须整表钉住。
EXPECTED_COLUMNS = [
    ("bucket_no", "numeric", "NO", 38, 0, None, None),
    ("claim_owner_id", "character varying", "YES", None, None, None, 128),
    ("lease_until", "timestamp without time zone", "YES", None, None, 6, None),
    ("version", "numeric", "NO", 38, 0, None, None),
]


@dataclass(frozen=True)
class Sandbox:
    url: str
    schema: str
    engine: Any
    config: Config
    admin: Any

    @property
    def table(self) -> str:
        return f'"{self.schema}".{TABLE}'


def _config(schema: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    return config


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0066_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    admin = portable_pg_connect(url, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute("SET TimeZone TO 'UTC'")
    try:
        yield Sandbox(url=url, schema=schema, engine=engine, config=_config(schema), admin=admin)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def _upgrade(sandbox: Sandbox, revision: str = "head") -> None:
    with sandbox.engine.connect() as connection:
        sandbox.config.attributes["connection"] = connection
        command.upgrade(sandbox.config, revision)


def _downgrade(sandbox: Sandbox, revision: str) -> None:
    with sandbox.engine.connect() as connection:
        sandbox.config.attributes["connection"] = connection
        command.downgrade(sandbox.config, revision)


def _contract_rows(cursor: Any) -> Any:
    """claims 的原语按 ``row.at(i)`` 读,所以连接必须交出契约 ``Row`` 而不是驱动元组。"""

    names = tuple(str(column.name) for column in (cursor.description or ()))
    return lambda values: Row(names, tuple(values))


def _runtime_connection(sandbox: Sandbox) -> Any:
    connection = portable_pg_connect(sandbox.url, row_factory=_contract_rows)
    connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    connection.execute("SET TimeZone TO 'UTC'")
    connection.commit()
    return connection


def _insert_due_job(sandbox: Sandbox, job_id: str) -> int:
    bucket = int(bucket_for(job_id))
    sandbox.admin.execute(
        "INSERT INTO coordination_jobs ("
        "job_id, operation_id, attempt_id, initial_event_id, initial_event_digest, "
        "request_digest, bucket_no, due_at, job_state, claim_version, last_event_seq) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', 0, 0)",
        (
            job_id,
            f"op-{job_id}",
            f"attempt-{job_id}",
            f"{job_id}-queued",
            DIGEST,
            DIGEST,
            bucket,
            DUE.replace(tzinfo=None),
        ),
    )
    return bucket


def _relation(sandbox: Sandbox, name: str) -> Any:
    return sandbox.admin.execute(
        "SELECT to_regclass(%s) AS name", (f"{sandbox.schema}.{name}",)
    ).fetchone()[0]


def _bucket_row(sandbox: Sandbox, bucket_no: int) -> tuple[Any, ...]:
    row = sandbox.admin.execute(
        f"SELECT claim_owner_id, lease_until, version FROM {sandbox.table} WHERE bucket_no = %s",
        (Decimal(bucket_no),),
    ).fetchone()
    assert row is not None, f"{TABLE} has no row for bucket {bucket_no}"
    return tuple(row)


def _count(sandbox: Sandbox, where: str = "TRUE", parameters: tuple[Any, ...] = ()) -> int:
    return int(
        sandbox.admin.execute(
            f"SELECT COUNT(*) FROM {sandbox.table} WHERE {where}", parameters
        ).fetchone()[0]
    )


def test_0066_migration_creates_the_canonical_shape_and_seeds_exactly_buckets(
    sandbox: Sandbox,
) -> None:
    _upgrade(sandbox)

    columns = [
        tuple(row)
        for row in sandbox.admin.execute(
            "SELECT column_name, data_type, is_nullable, numeric_precision, numeric_scale, "
            "datetime_precision, character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (sandbox.schema, TABLE),
        ).fetchall()
    ]
    assert columns == EXPECTED_COLUMNS
    primary_key = [
        str(row[0])
        for row in sandbox.admin.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary",
            (sandbox.table,),
        ).fetchall()
    ]
    assert primary_key == ["bucket_no"]

    seeded = _count(sandbox)
    assert seeded == BUCKETS, f"0066 seeded {seeded} bucket row(s), BUCKETS is {BUCKETS}"
    assert _count(sandbox, "version = 0") == BUCKETS
    assert _count(sandbox, "claim_owner_id IS NULL") == BUCKETS
    assert _count(sandbox, "lease_until IS NULL") == BUCKETS
    bucket_numbers = [
        int(row[0])
        for row in sandbox.admin.execute(
            f"SELECT bucket_no FROM {sandbox.table} ORDER BY bucket_no"
        ).fetchall()
    ]
    assert bucket_numbers == list(range(BUCKETS))


def test_0066_migration_is_what_makes_the_bucket_runtime_work(sandbox: Sandbox) -> None:
    """判据:迁移前 42P01 / 零认领,迁移后同一条 ``claim_batch`` 拿到 Job."""

    job_id = "job-0066-judgement"
    _upgrade(sandbox, PREVIOUS)
    assert _relation(sandbox, TABLE) is None
    bucket = _insert_due_job(sandbox, job_id)

    before = _runtime_connection(sandbox)
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            read_fence(before, bucket_no=Decimal(bucket))
        before.rollback()
        with pytest.raises(psycopg.errors.UndefinedTable):
            claim_batch(
                before,
                owner_id="owner-0066",
                now=NOW,
                lease_until=LEASE,
                limit=1,
                start_bucket=bucket,
            )
    finally:
        before.rollback()
        before.close()

    _upgrade(sandbox)

    after = _runtime_connection(sandbox)
    try:
        claims = claim_batch(
            after,
            owner_id="owner-0066",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=bucket,
        )
        after.commit()
    finally:
        after.close()
    assert [claim.job_id for claim in claims] == [job_id]
    assert claims[0].claim_version == Decimal(1)


def test_0066_migration_gives_the_runtime_a_usable_bucket_lease(sandbox: Sandbox) -> None:
    _upgrade(sandbox)
    bucket = int(bucket_for("job-0066-lease"))
    bucket_no = Decimal(bucket)
    connection = _runtime_connection(sandbox)
    try:
        fence = read_fence(connection, bucket_no=bucket_no)
        assert fence is not None
        assert fence.bucket_no == bucket_no
        assert fence.owner_id is None
        assert fence.lease_until is None
        assert fence.version == Decimal(0)

        assert (
            acquire_bucket(
                connection,
                bucket_no=bucket_no,
                owner_id="owner-0066",
                now=NOW,
                lease_until=LEASE,
                expected_version=Decimal(0),
            )
            is True
        )
        held = read_fence(connection, bucket_no=bucket_no)
        assert held is not None
        assert held.owner_id == "owner-0066"
        assert held.lease_until == LEASE
        assert held.version == Decimal(1)

        # 版本 CAS:拿旧版本再抢一次必须输,而不是把别人的租约覆盖掉。
        assert (
            acquire_bucket(
                connection,
                bucket_no=bucket_no,
                owner_id="owner-other",
                now=NOW,
                lease_until=LEASE,
                expected_version=Decimal(0),
            )
            is False
        )

        assert (
            release_bucket(
                connection,
                bucket_no=bucket_no,
                owner_id="owner-0066",
                expected_version=Decimal(1),
            )
            is True
        )
        released = read_fence(connection, bucket_no=bucket_no)
        assert released is not None
        assert released.owner_id is None
        assert released.lease_until is None
        assert released.version == Decimal(2)
        connection.commit()
    finally:
        connection.close()


def test_0066_migration_lets_claim_batch_claim_a_due_job_through_the_seeded_bucket(
    sandbox: Sandbox,
) -> None:
    _upgrade(sandbox)
    job_id = "job-0066-claim"
    bucket = _insert_due_job(sandbox, job_id)

    connection = _runtime_connection(sandbox)
    try:
        claims = claim_batch(
            connection,
            owner_id="owner-0066",
            now=NOW,
            lease_until=LEASE,
            limit=1,
            start_bucket=bucket,
        )
        connection.commit()
    finally:
        connection.close()

    assert [claim.job_id for claim in claims] == [job_id]
    claimed = sandbox.admin.execute(
        "SELECT job_state, claim_owner_id, claim_version, claim_expires_at "
        "FROM coordination_jobs WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    assert tuple(claimed) == ("claimed", "owner-0066", 1, LEASE.replace(tzinfo=None))
    # 成功路径也归还桶:owner 清空、租约清空、版本推进到 2(acquire 1 + release 1)。
    assert _bucket_row(sandbox, bucket) == (None, None, Decimal(2))


def test_0066_migration_re_entry_repairs_only_missing_buckets(sandbox: Sandbox) -> None:
    """版本戳回 0065 再 ``upgrade head``:已存在的租约不被重写,缺失的桶补齐."""

    _upgrade(sandbox)
    live_lease = (NOW + timedelta(minutes=5)).replace(tzinfo=None)
    sandbox.admin.execute(
        f"UPDATE {sandbox.table} SET claim_owner_id = 'live-owner', lease_until = %s, "
        "version = 7 WHERE bucket_no = 3",
        (live_lease,),
    )
    sandbox.admin.execute(f"DELETE FROM {sandbox.table} WHERE bucket_no IN (0, 1)")
    sandbox.admin.execute("UPDATE alembic_version SET version_num = %s", (PREVIOUS,))

    _upgrade(sandbox)

    assert _count(sandbox) == BUCKETS
    assert _bucket_row(sandbox, 3) == ("live-owner", live_lease, Decimal(7))
    for bucket in (0, 1):
        assert _bucket_row(sandbox, bucket) == (None, None, Decimal(0))
    # 只断言本修订已被应用,不断言它是 head:后续修订(0067 起)会把 head 推走,
    # 把这条写成 == REVISION 会让每个新增迁移都来改这张卡的用例。
    assert (
        sandbox.admin.execute("SELECT version_num FROM alembic_version").fetchone()[0] != PREVIOUS
    )


def test_0066_migration_downgrade_only_drops_the_table(sandbox: Sandbox) -> None:
    # 从 0066 自己开始,而不是从 head:``0070`` 是单向退役,任何「从 head 往下走」的
    # 回退都会先撞上它(downgrade 直接 raise)。本用例的判据是 0066 自己的 downgrade,
    # 而 0066 之下没有那条单向步,所以回环在 0066↔0065 之间做,再向前走到 head。
    _upgrade(sandbox, REVISION)
    job_id = "job-0066-downgrade"
    _insert_due_job(sandbox, job_id)

    _downgrade(sandbox, PREVIOUS)

    assert _relation(sandbox, TABLE) is None
    assert sandbox.admin.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
        PREVIOUS
    )
    # 其余的协调对象与行都还在:本修订只是这张表的所有者。
    for relation in ("coordination_jobs", "coordination_outbox", "speech_job_events"):
        assert _relation(sandbox, relation) is not None
    surviving = sandbox.admin.execute(
        "SELECT job_id, job_state FROM coordination_jobs WHERE job_id = %s", (job_id,)
    ).fetchone()
    assert tuple(surviving) == (job_id, "pending")

    _upgrade(sandbox)
    assert _count(sandbox) == BUCKETS


def test_0066_grant_covers_the_speech_role_only(sandbox: Sandbox) -> None:
    _upgrade(sandbox)
    speech_role = f"{sandbox.schema}_speech"
    gateway_role = f"{sandbox.schema}_gateway"
    sandbox.admin.execute(f'CREATE ROLE "{speech_role}"')
    sandbox.admin.execute(f'CREATE ROLE "{gateway_role}"')
    try:
        statements = coordination_runtime_grants(
            sandbox.schema, speech_role=speech_role, gateway_role=gateway_role
        )
        for statement in statements:
            sandbox.admin.execute(statement)

        bucket_statements = [statement for statement in statements if f'"{TABLE}"' in statement]
        speech_bucket = [
            statement for statement in bucket_statements if f'"{speech_role}"' in statement
        ]
        gateway_bucket = [
            statement for statement in bucket_statements if f'"{gateway_role}"' in statement
        ]
        assert speech_bucket == [
            f'GRANT SELECT ON "{sandbox.schema}"."{TABLE}" TO "{speech_role}"',
            f"GRANT UPDATE (claim_owner_id, lease_until, version) ON "
            f'"{sandbox.schema}"."{TABLE}" TO "{speech_role}"',
        ]
        assert gateway_bucket == []
        assert not any(
            "INSERT" in statement or "DELETE" in statement for statement in bucket_statements
        )

        def table_privilege(role: str, privilege: str) -> bool:
            return bool(
                sandbox.admin.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, sandbox.table, privilege),
                ).fetchone()[0]
            )

        def column_privilege(role: str, column: str) -> bool:
            return bool(
                sandbox.admin.execute(
                    "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')",
                    (role, sandbox.table, column),
                ).fetchone()[0]
            )

        assert table_privilege(speech_role, "SELECT") is True
        assert table_privilege(speech_role, "INSERT") is False
        assert table_privilege(speech_role, "DELETE") is False
        for column in ("claim_owner_id", "lease_until", "version"):
            assert column_privilege(speech_role, column) is True
        assert column_privilege(speech_role, "bucket_no") is False

        assert table_privilege(gateway_role, "SELECT") is False
        assert table_privilege(gateway_role, "INSERT") is False
        assert table_privilege(gateway_role, "DELETE") is False
        for column in ("claim_owner_id", "lease_until", "version"):
            assert column_privilege(gateway_role, column) is False
    finally:
        for role in (speech_role, gateway_role):
            sandbox.admin.execute(f'DROP OWNED BY "{role}"')
            sandbox.admin.execute(f'DROP ROLE "{role}"')
