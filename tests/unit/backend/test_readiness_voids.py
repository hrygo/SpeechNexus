"""就绪收据的显式作废:表、唯一判定与报告/准入同一答案(#1286)。

#1282 收敛之后仍留着一个窗口:准入的依据是**已签发的收据**,分歧再次发生时那张旧收据在
``expires_at_us`` 之前仍然 ``valid``,``create`` 照样放行——报告说红、准入说行,宽度就是收据 TTL。
本切片让「检测到**确定**不一致」变成一个显式动作:续租往 ``runtime_readiness_voids`` 追加一行,
准入与报告按同一条规则判它是否仍然适用。

三条判据都在这份用例里:

* **摘掉作废动作会红**:``void_invalidates_receipt`` 与准入的那一处检查各有一条反例;
* **把「不确定」也当作废会红**:控制器不可达时续租不写任何作废行(见
  ``test_engine_registration.py`` 的两条分支用例);
* **往返**:作废 → 准入拒绝且内部分码点名代次 → 续租签出新收据 → 准入恢复。

列清单在 ``readiness_void_schema`` 里只有一份定义点,所以这里的期望值**刻意独立写死**:判据与
实现同源时,两边一起漂而注入永远不红(同 ``test_generation_adoption_statements`` 的口径)。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from speechnexus_backend import engine_readiness
from speechnexus_backend.speech.domain.readiness_void import void_invalidates_receipt
from speechnexus_backend.speech.infrastructure import admission_sql, readiness_void_schema
from speechnexus_backend.speech.infrastructure.readiness_void_schema import (
    READINESS_VOID_COLUMN_DEFINITIONS,
    READINESS_VOID_COLUMNS,
    READINESS_VOID_DDL,
    READINESS_VOID_INSERT_COLUMNS,
    READINESS_VOID_PRIMARY_KEY,
    READINESS_VOID_REASON_GENERATION_MISMATCH,
    READINESS_VOID_TABLE,
    latest_void_at_us_expression,
)

#: 独立写死的期望列序:不从 ``READINESS_VOID_COLUMN_DEFINITIONS`` 派生。
EXPECTED_COLUMNS = (
    "instance_id",
    "instance_generation",
    "observed_generation",
    "reason",
    "void_source",
    "voided_at_us",
    "document",
)


def test_the_void_table_columns_are_declared_once_in_a_known_order() -> None:
    assert READINESS_VOID_TABLE == "runtime_readiness_voids"
    assert READINESS_VOID_COLUMNS == EXPECTED_COLUMNS
    assert tuple(name for name, _ in READINESS_VOID_COLUMN_DEFINITIONS) == EXPECTED_COLUMNS
    assert READINESS_VOID_INSERT_COLUMNS == EXPECTED_COLUMNS
    assert READINESS_VOID_PRIMARY_KEY == (
        "instance_id",
        "instance_generation",
        "observed_generation",
    )
    assert READINESS_VOID_REASON_GENERATION_MISMATCH == "instance_generation_mismatch"


def test_the_void_table_cannot_hold_two_equal_generations_or_orphans() -> None:
    ddl = "\n".join(READINESS_VOID_DDL)
    # 两代相同就不是分歧:那是一条错误的作废,表级 CHECK 挡住它。
    assert "CHECK (observed_generation <> instance_generation)" in ddl
    # 作废行必须指向真实存在的实例代次,否则删掉实例后无人能解释它为什么被作废。
    assert "FOREIGN KEY (instance_id, instance_generation) REFERENCES engine_instances" in ddl
    assert "PRIMARY KEY (instance_id, instance_generation, observed_generation)" in ddl
    # 只追加:没有 UPDATE/DELETE 面,Downgrade 由迁移负责。
    assert "CREATE TABLE IF NOT EXISTS runtime_readiness_voids" in ddl


def test_the_report_and_admission_share_one_reading_of_which_void_applies() -> None:
    # 报告说 invalid、准入说放行的窗口正是本切片要关的:两边必须**引用同一份**定义,而不是各写
    # 一份等价逻辑。断言读源码里的 import 来源,比断言「行为相同」更直接:一旦谁把那段 SQL 抄进
    # 自己的模块,这条用例就红。
    shared_import = "from speechnexus_backend.speech.infrastructure.readiness_void_schema import"
    for module in (engine_readiness, admission_sql):
        source = inspect.getsource(module)
        assert shared_import in source, module.__name__
        assert "latest_void_at_us_expression(" in source, module.__name__
    assert (
        "from speechnexus_backend.speech.domain.readiness_void import void_invalidates_receipt"
        in inspect.getsource(engine_readiness)
    )
    # 「哪一次作废仍然有效」的 SQL 文本只在 schema 模块里出现一次。
    schema_source = inspect.getsource(readiness_void_schema)
    assert schema_source.count("SELECT MAX(v.voided_at_us)") == 1
    expression = latest_void_at_us_expression(
        instance_column="i.instance_id", generation_column="i.instance_generation"
    )
    assert expression == (
        "(SELECT MAX(v.voided_at_us) FROM runtime_readiness_voids AS v "
        "WHERE v.instance_id = i.instance_id AND v.instance_generation = i.instance_generation)"
    )


@pytest.mark.parametrize(
    ("voided_at_us", "verified_at_us", "invalidates"),
    [
        (None, 100, False),
        (99, 100, False),
        (100, 100, True),
        (101, 100, True),
    ],
)
def test_a_void_invalidates_only_receipts_signed_before_it(
    voided_at_us: int | None, verified_at_us: int, invalidates: bool
) -> None:
    assert (
        void_invalidates_receipt(voided_at_us=voided_at_us, receipt_verified_at_us=verified_at_us)
        is invalidates
    )


def _report_row(document: str, *, voided_at_us: int | None) -> dict[str, Any]:
    """One joined instance/head/intent row, as the report's query returns it."""
    from tests.unit.backend.test_engine_readiness_report import _row

    row = _row(document=document)
    row["readiness_voided_at_us"] = voided_at_us
    return row


