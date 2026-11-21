"""`pyodbc` 驱动的统一 DB-API 实现:全产品唯一的真实数据访问入口。

`pyodbc` 原生使用 ODBC `?` 参数标记,因此这里不做任何占位符改写,也不按品牌分支:
换数据库只换 DSN 与底层 ODBC driver。

驱动是可选的(`speechnexus-portable-sql[odbc]`):没有装驱动时本模块仍可导入,
`connect()` 给出可判定的错误,而不是在导入期炸掉整个进程。
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

from speechnexus_portable_sql.contract import (
    Connection,
    ContractError,
    Cursor,
    Params,
    Row,
    check_statement,
)

__all__ = ["OdbcUnavailableError", "connect", "driver_available"]

#: ODBC 参数风格必须由驱动声明为 qmark;声明成别的就不满足契约。
_REQUIRED_PARAMSTYLE = "qmark"


class OdbcUnavailableError(RuntimeError):
    """没有可用的 ODBC 驱动实现:未安装 `pyodbc` extra 或驱动管理器不可用。"""


def _load_pyodbc() -> Any:
    try:
        return importlib.import_module("pyodbc")
    except ModuleNotFoundError as error:
        raise OdbcUnavailableError(
            "pyodbc is not installed; install speechnexus-portable-sql[odbc] and an ODBC driver"
        ) from error


def driver_available() -> bool:
    """是否能在本进程加载 ODBC 驱动实现。"""

    try:
        _load_pyodbc()
    except OdbcUnavailableError:
        return False
    return True


def connect(dsn: str, *, timeout_seconds: float = 3.0) -> Connection:
    """用 ODBC DSN 建立关闭 autocommit 的连接。"""

    pyodbc = _load_pyodbc()
    if getattr(pyodbc, "paramstyle", "") != _REQUIRED_PARAMSTYLE:
        raise ContractError(
            f"ODBC driver declares paramstyle {getattr(pyodbc, 'paramstyle', '?')!r}, "
            f"expected {_REQUIRED_PARAMSTYLE!r}"
        )
    if not 0 < timeout_seconds <= 30:
        raise ContractError("connect timeout must be within (0, 30] seconds")
    raw = pyodbc.connect(dsn, autocommit=False, timeout=int(timeout_seconds))
    return _OdbcConnection(raw)


class _OdbcConnection:
    __slots__ = ("_raw",)

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def cursor(self) -> Cursor:
        return _OdbcCursor(self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


class _OdbcCursor:
    __slots__ = ("_raw",)

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    @property
    def rowcount(self) -> int:
        value = self._raw.rowcount
        return int(value) if value is not None and int(value) >= 0 else 0

    def execute(self, sql: str, params: Params = ()) -> None:
        bound = check_statement(sql, params)
        self._raw.execute(sql, *bound)

    def fetchone(self) -> Row | None:
        row = self._raw.fetchone()
        if row is None:
            return None
        return Row(self._columns(), tuple(row))

    def fetchmany(self, size: int) -> list[Row]:
        if size < 1:
            raise ContractError("fetchmany size must be at least 1")
        return [Row(self._columns(), tuple(row)) for row in self._raw.fetchmany(size)]

    def fetchall(self) -> list[Row]:
        return [Row(self._columns(), tuple(row)) for row in self._raw.fetchall()]

    def close(self) -> None:
        self._raw.close()

    def _columns(self) -> Sequence[str]:
        description = self._raw.description or ()
        return tuple(str(column[0]) for column in description)
