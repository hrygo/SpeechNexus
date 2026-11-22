"""契约测试用的 fake DB-API:只支持共同子集,没有通知、`SKIP LOCKED`、upsert 或 advisory lock。

为什么用 scripted fake 而不是内存 SQL 引擎:引擎会把「fake 能不能执行这条 SQL」变成新的
语义实现,而这里要证明的恰恰是「同一份业务代码只依赖契约」。所以 fake 只做三件事:

1. 用与真驱动相同的 `check_statement` 校验语句与参数,违规 SQL 在 fake 上同样失败;
2. 记录每条语句与绑定参数,测试据此断言业务发出的 SQL 文本与顺序;
3. 按语句文本回放测试预置的结果集,并允许注入提交失败以覆盖 `commit_unknown`。

fake 不模拟任何品牌方言,也不解析 SQL:它没有方言可模拟。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from speechnexus_portable_sql.contract import (
    Connection,
    Cursor,
    Params,
    Row,
    check_statement,
)

__all__ = ["Executed", "FakeConnection", "FakeDatabase", "as_connection"]


class Executed:
    """一条已执行的语句与它的绑定参数。"""

    __slots__ = ("params", "sql")

    def __init__(self, sql: str, params: Params) -> None:
        self.sql = sql
        self.params = params

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Executed):
            return self.sql == other.sql and self.params == other.params
        return NotImplemented

    def __repr__(self) -> str:
        return f"Executed({self.sql!r}, {self.params!r})"


class FakeDatabase:
    """连接工厂 + 结果回放;每个 `connect()` 返回一条独立连接。"""

    def __init__(
        self,
        results: Mapping[str, Sequence[Sequence[Row]]] | None = None,
        *,
        rowcounts: Mapping[str, int] | None = None,
        fail_commit: bool = False,
        fail_execute: BaseException | None = None,
    ) -> None:
        self._results = {
            sql: [list(page) for page in pages] for sql, pages in (results or {}).items()
        }
        self._rowcounts = dict(rowcounts or {})
        self._fail_commit = fail_commit
        self._fail_execute = fail_execute
        self.connections: list[FakeConnection] = []

    def connect(self) -> FakeConnection:
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def _replay(self, sql: str) -> list[Row]:
        pages = self._results.get(sql)
        if not pages:
            return []
        return pages.pop(0) if len(pages) > 1 else pages[0]

    @property
    def statements(self) -> list[Executed]:
        return [statement for connection in self.connections for statement in connection.statements]


class FakeConnection:
    """关闭 autocommit 的连接替身:提交与回滚都由用例显式调用。"""

    def __init__(self, database: FakeDatabase) -> None:
        self._database = database
        self.statements: list[Executed] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> Cursor:
        if self.closed:
            raise RuntimeError("connection is closed")
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        if self._database._fail_commit:
            raise ConnectionError("simulated commit failure")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    """游标替身:执行前用与真驱动相同的契约校验。"""

    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self._rows: list[Row] = []
        self._rowcount = 0
        self._columns: tuple[str, ...] = ()
        self.closed = False

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def execute(self, sql: str, params: Params = ()) -> None:
        bound = check_statement(sql, params)
        failure = self._connection._database._fail_execute
        if failure is not None:
            raise failure
        self._connection.statements.append(Executed(sql, bound))
        self._rows = list(self._connection._database._replay(sql))
        self._columns = self._rows[0].columns if self._rows else ()
        default = 1 if sql.lstrip()[:6].upper() in {"INSERT", "UPDATE", "DELETE"} else 0
        self._rowcount = self._connection._database._rowcounts.get(sql, default)

    def fetchone(self) -> Row | None:
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int) -> list[Row]:
        page, self._rows = self._rows[:size], self._rows[size:]
        return page

    def fetchall(self) -> list[Row]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        self.closed = True


def as_connection(database: FakeDatabase) -> Connection:
    """类型辅助:让 fake 连接满足 `Connection` 协议。"""

    return database.connect()
