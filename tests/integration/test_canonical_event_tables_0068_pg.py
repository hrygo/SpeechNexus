"""0068 建 canonical ``job_events``、给 ``coordination_outbox`` 加 ``delivered_at``。

事件侧切 canonical 之前,canonical 事件对象在生产 schema 里**根本不存在**:``0003`` 声明了
``job_events`` 与 ``coordination_outbox.delivered_at``,但 alembic 链到 ``0067`` 为止没有这张表,
outbox 上只有草案的 ``delivered_at_us BIGINT``。所以本文件的判据全部落在真库上,而且都是形状判据
而不是「迁移退出码为 0」:

* ``job_events`` 的列、类型、NULL 性与主键**整表**钉住 —— 列类型写错(例如把 ``event_seq`` 写成
  ``BIGINT``)不会让建表失败,只会让 ``Decimal`` 绑定在某个驱动上悄悄收敛域;
* ``coordination_outbox.delivered_at`` 必须是 ``timestamp(6)`` 可空,且 ``delivered_at_us``
  **仍在** —— 本修订是加法,删列不属于它;
* 外键指向 ``coordination_jobs(job_id)``:没有它,Job 删掉之后仍会有读不到的事件行;
* 幂等:把 ``alembic_version`` 戳回 ``0067`` 再 ``upgrade head``,以及直接把本模块的 DDL 再执行
  一遍,都不报错,且**已有事件行不丢** —— 去掉 ``IF NOT EXISTS`` 两条守卫,这一步立刻以
  ``DuplicateTable`` / ``DuplicateColumn`` 变红;
* ``downgrade`` 只删本修订加的两样东西:``delivered_at`` 列与 ``job_events`` 表,``delivered_at_us``
  的值与 ``coordination_jobs`` 的行都留着;
* 授权面按列:``job_event.read`` 的三个投影列可 ``SELECT``,四个可写列可 ``INSERT``,事件表上
  **没有** ``UPDATE``/``DELETE``(只追加),outbox 只授 ``delivered_at``。

连接固定 ``TimeZone = 'UTC'``:canonical 的时刻列是 ``TIMESTAMP(6)``,会话时区不同会把同一时刻
写成不同的墙上时间,而那个错误不会报错。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.infrastructure.canonical_event_schema import (
    COORDINATION_OUTBOX_TABLE,
    JOB_EVENT_COLUMNS,
    JOB_EVENT_DDL,
    JOB_EVENT_TABLE,
    OUTBOX_DELIVERED_AT_COLUMN,
    canonical_event_grants,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PREVIOUS = "0067_coordination_keyed_locks"
REVISION = "0068_canonical_event_tables"
#: 表名不在这里重写:它们只在 ``canonical_event_schema`` 有一份字面量。
EVENT_TABLE = JOB_EVENT_TABLE
OUTBOX_TABLE = COORDINATION_OUTBOX_TABLE
DIGEST = "sha256:" + "d" * 64
NOW = datetime(2026, 11, 26, 9, 0, 0, tzinfo=UTC)

#: 迁移后 ``information_schema.columns`` 的逐列期望:canonical shape 本身,不是「至少包含这几
#: 列」。列类型写错(``BIGINT`` 的 ``event_seq``、``TEXT`` 的 ``payload_ref``)不会让任何语句报错,
#: 只会让域变小,所以形状必须整表钉住。
EXPECTED_EVENT_COLUMNS = [
    ("job_id", "character varying", "NO", None, None, None, 128),
    ("event_seq", "numeric", "NO", 38, 0, None, None),
    ("event_digest", "character varying", "NO", None, None, None, 71),
    ("payload_ref", "character varying", "YES", None, None, None, 512),
]

#: ``delivered_at`` 的期望:``TIMESTAMP(6)`` 可空。``datetime_precision`` 为 6 是「微秒列」的判据,
#: 少了它 ``TIMESTAMP`` 默认精度 6 也成立,所以两者一起钉。
EXPECTED_DELIVERED_AT = ("delivered_at", "timestamp without time zone", "YES", 6)


@dataclass(frozen=True)
class Sandbox:
    url: str
    schema: str
    engine: Any
    config: Config
    admin: Any

    @property
    def events(self) -> str:
        return f'"{self.schema}".{EVENT_TABLE}'

    @property
    def outbox(self) -> str:
        return f'"{self.schema}".{OUTBOX_TABLE}'


def _config(schema: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    return config


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0068_" + uuid.uuid4().hex[:12]
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


def _relation(sandbox: Sandbox, name: str) -> Any:
    return sandbox.admin.execute(
        "SELECT to_regclass(%s) AS name", (f"{sandbox.schema}.{name}",)
    ).fetchone()[0]


def _columns(sandbox: Sandbox, table: str) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in sandbox.admin.execute(
            "SELECT column_name, data_type, is_nullable, numeric_precision, numeric_scale, "
            "datetime_precision, character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (sandbox.schema, table),
        ).fetchall()
    ]


def _primary_key(sandbox: Sandbox, table: str) -> list[str]:
    return [
        str(row[0])
        for row in sandbox.admin.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary",
            (f"{sandbox.schema}.{table}",),
        ).fetchall()
    ]


def _version(sandbox: Sandbox) -> str:
    return str(sandbox.admin.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def _insert_job(sandbox: Sandbox, job_id: str) -> None:
    sandbox.admin.execute(
        "INSERT INTO coordination_jobs ("
        "job_id, operation_id, attempt_id, initial_event_id, initial_event_digest, "
        "request_digest, bucket_no, due_at, job_state, claim_version, last_event_seq) "
        "VALUES (%s, %s, %s, %s, %s, %s, 0, %s, 'pending', 0, 0)",
        (
            job_id,
            f"op-{job_id}",
            f"attempt-{job_id}",
            f"{job_id}-queued",
            DIGEST,
            DIGEST,
            NOW.replace(tzinfo=None),
        ),
    )


def test_0068_creates_the_canonical_event_table(sandbox: Sandbox) -> None:
    _upgrade(sandbox)

    assert _relation(sandbox, EVENT_TABLE) is not None
    assert _columns(sandbox, EVENT_TABLE) == EXPECTED_EVENT_COLUMNS
    # 主键就是事件顺序本身:(job_id, event_seq) 连续,没有第二套公开序号。
    assert _primary_key(sandbox, EVENT_TABLE) == ["job_id", "event_seq"]

    foreign_keys = [
        tuple(row)
        for row in sandbox.admin.execute(
            "SELECT c.conrelid = to_regclass(%s) AS on_events, "
            "c.confrelid = to_regclass(%s) AS references_jobs, "
            "ARRAY(SELECT att.attname FROM unnest(c.conkey) AS k(attnum) "
            "JOIN pg_attribute att ON att.attrelid = c.conrelid AND att.attnum = k.attnum "
            "ORDER BY k.attnum) AS columns, "
            "ARRAY(SELECT att.attname FROM unnest(c.confkey) AS k(attnum) "
            "JOIN pg_attribute att ON att.attrelid = c.confrelid AND att.attnum = k.attnum "
            "ORDER BY k.attnum) AS referenced, "
            "c.confdeltype, c.confupdtype "
            "FROM pg_constraint c WHERE c.conrelid = to_regclass(%s) AND c.contype = 'f'",
            (
                f"{sandbox.schema}.{EVENT_TABLE}",
                f"{sandbox.schema}.coordination_jobs",
                f"{sandbox.schema}.{EVENT_TABLE}",
            ),
        ).fetchall()
    ]
    # 只有一条外键,指向 coordination_jobs(job_id),且不是级联删除:Job 删掉时不允许静默带走
    # 事件行,那会让「事件条数和 Job 生命周期」的关系失去证据。
    assert foreign_keys == [(True, True, ["job_id"], ["job_id"], "a", "a")]

    # 声明面与真库一致:列清单只有一个定义点,这条让「改了模块没跑迁移」也红。
    assert [column[0] for column in EXPECTED_EVENT_COLUMNS] == list(JOB_EVENT_COLUMNS)


def test_0068_adds_delivered_at_and_keeps_the_draft_column(sandbox: Sandbox) -> None:
    _upgrade(sandbox)

    delivered_at = [
        (row[0], row[1], row[2], row[5])
        for row in _columns(sandbox, OUTBOX_TABLE)
        if row[0] == OUTBOX_DELIVERED_AT_COLUMN
    ]
    assert delivered_at == [EXPECTED_DELIVERED_AT]

    # 加法:草案列仍在,形状不变。它仍有读者,删列是写者切换之后的单向步骤。
    draft = [
        (row[0], row[1], row[2])
        for row in _columns(sandbox, OUTBOX_TABLE)
        if row[0] == "delivered_at_us"
    ]
    assert draft == [("delivered_at_us", "bigint", "YES")]


def test_0068_re_entry_repairs_nothing_and_keeps_existing_events(sandbox: Sandbox) -> None:
    """幂等:重入同一修订不报错,且已有事件行不被重建掉。"""

    _upgrade(sandbox)
    job_id = "job-0068-reentry"
    _insert_job(sandbox, job_id)
    sandbox.admin.execute(
        f"INSERT INTO {sandbox.events} (job_id, event_seq, event_digest, payload_ref) "
        "VALUES (%s, 1, %s, NULL)",
        (job_id, DIGEST),
    )
    sandbox.admin.execute(
        f"INSERT INTO {sandbox.outbox} (event_id, job_id, event_digest) VALUES (%s, %s, %s)",
        (f"{job_id}-e1", job_id, DIGEST),
    )

    # 1) 把版本戳回 0067 再 ``upgrade head``:0068 在「对象已经在」的 schema 上重跑。
    sandbox.admin.execute("UPDATE alembic_version SET version_num = %s", (PREVIOUS,))
    _upgrade(sandbox)
    assert _version(sandbox) == REVISION

    # 2) 直接把本模块的 DDL 再执行一遍。去掉 ``IF NOT EXISTS``,这一句就以
    #    ``DuplicateTable`` / ``DuplicateColumn`` 失败 —— 也就是下面这个循环的判据。
    for statement in JOB_EVENT_DDL:
        sandbox.admin.execute(statement)

    assert _columns(sandbox, EVENT_TABLE) == EXPECTED_EVENT_COLUMNS
    delivered_at = [
        (row[0], row[1], row[2], row[5])
        for row in _columns(sandbox, OUTBOX_TABLE)
        if row[0] == OUTBOX_DELIVERED_AT_COLUMN
    ]
    assert delivered_at == [EXPECTED_DELIVERED_AT]
    assert sandbox.admin.execute(
        f"SELECT job_id, event_seq FROM {sandbox.events} WHERE job_id = %s", (job_id,)
    ).fetchall() == [(job_id, 1)]
    assert sandbox.admin.execute(
        f"SELECT event_id, delivered_at FROM {sandbox.outbox} WHERE job_id = %s", (job_id,)
    ).fetchall() == [(f"{job_id}-e1", None)]


def test_0068_downgrade_only_removes_what_it_added(sandbox: Sandbox) -> None:
    _upgrade(sandbox)
    job_id = "job-0068-downgrade"
    _insert_job(sandbox, job_id)
    sandbox.admin.execute(
        f"INSERT INTO {sandbox.outbox} (event_id, job_id, event_digest, delivered_at_us) "
        "VALUES (%s, %s, %s, %s)",
        (f"{job_id}-e1", job_id, DIGEST, 1_800_000_000_000_000),
    )

    _downgrade(sandbox, PREVIOUS)

    assert _relation(sandbox, EVENT_TABLE) is None
    assert _version(sandbox) == PREVIOUS
    assert OUTBOX_DELIVERED_AT_COLUMN not in [row[0] for row in _columns(sandbox, OUTBOX_TABLE)]
    # 草案列的值与 Job 行都还在:回退不丢本修订之外的任何事实。
    assert sandbox.admin.execute(
        f"SELECT event_id, delivered_at_us FROM {sandbox.outbox} WHERE job_id = %s", (job_id,)
    ).fetchall() == [(f"{job_id}-e1", 1_800_000_000_000_000)]
    assert sandbox.admin.execute(
        "SELECT job_id, job_state FROM coordination_jobs WHERE job_id = %s", (job_id,)
    ).fetchall() == [(job_id, "pending")]

    _upgrade(sandbox)
    assert _columns(sandbox, EVENT_TABLE) == EXPECTED_EVENT_COLUMNS


def test_0068_grants_only_the_declared_event_surface(sandbox: Sandbox) -> None:
    _upgrade(sandbox)
    role = f"{sandbox.schema}_events"
    sandbox.admin.execute(f'CREATE ROLE "{role}"')

    def held(privilege: str, table: str, column: str) -> bool:
        return bool(
            sandbox.admin.execute(
                "SELECT has_column_privilege(%s, %s, %s, %s) AS held",
                (role, f"{sandbox.schema}.{table}", column, privilege),
            ).fetchone()[0]
        )

    try:
        for statement in canonical_event_grants(sandbox.schema, role):
            sandbox.admin.execute(statement)

        # 读者只有三个投影列:``job_id`` 出现在 ``WHERE`` 里,不需要 SELECT 授权。
        assert held("SELECT", EVENT_TABLE, "event_seq")
        assert held("SELECT", EVENT_TABLE, "event_digest")
        assert held("SELECT", EVENT_TABLE, "payload_ref")
        assert not held("SELECT", EVENT_TABLE, "job_id")
        # 写者要写整行:INSERT 覆盖每一个声明列。
        for column in JOB_EVENT_COLUMNS:
            assert held("INSERT", EVENT_TABLE, column), column
        # 只追加:事件行不可被改写或删除。``DELETE`` 在 PostgreSQL 没有列级形式,所以它只有
        # 表级判据;``UPDATE`` 逐列判,因为列级 UPDATE 才是这里可能出现的授权形态。
        for column in JOB_EVENT_COLUMNS:
            assert not held("UPDATE", EVENT_TABLE, column), column
        assert not bool(
            sandbox.admin.execute(
                "SELECT has_table_privilege(%s, %s, 'DELETE') AS held",
                (role, f"{sandbox.schema}.{EVENT_TABLE}"),
            ).fetchone()[0]
        )
        # outbox 只授本修订加的那一列;草案的 ``delivered_at_us`` 归 ``job_runtime_grants``。
        assert held("INSERT", OUTBOX_TABLE, OUTBOX_DELIVERED_AT_COLUMN)
        assert held("UPDATE", OUTBOX_TABLE, OUTBOX_DELIVERED_AT_COLUMN)
        assert not held("UPDATE", OUTBOX_TABLE, "delivered_at_us")
    finally:
        # 授权本身是「角色依赖对象」的反向依赖,DROP ROLE 会以 DependentObjectsStillExist
        # 失败;先收回该角色持有的一切,再删角色,schema 由 fixture 用 CASCADE 收尾。
        sandbox.admin.execute(f'DROP OWNED BY "{role}"')
        sandbox.admin.execute(f'DROP ROLE "{role}"')


def test_0068_migration_is_what_makes_the_event_table_exist(sandbox: Sandbox) -> None:
    """判据:迁移前关系不存在,迁移后才存在 —— 不是「建表语句存在」而是「真库里有」。"""

    _upgrade(sandbox, PREVIOUS)
    assert _relation(sandbox, EVENT_TABLE) is None
    with pytest.raises(psycopg.errors.UndefinedTable):
        sandbox.admin.execute(f"SELECT job_id FROM {sandbox.events}")

    _upgrade(sandbox)
    assert _relation(sandbox, EVENT_TABLE) is not None
    assert sandbox.admin.execute(f"SELECT job_id FROM {sandbox.events}").fetchall() == []
