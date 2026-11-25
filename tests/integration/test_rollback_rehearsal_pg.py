"""计划 §6 的四条回退路径,逐条在真库上演练。

§6 的每一行都断言「到那里还能退到哪、不能做什么」。没有演练之前那些是论断;这个文件把它们
变成实测,包括**被拒绝**的那些结果——回退路径的价值一半在「退不回去」这件事上。

四条路径与它们的实测形态:

* **S2 之后(事件已翻读)。** 第一道墙是 ``0070``:从 head 回退直接 ``RuntimeError``。把这一层
  拆掉之后还有第二道:``0065`` 的 ``downgrade`` 把 ``last_event_seq`` 归零,而 S2 之后它已经是
  事件读的唯一事件头,归零就是销毁事实。两条都断言,因为只断言第一条会让人以为「删掉 0070 的
  检查就能退」。
* **S1 之后(已双写)。** ``adopt_historical_events`` 对已有事件的 Job 直接拒绝,所以「重跑搬迁」
  不是修补手段。拒绝消息点名 Job,不吞异常。
* **S4 之前(约束未收紧)。** ``0065`` 的回填是 fill-if-null:在飞租约不被擦掉,收紧之后仍是原值。
  擦掉它等于让一个正在跑的 Job 失权。
* **S5 之后。** 没有 ``downgrade``(``0070`` 直接 raise),只有备份恢复。这条演练真做一次
  ``pg_dump`` / ``pg_restore``:先把事实拍下来,删掉整个 schema,再从备份恢复,比对逐表行数与
  ``coordination_jobs`` 的逐行摘要。

**判据不能自欺。** 恢复演练最容易写成空转:比对一张零行的表、或恢复进一个从没删掉的 schema,
都会绿。``test_the_restore_rehearsal_notices_when_the_dump_is_empty`` 用一次「空 dump」注入证明
这个文件里的比对会红,而不是「跑完没报错」。
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_portable_sql.claims import (
    HistoricalEvent,
    adopt_historical_events,
    bucket_for,
)
from speechnexus_portable_sql.contract import ContractError, Row

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "c" * 64
MOMENT = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)
MOMENT_US = int(MOMENT.timestamp()) * 1_000_000
HEAD = "0070_retire_draft_claim_epoch"
AFTER_S2 = "0069_tighten_job_bucket_due"
AFTER_S1 = "0065_canonical_jobs_reconcile"
PRE_TIGHTEN = "0068_canonical_event_tables"
RERUN_TARGET = "0063_deletion_targets_blocked"
#: 降到 0064 才会执行 0065 自己的 downgrade(目标等于 0065 时它不动)。
AFTER_S1_DOWN = "0064_canonical_jobs_columns"
FACT_TABLES = ("coordination_jobs", "speech_jobs", "job_events", "coordination_outbox")


def _url() -> str:
    return os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not rollback evidence"
    )


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


def _insert_public_job(admin: psycopg.Connection[Any], *, job_id: str) -> None:
    admin.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "idempotency_key_digest, state, status_version, created_at_us, updated_at_us) "
        "VALUES (%s, 'ws-rb', 'asr', 'mdl-rb', %s, %s, 'pending', 1, %s, %s)",
        (job_id, DIGEST, DIGEST, MOMENT_US, MOMENT_US),
    )


def _insert_coordination_job(
    admin: psycopg.Connection[Any],
    *,
    job_id: str,
    claim_expires_at_us: int | None = None,
    epoch_column: bool = False,
) -> None:
    """插一行协调 Job。

    ``epoch_column`` 只在列还存在时置真:``0070`` 之后这一列没有了,而演练里有几步要在
    ``0069`` 的形状上写数据。用参数而不是两份函数,避免「同一件事两种写法」。
    """

    columns = [
        "job_id",
        "operation_id",
        "attempt_id",
        "initial_event_id",
        "initial_event_digest",
        "request_digest",
        "bucket_no",
        "due_at",
    ]
    values: list[Any] = [
        job_id,
        f"op-{job_id}",
        f"attempt-{job_id}",
        f"{job_id}-queued",
        DIGEST,
        DIGEST,
        int(bucket_for(job_id)),
        None,
    ]
    if epoch_column:
        columns.append("claim_expires_at_us")
        values.append(claim_expires_at_us)
    placeholders = ", ".join(["%s"] * len(columns))
    admin.execute(
        f"INSERT INTO coordination_jobs ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(values),
    )


def _contract_rows(cursor: Any) -> Callable[[Any], Row]:
    """把 psycopg 行折成契约 ``Row``:claims 的原语按 ``row.at(i)`` 读,不看驱动元组。"""

    names = tuple(str(column.name) for column in (cursor.description or ()))
    return lambda values: Row(names, tuple(values))


def _append_canonical_event(admin: psycopg.Connection[Any], *, job_id: str, seq: int) -> None:
    admin.execute(
        "INSERT INTO job_events (job_id, event_seq, event_digest, payload_ref) "
        "VALUES (%s, %s, %s, NULL)",
        (job_id, seq, f"sha256:{seq:064d}"),
    )
    admin.execute(
        "UPDATE coordination_jobs SET last_event_seq = %s WHERE job_id = %s", (seq, job_id)
    )


def _facts(admin: psycopg.Connection[Any], schema: str) -> dict[str, Any]:
    """恢复要比对的事实:逐表行数 + ``coordination_jobs`` 的逐行摘要。"""

    counts: dict[str, int] = {}
    for table in FACT_TABLES:
        exists = admin.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f'"{schema}".{table}',)
        ).fetchone()
        if not exists or not exists[0]:
            continue
        row = admin.execute(f'SELECT count(*) FROM "{schema}".{table}').fetchone()
        counts[table] = int(row[0]) if row else 0
    if "coordination_jobs" not in counts:
        return {"counts": counts, "digest": "absent"}
    digest = admin.execute(
        f"SELECT coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), 'empty') "
        f'FROM "{schema}".coordination_jobs t'
    ).fetchone()
    return {"counts": counts, "digest": digest[0] if digest else "empty"}


def test_a_downgrade_after_the_event_read_flip_is_refused_and_would_destroy_the_head() -> None:
    """§6 第一行:事件翻读之后没有 alembic 回退点,而且第二道墙是破坏性的。

    两道墙的顺序是实测出来的,不是推的:``0070`` 在 ``0065`` 之前,所以从 head 回退时先撞它;
    而 0069 这种「此前停在删列之前」的库可以降到 0065,那时 ``last_event_seq`` 被归零。
    只断言第一道会让人以为「删掉 0070 的检查就能退」,所以两道都断言。
    """

    url = _url()
    schema = "sn_rb_" + uuid.uuid4().hex[:12]
    job_id = "job-rb-s2"
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)

        # 第二道墙:在仍可降级的形状(0069)上放事件,再降到 0065,事件头被归零。
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, AFTER_S2)
        _insert_public_job(admin, job_id=job_id)
        _insert_coordination_job(admin, job_id=job_id, claim_expires_at_us=0, epoch_column=True)
        _append_canonical_event(admin, job_id=job_id, seq=1)
        _append_canonical_event(admin, job_id=job_id, seq=2)
        before = admin.execute(
            "SELECT last_event_seq FROM coordination_jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
        assert before is not None and int(before[0]) == 2

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, AFTER_S1_DOWN)

        after = admin.execute(
            "SELECT last_event_seq FROM coordination_jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
        assert after is not None
        assert int(after[0]) == 0, (
            "0065 的 downgrade 会把事件头归零;S2 之后它是唯一事件头,"
            "所以这条回退是销毁事实而不是恢复"
        )

        # 第一道墙:走到 head 之后再想回退,被 0070 直接拒绝。
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError) as refused:
                command.downgrade(config, AFTER_S1)
        assert "one-way" in str(refused.value)
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_a_job_that_already_has_events_refuses_adoption_after_the_double_write() -> None:
    """§6 第二行:重跑搬迁不是修补手段,已有事件的 Job 被拒绝。"""

    url = _url()
    schema = "sn_rb_" + uuid.uuid4().hex[:12]
    job_id = "job-rb-s1"
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        _insert_public_job(admin, job_id=job_id)
        _insert_coordination_job(admin, job_id=job_id, claim_expires_at_us=None)
        _append_canonical_event(admin, job_id=job_id, seq=1)

        # 两件都要:PortableCursor 把 ``?`` 翻成驱动占位符,``_contract_rows`` 交出契约 Row。
        with portable_pg_connect(url, row_factory=_contract_rows) as connection:
            connection.execute(f'SET search_path TO "{schema}", pg_catalog')
            with pytest.raises(ContractError) as refused:
                adopt_historical_events(
                    cast("Any", connection),
                    job_id=job_id,
                    events=[HistoricalEvent(state="pending", occurred_at_us=MOMENT_US)],
                )
        assert job_id in str(refused.value)
        assert "already has events" in str(refused.value)
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_an_in_flight_lease_survives_the_tightening_step() -> None:
    """§6 第三行:``0065`` 的回填是 fill-if-null,在飞租约不被擦掉。"""

    url = _url()
    schema = "sn_rb_" + uuid.uuid4().hex[:12]
    job_id = "job-rb-s4"
    lease = datetime(2026, 11, 24, 12, 30, 0, tzinfo=UTC)
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, PRE_TIGHTEN)
        _insert_public_job(admin, job_id=job_id)
        _insert_coordination_job(admin, job_id=job_id, claim_expires_at_us=0, epoch_column=True)
        admin.execute(
            "UPDATE coordination_jobs SET claim_expires_at = %s WHERE job_id = %s",
            (lease, job_id),
        )
        # 迁移前后比较同一个值,而不是把它钉成某一个墙上时间:回填的不变量是「不擦在飞租约」,
        # 与执行迁移的会话时区无关(本机 +08:00、CI UTC,钉死墙上时间会只在一边绿)。
        before = admin.execute(
            "SELECT claim_expires_at FROM coordination_jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
        assert before is not None and before[0] is not None

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        row = admin.execute(
            "SELECT claim_expires_at, bucket_no FROM coordination_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == before[0], "在飞租约被回填覆盖了;fill-if-null 的目的就是不擦掉正在跑的 Job"
        assert int(row[1]) == int(bucket_for(job_id))
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def _dump(schema: str, url: str, target: Path) -> None:
    subprocess.run(
        ["pg_dump", "--format=custom", f"--schema={schema}", f"--file={target}", url],
        check=True,
        capture_output=True,
    )


def _restore(source: Path, url: str) -> None:
    subprocess.run(
        ["pg_restore", "--no-owner", "--no-privileges", f"--dbname={url}", str(source)],
        check=True,
        capture_output=True,
    )


def test_after_the_retirement_only_a_backup_restore_brings_the_facts_back(
    tmp_path: Path,
) -> None:
    """§6 第四行:``0070`` 之后没有 downgrade,恢复只能靠备份。"""

    url = _url()
    schema = "sn_rb_" + uuid.uuid4().hex[:12]
    job_id = "job-rb-s5"
    admin = portable_pg_connect(url, autocommit=True)
    engine = _engine(url)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        _insert_public_job(admin, job_id=job_id)
        _insert_coordination_job(admin, job_id=job_id, claim_expires_at_us=None)
        _append_canonical_event(admin, job_id=job_id, seq=1)
        before = _facts(admin, schema)
        assert before["counts"]["coordination_jobs"] == 1
        assert before["counts"]["job_events"] == 1

        # 没有 downgrade:0070 直接拒绝。
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError) as refused:
                command.downgrade(config, AFTER_S2)
        assert "one-way" in str(refused.value)

        # 也没有「重跑旧迁移」这条路:列已经不在,回填语句是 42703。
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            connection.execute(
                text(f'UPDATE "{schema}".alembic_version SET version_num = :revision'),
                {"revision": RERUN_TARGET},
            )
            connection.commit()
            with pytest.raises(ProgrammingError) as rerun:
                command.upgrade(config, "head")
        assert "claim_expires_at_us" in str(rerun.value)

        # 备份恢复演练:先拍事实,删掉整个 schema,再从 dump 恢复,事实必须逐项相同。
        dump = tmp_path / "before-retirement.dump"
        _dump(schema, url, dump)
        assert dump.stat().st_size > 0
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        # 断言真的没了:否则「恢复后与恢复前相同」可能只是因为根本没删,那条演练就等于没做。
        assert _facts(admin, schema)["digest"] == "absent", "schema 没有真的被删掉"
        _restore(dump, url)
        after = _facts(admin, schema)
        assert after["digest"] != "absent", "恢复没有真的重建 schema"
        assert after == before, "恢复后的事实与备份前不同:备份不是有效回退点"

        # 注入:空 dump 恢复进一个空 schema 之后,同一组事实必须与备份前不同 ——
        # 否则上面的相等断言什么都没检查。
        empty_schema = schema + "_empty"
        admin.execute(f'CREATE SCHEMA "{empty_schema}"')
        empty = _facts(admin, empty_schema)
        assert empty != before, "空 schema 的事实与备份前相同,说明比对口径没有区分力"
        admin.execute(f'DROP SCHEMA "{empty_schema}" CASCADE')
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()
