"""公共事件摘要的规范算法:一条规则、一个定长向量、不可漂移.

`event_digest` 是 SSE 去重、outbox 幂等与事件重放的共同依据。草案表 `speech_job_events`
没有摘要列,搬迁(#1102)必须按同一规则重算;一旦序列化细节变了,历史事件与新事件的摘要
就对不上,而库里两种摘要并存时没有任何报错会告诉运维。

所以这里钉住三件事:

1. **定长向量**:同一输入必须得到这一个值。改序列化(时间格式、字段集合、分隔符、编码)
   都会让向量变红——这是唯一能提前发现漂移的判据。
2. **时区折叠**:同一时刻的不同时区写法给出同一个摘要。
3. **每字段参与**:任一字段变化都必须改变摘要,否则该字段在去重语义里等于不存在。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from speechnexus_portable_sql.claims import EVENT_DIGEST_FIELDS, event_digest
from speechnexus_portable_sql.contract import ContractError

#: 规则:sha256 over `{"event_seq":1,"job_id":"job-1","kind":null,"occurred_at":
#: "2026-11-23T00:00:00.123456Z","payload_ref":null,"progress_completed":null,
#: "progress_total":null,"state":"running"}`。
GOLDEN = "sha256:4abea1cc8ddf2646b6fb5a6cf34ac219a8bb91a3b486428f6e79dc1135adf529"
MOMENT = datetime(2026, 11, 23, 0, 0, 0, 123456, tzinfo=UTC)


def _base(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "job_id": "job-1",
        "event_seq": Decimal(1),
        "state": "running",
        "occurred_at": MOMENT,
    }
    values.update(overrides)
    return values


def test_the_golden_digest_is_pinned() -> None:
    assert event_digest(**_base()) == GOLDEN


def test_a_shifted_zone_gives_the_same_digest() -> None:
    shifted = MOMENT.astimezone(timezone(timedelta(hours=8)))
    assert event_digest(**_base(occurred_at=shifted)) == GOLDEN


def test_a_naive_timestamp_is_read_as_utc() -> None:
    # 包的既有契约:`require_utc` 把 naive 当 UTC,不拒绝。这里钉住摘要沿用它,
    # 免得搬迁脚本自己另立一条「naive 就是错」的规矩。
    naive = datetime(2026, 11, 23, 0, 0, 0, 123456)
    assert event_digest(**_base(occurred_at=naive)) == GOLDEN


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "job-2"),
        ("event_seq", Decimal(2)),
        ("state", "succeeded"),
        ("occurred_at", datetime(2026, 11, 23, 0, 0, 1, 123456, tzinfo=UTC)),
        ("kind", "progress"),
        ("progress_completed", 1),
        ("progress_total", 2),
        ("payload_ref", "payload/1"),
    ],
)
def test_every_declared_field_participates(field: str, value: object) -> None:
    assert event_digest(**_base(**{field: value})) != GOLDEN, (
        f"{field} does not participate in the digest"
    )


def test_the_field_set_is_explicit_and_ordered() -> None:
    # 加字段就是改公共契约:必须同时改这个元组,让上面那条定长向量重新审核。
    assert EVENT_DIGEST_FIELDS == (
        "job_id",
        "event_seq",
        "state",
        "occurred_at",
        "kind",
        "progress_completed",
        "progress_total",
        "payload_ref",
    )


def test_progress_bounds_are_validated() -> None:
    with pytest.raises(ContractError):
        event_digest(**_base(progress_completed=5, progress_total=4))
    with pytest.raises(ContractError):
        event_digest(**_base(progress_completed=-1))


def test_the_conversion_emits_naive_utc_so_the_session_zone_cannot_change_it() -> None:
    # canonical 列是 TIMESTAMP(6):把 aware datetime 交给驱动,存下来的是会话时区下的墙上
    # 时间。真库演练在 +08:00 会话里把 12:00Z 存成了 20:00,所以换算必须交 naive UTC。
    from speechnexus_portable_sql.claims import canonical_job_from_draft

    row = canonical_job_from_draft(
        job_id="job-1",
        operation_id="op-1",
        attempt_id="attempt-1",
        request_digest="sha256:" + "d" * 64,
        job_state="pending",
        claim_owner_id=None,
        claim_expires_at_us=0,
        claim_version=0,
        due_at_us=int(MOMENT.timestamp() * 1_000_000),
        last_event_seq=0,
    )
    due_at = row["due_at"]
    assert isinstance(due_at, datetime)
    assert due_at.tzinfo is None
    assert due_at == MOMENT.replace(tzinfo=None)
    assert row["claim_expires_at"] is None
