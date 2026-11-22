"""The admission-time root write must fail closed when the readback disagrees."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.infrastructure.asr_settlement_sql import (
    insert_asr_settlement,
)

_REQUEST = cast(
    Any,
    SimpleNamespace(
        stages=[
            SimpleNamespace(
                lease_id="lease-one", gateway_id="gateway-test", host_node_id="host-test"
            )
        ],
        identity=SimpleNamespace(
            operation_id="operation-" + "a" * 48, attempt_id="attempt-" + "b" * 48
        ),
    ),
)
_ROW = {
    "lease_id": "lease-one",
    "operation_id": "operation-" + "a" * 48,
    "attempt_id": "attempt-" + "b" * 48,
    "gateway_id": "gateway-test",
    "host_node_id": "host-test",
}


class FakeCursor:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        self.statements.append((query, parameters))
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.rows)


def test_insert_reads_back_the_exact_identity() -> None:
    cursor = FakeCursor([_ROW])
    insert_asr_settlement(cursor, _REQUEST)
    assert len(cursor.statements) == 2
    # 判据是「重复准入不改写已有根」,不是某句厂商语法:换成 NOT EXISTS 后仍然成立。
    assert "NOT EXISTS" in cursor.statements[0][0]
    assert "ON CONFLICT" not in cursor.statements[0][0]


def test_insert_rejects_a_root_pinned_to_another_identity() -> None:
    cursor = FakeCursor([{**_ROW, "operation_id": "operation-" + "c" * 48}])
    with pytest.raises(AdmissionRejectedError) as raised:
        insert_asr_settlement(cursor, _REQUEST)
    assert raised.value.code == "reconciliation_required"


def test_insert_rejects_an_empty_readback() -> None:
    with pytest.raises(AdmissionRejectedError) as raised:
        insert_asr_settlement(FakeCursor([]), _REQUEST)
    assert raised.value.code == "reconciliation_required"