def test_a_voided_receipt_reads_invalid_and_names_the_generation() -> None:
    from tests.unit.backend.test_engine_readiness_report import NOW

    receipt = _receipt_for(NOW, verified=NOW - 10, expires=NOW + 60_000_000_000)
    entry = engine_readiness.readiness_report(
        [_report_row(receipt, voided_at_us=NOW - 5)], now_us=NOW, warn_within_us=0
    )[0]
    # 收据自己的窗口还有 60 000 s,但作废是更新的事实。
    assert entry["readiness_state"] == "invalid"
    assert entry["reason"] == "instance_generation_mismatch"
    assert entry["voided_at_us"] == NOW - 5
    assert entry["expires_at_us"] is None
    assert "engine_state_mismatch" in entry["remediation"]
    assert "runtime_readiness_voids" in entry["remediation"]


def test_a_receipt_signed_after_the_void_reads_valid_again() -> None:
    from tests.unit.backend.test_engine_readiness_report import NOW

    receipt = _receipt_for(NOW, verified=NOW - 10, expires=NOW + 60_000_000_000)
    entry = engine_readiness.readiness_report(
        [_report_row(receipt, voided_at_us=NOW - 20)], now_us=NOW, warn_within_us=0
    )[0]
    # 覆盖靠时间序:更早的作废不适用于更晚签出的收据,不需要撤销动作。
    assert entry["readiness_state"] == "valid"
    assert entry.get("reason") is None


def test_a_generation_that_was_never_voided_reads_exactly_as_before() -> None:
    from tests.unit.backend.test_engine_readiness_report import NOW

    receipt = _receipt_for(NOW, verified=NOW - 10, expires=NOW + 60_000_000_000)
    entry = engine_readiness.readiness_report(
        [_report_row(receipt, voided_at_us=None)], now_us=NOW, warn_within_us=0
    )[0]
    assert entry["readiness_state"] == "valid"


def _receipt_for(now_us: int, *, verified: int, expires: int) -> str:
    from tests.unit.backend.test_engine_readiness_report import _receipt

    return _receipt(verified=verified, expires=expires)
