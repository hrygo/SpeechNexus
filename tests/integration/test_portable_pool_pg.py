"""池连接按 `?` 契约执行:真实 PG 上的最小闭环,也是 #1100 的执行路径前提."""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

DATABASE_URL = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")


def _url_schema() -> tuple[str, str]:
    if DATABASE_URL is None:
        pytest.fail("portable pool behavior requires SPEECHNEXUS_TEST_DATABASE_URL")
    schema = f"sn_pool_{uuid.uuid4().hex[:12]}"
    admin = portable_pg_connect(DATABASE_URL, autocommit=True)
    try:
        admin.execute(f"CREATE SCHEMA {schema}")
    finally:
        admin.close()
    return DATABASE_URL, schema


def test_pool_translates_qmark_and_keeps_legacy_pyformat() -> None:
    url, schema = _url_schema()
    pool = speech_pool(url, schema=schema, max_size=1, name="portable-pool")
    try:
        pool.open()
        with pool.connection() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT ? AS answer, ? AS label", (7, "qmark"))
            row = cursor.fetchone()
            assert row is not None
            assert row["answer"] == 7
            assert row["label"] == "qmark"
            connection.commit()

        with pool.connection() as connection:
            cursor = connection.cursor()
            # 迁移期:旧 `%s` 语句必须仍然可用,驱动原样放过。
            cursor.execute("SELECT %s AS legacy", (11,))
            legacy = cursor.fetchone()
            assert legacy is not None
            assert legacy["legacy"] == 11
            connection.commit()

        with pool.connection() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT 1 AS one WHERE 'op%' LIKE ? ", ("op%",))
            filtered = cursor.fetchone()
            assert filtered is not None
            assert filtered["one"] == 1
            connection.rollback()
    finally:
        pool.close()
        admin = portable_pg_connect(url, autocommit=True)
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def test_pool_rejects_untranslated_marker() -> None:
    """注入一个仍在业务层出现的 `?` 语句必须真的到达驱动并被拒绝,证明翻译点确实在链路上."""

    url, schema = _url_schema()
    pool = speech_pool(url, schema=schema, max_size=1, name="portable-pool-bad")
    try:
        pool.open()
        with pool.connection() as connection, pytest.raises(psycopg.errors.SyntaxError):
            connection.cursor().execute("SELECT ? AS broken", None)
    finally:
        pool.close()
        admin = portable_pg_connect(url, autocommit=True)
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()
