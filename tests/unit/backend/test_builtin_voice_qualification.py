"""Admission must reject missing, expired, revoked or mismatched qualifications."""

from datetime import UTC, datetime

import pytest

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
    require_builtin_voice,
)


class Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.query = ""
        self.parameters: tuple[object, ...] = ()

    @property
    def rowcount(self) -> int:
        return len(self.rows)

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.query, self.parameters = query, parameters

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows

    def fetchone(self) -> dict[str, object] | None:
        # 时钟读取用 fetchone;替身回答一个固定时间点。
        return {"now": datetime(2026, 11, 22, tzinfo=UTC)}


def test_admission_fails_closed_on_missing_qualification() -> None:
    cursor = Cursor([])
    with pytest.raises(AdmissionRejectedError, match="policy_or_resource_disabled"):
        require_builtin_voice(
            cursor,
            voice_id="voice-demo",
            revision=1,
            revision_id="voice-rev",
            binding_revision_id="binding-rev",
            instance_id="instance-1",
            instance_generation=1,
            host_node_id="host-1",
            gateway_id="gateway-1",
            policy_revision_id="policy-1",
        )
    # 参数顺序必须与语句里 `?` 的真实顺序一致:微秒「现在」在首位,
    # 两处到期判定各用一个「现在」时间点(批准、来源批准),夹在条件之间。
    moment = datetime(2026, 11, 22, tzinfo=UTC)
    assert cursor.parameters == (
        1_795_305_600_000_000,
        "voice-demo",
        "1",
        "voice-rev",
        "binding-rev",
        moment,
        "instance-1",
        "1",
        "host-1",
        "gateway-1",
        "policy-1",
        moment,
    )
    assert "speech_builtin_voice_source_revocations" in cursor.query
    assert "speech_builtin_voice_revocations" in cursor.query
    assert "clock_timestamp()" in cursor.query


def test_admission_rejects_malformed_database_fact() -> None:
    cursor = Cursor([{"facts": {}}])
    with pytest.raises(AdmissionRejectedError):
        require_builtin_voice(
            cursor,
            voice_id="voice-demo",
            revision=1,
            revision_id="voice-rev",
            binding_revision_id="binding-rev",
            instance_id="instance-1",
            instance_generation=1,
            host_node_id="host-1",
            gateway_id="gateway-1",
            policy_revision_id="policy-1",
        )


def test_incomplete_and_unrecognized_qualification_are_rejected() -> None:
    from speechnexus_backend.speech.domain.builtin_voice_qualification import BuiltinVoiceFacts

    with pytest.raises(ValueError, match="incomplete qualification facts"):
        BuiltinVoiceFacts({"required_checks": ["anything"]})
