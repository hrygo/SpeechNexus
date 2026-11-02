"""Scheduled fake lock seam, not PostgreSQL concurrency or TTS admission proof."""

import sqlite3
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError, unknown
from speechnexus_backend.speech.infrastructure import admission_postgres as adapter
from speechnexus_backend.speech.infrastructure.admission_codec import prepare_admission
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import revision_is_disabled
from speechnexus_backend.speech.infrastructure.leases_postgres import _DeadlineCursor
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from tests.fake.admission import COMMAND


class Cursor:
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        assert query.startswith("SELECT binding_id")

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"binding_id": "binding-test"}]


@pytest.mark.parametrize("replay_after_lock", [False, True])
def test_disable_winning_lock_is_rechecked_before_admission(
    monkeypatch: pytest.MonkeyPatch,
    replay_after_lock: bool,
) -> None:
    events: list[str] = []
    disabled = [False]
    original = unknown(COMMAND.request.identity)

    def read(*args: Any, **kwargs: Any) -> Any:
        events.append("original")
        return original if disabled[0] and replay_after_lock else None

    def lock(*args: Any) -> None:
        events.append("lock")
        disabled[0] = True  # competing disable committed before lock was acquired

    def check(*args: Any) -> bool:
        events.append("disabled")
        return disabled[0]

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Disabled binding must not reserve resources")

    monkeypatch.setattr(adapter, "read_original", read)
    monkeypatch.setattr(adapter, "lock_binding", lock)
    monkeypatch.setattr(adapter, "revision_is_disabled", check)
    monkeypatch.setattr(adapter, "admit_in_transaction", forbidden)
    port = adapter.PostgresAdmissionTransactions(
        cast(SpeechPool, None),
        max_command_bytes=1048576,
        enforce_binding_disable=True,
    )
    prepared = prepare_admission(COMMAND, max_command_bytes=1048576)
    if replay_after_lock:
        assert port._admit(cast(_DeadlineCursor, Cursor()), prepared) is original
        assert events == ["original", "lock", "original"]
    else:
        with pytest.raises(AdmissionRejectedError, match="policy_or_resource_disabled"):
            port._admit(cast(_DeadlineCursor, Cursor()), prepared)
        assert events == ["original", "lock", "original", "disabled"]


@pytest.mark.parametrize("mode,expected", [("retire", False), ("disable", True)])
def test_binding_retirement_does_not_mean_disabled(mode: str, expected: bool) -> None:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute(
        "CREATE TABLE speech_publication_retirements "
        "(kind TEXT, object_id TEXT, revision_id TEXT, mode TEXT)"
    )
    database.execute(
        "INSERT INTO speech_publication_retirements "
        "VALUES ('binding','binding-test','binding-rev',?)",
        (mode,),
    )

    class BarrierCursor:
        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
            self.cursor = database.execute(query.replace("%s", "?"), parameters)
            return self.cursor

        def fetchall(self) -> list[dict[str, Any]]:
            return [dict(row) for row in self.cursor.fetchall()]

    try:
        assert revision_is_disabled(BarrierCursor(), "binding-test", "binding-rev") is expected
    finally:
        database.close()
