"""TTS 取消/准入屏障:一个键、两条语句、无事务即失败.

这一层不碰数据库:断言的是「键文本怎么拼」「先插桶行再锁同一行」「没有事务直接失败」。
真实库上的串行化证据在 ``tests/integration/test_tts_durable_pg.py``。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from speechnexus_backend.speech.infrastructure.tts_cancel_barrier import (
    lock_tts_cancel,
    tts_cancel_lock_key,
)
from speechnexus_portable_sql.contract import ContractError
from speechnexus_portable_sql.keyed_lock import bucket_for_key, key_digest

SAVEPOINT = "portable_sql_insert_if_absent"


class _Executor:
    """既是执行者又是游标:包装过的游标在准入事务里就是这个形状。"""

    def __init__(self, *, schema: str | None = "speech", autocommit: bool = False) -> None:
        self.statements: list[str] = []
        self.params: list[tuple[object, ...]] = []
        self.autocommit = autocommit
        self._schema = schema

    def execute(self, statement: str, params: Any = ()) -> None:
        self.statements.append(statement)
        self.params.append(tuple(params))

    def fetchall(self) -> list[dict[str, Any]]:
        if self.statements and "current_schema()" in self.statements[-1]:
            return [] if self._schema is None else [{"schema": self._schema}]
        if self.statements and "FOR UPDATE" in self.statements[-1]:
            return [{"bucket_no": Decimal(1)}]
        return []


def test_the_key_text_is_the_old_advisory_lock_text() -> None:
    """键文本不变:两侧(以及任何仍按旧文本拼键的参与者)必须算出同一个桶号。"""

    key = tts_cancel_lock_key("speech", "operation-1")

    assert key == "speechnexus:tts-cancel:speech:operation-1"
    # 旧语句是 `'speechnexus:tts-cancel:' || current_schema() || ':' || ?`,逐段对位。
    schema = "speech"
    operation_id = "operation-1"
    assert key == f"{'speechnexus:tts-cancel:'}{schema}{':'}{operation_id}"


def test_the_bucket_row_is_inserted_then_locked_in_the_callers_transaction() -> None:
    executor = _Executor()
    key = "speechnexus:tts-cancel:speech:operation-1"

    lock_tts_cancel(executor, "operation-1")

    assert executor.statements == [
        "SELECT current_schema() AS schema",
        f"SAVEPOINT {SAVEPOINT}",
        "INSERT INTO coordination_keyed_locks (bucket_no) VALUES (?)",
        f"RELEASE SAVEPOINT {SAVEPOINT}",
        "SELECT bucket_no FROM coordination_keyed_locks WHERE bucket_no = ? FOR UPDATE",
    ]
    # 两条语句绑定同一个桶号:先让行存在,再锁同一行。
    assert executor.params[2] == (bucket_for_key(key),)
    assert executor.params[4] == (bucket_for_key(key),)


def test_two_operations_land_on_two_keys_and_a_schema_change_moves_the_bucket() -> None:
    first = tts_cancel_lock_key("speech", "operation-1")
    second = tts_cancel_lock_key("speech", "operation-2")
    other_schema = tts_cancel_lock_key("speech_shadow", "operation-1")

    assert first != second and first != other_schema
    # 键是纯文本推导:同一段文本永远同一个桶号,不同文本的摘要一定不同(桶可能碰)。
    assert bucket_for_key(tts_cancel_lock_key("speech", "operation-1")) == bucket_for_key(first)
    assert key_digest(other_schema) != key_digest(first)


def test_a_connection_without_a_transaction_fails_instead_of_not_locking() -> None:
    executor = _Executor(autocommit=True)

    with pytest.raises(ContractError, match="open transaction"):
        lock_tts_cancel(executor, "operation-1")

    # 只读了 schema,一条锁语句都没发出去。
    assert executor.statements == ["SELECT current_schema() AS schema"]


def test_a_session_without_a_current_schema_is_refused_before_any_lock() -> None:
    executor = _Executor(schema=None)

    with pytest.raises(ContractError):
        lock_tts_cancel(executor, "operation-1")

    assert executor.statements == ["SELECT current_schema() AS schema"]
