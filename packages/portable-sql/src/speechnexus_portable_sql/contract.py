"""统一 DB-API 契约:PEP 249 的 qmark 子集、行形状与异常分类。

业务只依赖本模块的 `Connection` / `Cursor` 协议与 `transaction()`。驱动实现(`odbc.py`)
与测试替身(`tests/support/portable_sql_fake.py`)都实现同一协议,因此业务用例在真驱动和
fake 上执行的是同一份代码。

边界规则(见 ADR 0016):

- SQL 只使用 `?`;`execute()` 在发送前校验子集、参数风格与参数个数。
- 结果行是**固定列序**的位置值,列名只用于按名读取;不依赖驱动的列名大小写。
- 业务不读取 SQLSTATE 或厂商错误码;CAS 只看 `rowcount`。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

from speechnexus_portable_sql.subset import placeholder_count, validate

__all__ = [
    "CommitUnknownError",
    "ConflictError",
    "Connection",
    "ConnectionFactory",
    "ConnectionLostError",
    "ContractError",
    "Cursor",
    "ExecutableConnection",
    "Param",
    "Params",
    "PoolUnavailableError",
    "PortableSqlError",
    "Row",
    "StorageError",
    "TransactionError",
    "check_parameters",
    "check_statement",
]

#: 允许绑定的参数类型。`bool` 是 `int` 的子类,单独拒绝;`float` 永不用于预算与样本坐标。
type Param = str | int | Decimal | datetime | None
type Params = tuple[Param, ...]


class PortableSqlError(Exception):
    """统一数据路径的错误基类。"""


class ContractError(PortableSqlError, ValueError):
    """调用方违反契约:语句超出子集、参数风格不对、参数个数不符、值类型不允许。"""


class TransactionError(PortableSqlError):
    """执行期间失败,事务已回滚,结果确定没有提交。"""


class CommitUnknownError(PortableSqlError):
    """`commit()` 抛错或结果不可确认:只允许按原业务身份查询,不得新建执行或释放预算。"""


class StorageError(PortableSqlError):
    """语句在存储端失败,事务已回滚。业务只按类别处理,不读 SQLSTATE 或厂商错误码。"""


class ConflictError(StorageError):
    """唯一键或外键冲突。业务据此重读原身份,而不是按厂商错误码分支。"""


class ConnectionLostError(StorageError):
    """连接在执行期间不可用。结果未确认,按 `commit_unknown` 同级处理。"""


class PoolUnavailableError(PortableSqlError):
    """拿不到连接:池已关闭、等待超时或排队已满。与语句失败不同,此时没有发出任何语句。"""


class Row:
    """固定列序的结果行:按序号或列名读取,不依赖驱动的列名大小写。"""

    __slots__ = ("_columns", "_values")

    def __init__(self, columns: Sequence[str], values: Sequence[object]) -> None:
        if len(columns) != len(values):
            raise ContractError("row column and value counts differ")
        self._columns = tuple(columns)
        self._values = tuple(values)

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    def at(self, index: int) -> object:
        """按位置读取,用于列名因驱动而异的表达式(如 `CURRENT_TIMESTAMP`)。"""

        return self._values[index]

    def values(self) -> tuple[object, ...]:
        return self._values

    def keys(self) -> tuple[str, ...]:
        return self._columns

    def items(self) -> tuple[tuple[str, object], ...]:
        return tuple(zip(self._columns, self._values, strict=True))

    def as_dict(self) -> dict[str, object]:
        return dict(self.items())

    def __getitem__(self, key: str | int) -> object:
        if isinstance(key, int):
            return self._values[key]
        try:
            return self._values[self._columns.index(key)]
        except ValueError:
            raise KeyError(key) from None

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._columns

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._columns == other._columns and self._values == other._values
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._columns, self._values))

    def __repr__(self) -> str:
        body = ", ".join(f"{name}={value!r}" for name, value in self.items())
        return f"Row({body})"


@runtime_checkable
class Cursor(Protocol):
    """PEP 249 游标的子集;`rowcount` 是唯一的 CAS 判据。"""

    @property
    def rowcount(self) -> int: ...

    def execute(self, sql: str, params: Params = ()) -> None: ...

    def fetchone(self) -> Row | None: ...

    def fetchmany(self, size: int) -> list[Row]: ...

    def fetchall(self) -> list[Row]: ...

    def close(self) -> None: ...


@runtime_checkable
class Connection(Protocol):
    """关闭 autocommit 的连接;提交与回滚由用例显式调用。"""

    def cursor(self) -> Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class ExecutableConnection(Connection, Protocol):
    """带 `execute` 便利方法的连接:适配器在一条语句上不需要自己开游标."""

    def execute(self, sql: str, params: Sequence[object] = ()) -> Cursor: ...


#: 新建连接的工厂。时间探针必须用独立连接,不能复用持锁的写事务。
type ConnectionFactory = Callable[[], Connection]


def check_parameters(params: Sequence[object]) -> Params:
    """校验并规范化绑定参数;`bool`、`float`、`bytes` 一律拒绝。"""

    normalized: list[Param] = []
    for value in params:
        if value is None or isinstance(value, str):
            normalized.append(value)
        elif isinstance(value, bool):
            raise ContractError("bind Y/N as CHAR(1), not bool")
        elif isinstance(value, int | Decimal):
            normalized.append(value)
        elif isinstance(value, datetime):
            # naive 视为 UTC:ODBC 驱动返回的 TIMESTAMP 通常没有时区;aware 值必须是 UTC。
            offset = value.utcoffset() if value.tzinfo is not None else None
            if offset is not None and offset != timedelta(0):
                raise ContractError("bind UTC datetimes only")
            normalized.append(value)
        else:
            raise ContractError(f"unsupported parameter type: {type(value).__name__}")
    return tuple(normalized)


def check_statement(sql: str, params: Sequence[object]) -> Params:
    """在发送前校验语句子集、参数风格与参数个数。"""

    validate(sql)
    expected = placeholder_count(sql)
    if expected != len(params):
        raise ContractError(f"statement expects {expected} parameters, got {len(params)}")
    return check_parameters(params)
