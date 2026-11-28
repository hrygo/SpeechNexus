"""0069 收紧 ``bucket_no``;0070 退役草案 epoch 列。

四条判据,都只能在真库上得到:

* **head 的形状。** 从空 schema ``upgrade head`` 之后 ``bucket_no`` 是 ``NOT NULL``,
  ``claim_expires_at_us`` 不存在,canonical ``claim_expires_at`` 仍在。``due_at`` **仍可空**,
  这是判据不是遗漏:``0001_jobs`` 时代的 ``coordination_jobs`` 行没有 ``speech_jobs`` 行、
  也没有任何时间列,``0065`` 的对账有意允许它们保留 ``NULL``(见
  ``tests/support/legacy_jobs.seed_legacy_job`` 与 ``0069`` 的模块 docstring)。
  这条同时检查整条迁移链仍可从空库重放 —— ``claim_expires_at_us`` 已从 ``JOB_DDL`` 移除,
  由 ``0001`` 自己的 ``_LEGACY_DRAFT_COLUMNS`` 补上,``0064``/``0065`` 才有列可读。
* **约束不是回填的替代品。** 一行 ``bucket_no`` 为 ``NULL`` 的旧形状数据,直接
  ``SET NOT NULL`` 必须是 ``23502``;``0069`` 先回填,同一行才活下来。
* **时区折叠。** 回填在 ``Asia/Shanghai`` 会话里跑,``due_at`` 必须是 UTC 墙上时间
  (``12:00``),不是 ``20:00``。去掉 ``AT TIME ZONE 'UTC'`` 这条断言就红。
* **没有来源的 ``due_at`` 不编造时刻。** 没有对应 ``speech_jobs.created_at_us`` 的行保留
  ``NULL``(「创建时刻未知、不参与派发」),迁移仍然成功、行仍然可读 —— 给这类行写一个算出来
  的时刻等于记录一个没人观测过的事实,而且会把一个历史 Job 改成「立刻到期」。

``0070`` 之后重跑 ``0064``/``0065`` 不再是合法回退:它们的回填语句读这一列,删列之后是
``42703``。最后一条判据把这件事写成测试,而不是只写在计划 §6 里。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from tests.support.migrations import HEAD
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    COORDINATION_JOB_INSERT_COLUMNS,
)
from speechnexus_portable_sql.claims import bucket_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "b" * 64
MOMENT = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)
MOMENT_US = int(MOMENT.timestamp()) * 1_000_000
NAIVE_MOMENT = datetime(2026, 11, 24, 12, 0, 0)
PRE_TIGHTEN = "0068_canonical_event_tables"
PRE_RERUN = "0063_deletion_targets_blocked"


def _config(schema: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    return config


def _engine(url: str) -> Any:
    return create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        connect_args={"options": "-c timezone=Asia/Shanghai"},
    )


def _nullable(admin: psycopg.Connection[Any], schema: str, column: str) -> Any:
    row = admin.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'coordination_jobs' AND column_name = %s",
        (schema, column),
    ).fetchone()
    return None if row is None else row[0]


def _insert_public_job(admin: psycopg.Connection[Any], *, job_id: str) -> None:
    admin.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "idempotency_key_digest, state, status_version, created_at_us, updated_at_us) "
        "VALUES (%s, 'ws-0069', 'asr', 'mdl-0069', %s, %s, 'pending', 1, %s, %s)",
        (job_id, DIGEST, DIGEST, MOMENT_US, MOMENT_US),
    )


def _insert_pre_tighten_row(
    admin: psycopg.Connection[Any], *, job_id: str, claim_expires_at_us: int
) -> None:
    """Write the row the 0065-era writer could still leave behind: ``bucket_no`` NULL."""

    admin.execute(
        "INSERT INTO coordination_jobs "
        "(job_id, operation_id, attempt_id, initial_event_id, initial_event_digest, "
        "request_digest, claim_expires_at_us) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            job_id,
            f"op-{job_id}",
            f"attempt-{job_id}",
            f"{job_id}-queued",
            DIGEST,
            DIGEST,
            claim_expires_at_us,
        ),
    )


def _row(admin: psycopg.Connection[Any], job_id: str) -> tuple[Any, ...]:
    row = admin.execute(
        "SELECT bucket_no, due_at, claim_expires_at FROM coordination_jobs WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None, f"coordination_jobs lost the row for {job_id}"
    return tuple(row)


def test_a_fresh_upgrade_head_has_the_tightened_shape_and_no_draft_epoch_column() -> None:
    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0069_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            head = connection.execute(
                text(f'SELECT version_num FROM "{schema}".alembic_version')
            ).scalar_one()
        assert head == HEAD
        assert _nullable(admin, schema, "bucket_no") == "NO"
        # ``due_at`` 保持可空:0001 时代的行没有时刻来源,见模块 docstring。
        assert _nullable(admin, schema, "due_at") == "YES"
        assert _nullable(admin, schema, "claim_expires_at_us") is None
        # canonical 租约列仍在:收紧与退役都没有碰到它。
        assert _nullable(admin, schema, "claim_expires_at") == "YES"
        # 写入者声明面与 head 形状一致:表里没有的列不会出现在 INSERT 列清单里。
        declared = {
            row[0]
            for row in admin.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'coordination_jobs'",
                (schema,),
            ).fetchall()
        }
        assert set(COORDINATION_JOB_INSERT_COLUMNS) <= declared
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_a_legacy_row_is_backfilled_before_the_constraint_not_dropped() -> None:
    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0069_" + uuid.uuid4().hex[:12]
    job_id = "job-0069-legacy"
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, PRE_TIGHTEN)
        # 0065 之后、收紧之前能合法存在的形状:``bucket_no`` 与 ``due_at`` 都是 NULL,
        # 公开行有 ``created_at_us``,所以 due_at 有来源。
        _insert_public_job(admin, job_id=job_id)
        _insert_pre_tighten_row(admin, job_id=job_id, claim_expires_at_us=0)
        assert _row(admin, job_id)[:2] == (None, None)

        # 约束本身不接受 NULL:没有回填时这条 ALTER 是 23502,不是静默丢行。
        with pytest.raises(psycopg.errors.NotNullViolation) as refused:
            admin.execute("ALTER TABLE coordination_jobs ALTER COLUMN bucket_no SET NOT NULL")
        assert refused.value.sqlstate == "23502"
        assert "bucket_no" in str(refused.value)

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        bucket_no, due_at, claim_expires_at = _row(admin, job_id)
        assert int(bucket_no) == int(bucket_for(job_id))
        # naive UTC 的墙上时间:会话是 +08:00,少了 AT TIME ZONE 'UTC' 会是 20:00。
        assert due_at == NAIVE_MOMENT
        assert claim_expires_at is None
        assert _nullable(admin, schema, "bucket_no") == "NO"
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_a_row_without_a_due_at_source_keeps_null_and_is_still_backfilled() -> None:
    """``0001`` 时代没有公开行的 Job:``bucket_no`` 补齐,``due_at`` 不编造时刻。"""

    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0069_" + uuid.uuid4().hex[:12]
    job_id = "job-0069-orphan"
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, PRE_TIGHTEN)
        # 没有 speech_jobs 行:``due_at`` 没有任何来源(``seed_legacy_job`` 的形状)。
        _insert_pre_tighten_row(admin, job_id=job_id, claim_expires_at_us=0)

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        bucket_no, due_at, _ = _row(admin, job_id)
        assert int(bucket_no) == int(bucket_for(job_id))
        assert due_at is None, "a create time nobody recorded must not be invented"
        assert _nullable(admin, schema, "bucket_no") == "NO"
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_rerunning_0064_after_the_epoch_column_is_gone_is_no_longer_a_rollback() -> None:
    """计划 §6:越过 S5 之后没有 alembic 回退点,重跑旧迁移以 42703 失败。"""

    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0069_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.execute(
                text(f'UPDATE "{schema}".alembic_version SET version_num = :revision'),
                {"revision": PRE_RERUN},
            )
            connection.commit()
            with pytest.raises(ProgrammingError) as refused:
                command.upgrade(config, "head")
        message = str(refused.value)
        assert 'column "claim_expires_at_us" does not exist' in message
        assert getattr(refused.value.orig, "sqlstate", None) == "42703"
        # 版本号没被推进:失败是整笔回滚的,不留半迁移状态。
        version = admin.execute(f'SELECT version_num FROM "{schema}".alembic_version').fetchone()
        assert version == (PRE_RERUN,)
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_the_head_shape_refuses_a_row_without_a_bucket() -> None:
    """``bucket_no`` 的 ``NOT NULL`` 是有判据的:省略它的写入以 ``23502`` 失败。

    这条同时是「不能退回 ``DEFAULT``」的判据:一旦这一列带上默认值,下面的 INSERT 会被
    静默补成 0 号桶,而不是被拒绝 —— 约束就只剩声明,不再约束任何写入。
    """

    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0069_" + uuid.uuid4().hex[:12]
    job_id = "job-0069-required-bucket"
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    columns = (
        "job_id, operation_id, attempt_id, initial_event_id, initial_event_digest, request_digest"
    )
    identity = (job_id, f"op-{job_id}", f"attempt-{job_id}", f"{job_id}-queued", DIGEST, DIGEST)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        with pytest.raises(psycopg.errors.NotNullViolation) as refused:
            admin.execute(
                f"INSERT INTO coordination_jobs ({columns}) VALUES (%s, %s, %s, %s, %s, %s)",
                identity,
            )
        assert refused.value.sqlstate == "23502"
        assert "bucket_no" in str(refused.value)

        # 补充同一个定义点算出的桶值之后,同一行被接受。
        admin.execute(
            f"INSERT INTO coordination_jobs ({columns}, bucket_no) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (*identity, int(bucket_for(job_id))),
        )
        assert int(_row(admin, job_id)[0]) == int(bucket_for(job_id))
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_downgrade_from_head_refuses_at_the_one_way_epoch_step() -> None:
    """计划 §6:``0070`` 没有回退段,所以从 head 往下的第一条 downgrade 就被拒绝。

    拒绝发生在 ``0070`` 自己的 ``downgrade``;版本号和形状都不动 —— 退役的列没有回来,
    ``0069`` 的 ``NOT NULL`` 也还在。
    """

    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0069_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        # 任何比 0070 更低的 target 都必须先穿过它;0068 是最短的那一步。
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="one-way step"):
                command.downgrade(config, PRE_TIGHTEN)

        version = admin.execute(f'SELECT version_num FROM "{schema}".alembic_version').fetchone()
        assert version == (HEAD,)
        assert _nullable(admin, schema, "claim_expires_at_us") is None
        assert _nullable(admin, schema, "bucket_no") == "NO"
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()
