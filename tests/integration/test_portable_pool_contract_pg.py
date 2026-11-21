"""池契约在真实 PG 上:行形状是 `Row`,拿不到连接是 `PoolUnavailableError`。

业务层只依赖 `Pool` / `Connection` / `Row`;厂商池类型与异常名都留在
`psycopg_pool_driver` 里,这两条用例就是那条边界的证据。
"""

from __future__ import annotations

import os

import pytest

from speechnexus_portable_sql.contract import PoolUnavailableError, Row
from speechnexus_portable_sql.psycopg_pool_driver import create_pool


def _dsn() -> str:
    """缺 DSN 时失败而不是跳过:数据库用例不得把「没配环境」降级成通过。"""

    value = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not value:
        raise AssertionError(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL"
        )
    return value


def test_a_pooled_statement_returns_contract_rows() -> None:
    pool = create_pool(_dsn(), schema="public", max_size=1, name="portable-contract")
    try:
        pool.open()
        with pool.connection(timeout=3) as connection:
            cursor = connection.execute("SELECT ? AS answer", ("forty-two",))
            rows = cursor.fetchall()
            cursor.close()
        assert len(rows) == 1
        assert isinstance(rows[0], Row)
        # 按名与按位都要能读,且不依赖驱动给的大小写。
        assert rows[0]["answer"] == "forty-two"
        assert rows[0].at(0) == "forty-two"
    finally:
        pool.close()


def test_an_unreachable_database_is_a_pool_failure_not_a_statement_failure() -> None:
    """连接拿不到时没有发出任何语句,调用方不能按「已回滚的语句失败」处理。"""

    pool = create_pool(
        "postgresql://speechnexus@127.0.0.1:1/absent",
        schema="public",
        max_size=1,
        name="portable-contract-down",
        timeout=1,
    )
    try:
        pool.open()
        with pytest.raises(PoolUnavailableError), pool.connection(timeout=1):
            pass
    finally:
        pool.close()
