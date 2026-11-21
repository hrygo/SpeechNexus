"""显式事务:提交成功才算提交,执行失败确定回滚,提交异常只报 `commit_unknown`。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress

from speechnexus_portable_sql.contract import (
    CommitUnknownError,
    Connection,
    Cursor,
    TransactionError,
)

__all__ = ["transaction"]


@contextmanager
def transaction(connection: Connection) -> Iterator[Cursor]:
    """打开游标,成功时提交,失败时回滚.

    失败语义:

    - `yield` 内的异常:回滚后以 `TransactionError` 抛出,调用方可确定「没有提交」。
    - `commit()` 抛错:结果不可确认,抛 `CommitUnknownError`。调用方只能按原业务身份查询,
      不得新建执行、不得提前释放预算。
    """

    cursor = connection.cursor()
    try:
        try:
            yield cursor
        except BaseException as error:
            _rollback_quietly(connection)
            raise TransactionError("transaction failed and was rolled back") from error
        else:
            try:
                connection.commit()
            except BaseException as error:
                _rollback_quietly(connection)
                raise CommitUnknownError("commit outcome unknown") from error
    finally:
        cursor.close()


def _rollback_quietly(connection: Connection) -> None:
    # 回滚失败不改变「未提交」这一结论,只丢连接。
    with suppress(Exception):
        connection.rollback()
