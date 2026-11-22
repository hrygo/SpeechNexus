"""池契约与厂商异常翻译:业务层不该再看见 psycopg 的类型名."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_portable_sql.contract import (
    ConflictError,
    ConnectionLostError,
    PoolUnavailableError,
    StorageError,
)
from speechnexus_portable_sql.psycopg_driver import portable_error
from speechnexus_portable_sql.psycopg_pool_driver import PsycopgPool


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (psycopg.errors.UniqueViolation("dup"), ConflictError),
        (psycopg.errors.ForeignKeyViolation("fk"), ConflictError),
        (psycopg.OperationalError("down"), ConnectionLostError),
        (psycopg.InterfaceError("closed"), ConnectionLostError),
        (psycopg.errors.UndefinedTable("gone"), StorageError),
    ],
)
def test_vendor_errors_become_one_portable_category(
    error: BaseException, expected: type[BaseException]
) -> None:
    translated = portable_error(error)
    assert type(translated) is expected
    # 分类必须落在 PortableSqlError 之下,业务只按类别处理。
    assert translated.__cause__ is None or isinstance(translated, StorageError)


def test_an_unrelated_error_is_not_disguised_as_storage() -> None:
    """编程错误不能被伪装成存储失败,否则「失败即回滚」的判据会失真。"""

    error = ValueError("boom")
    assert portable_error(error) is error


class _FailingPool:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def open(self) -> None: ...

    def close(self) -> None: ...

    def connection(self, *, timeout: float | None = None) -> object:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [PoolClosed("closed"), PoolTimeout("timeout"), TooManyRequests("full")],
)
def test_pool_failures_are_told_apart_from_statement_failures(error: BaseException) -> None:
    """拿不到连接时没有发出任何语句,调用方不能按「语句失败已回滚」处理。"""

    pool = PsycopgPool(_FailingPool(error))  # type: ignore[arg-type]
    with pytest.raises(PoolUnavailableError), pool.connection(timeout=1):
        pass
