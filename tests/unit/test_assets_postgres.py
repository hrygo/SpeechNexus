"""Metadata adapter error boundaries and guards; real SQL is integration-tested."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import Any, cast
from unittest.mock import MagicMock, Mock

import psycopg
import pytest

from speechnexus_backend.speech.domain.assets import AssetError, UploadResult
from speechnexus_backend.speech.infrastructure.assets_postgres import PostgresAssetMetadata, _Cursor
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from tests.unit.test_assets import INSPECTION, INTENT, POLICY, PROOF, READY, RESERVED, WRITING


def adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[PostgresAssetMetadata, Mock]:
    metadata = PostgresAssetMetadata(cast(SpeechPool, Mock()))
    cursor = Mock(spec=_Cursor)
    cursor.rows.return_value = []
    cursor.asset.return_value = WRITING
    cursor.proof.return_value = None
    cursor.workspace.return_value = True
    cursor.clock.return_value = 3

    @contextmanager
    def transaction(**kwargs: Any) -> Any:
        yield cursor

    monkeypatch.setattr(metadata, "_transaction", transaction)
    return metadata, cursor


def row() -> dict[str, Any]:
    stored = asdict(READY)
    stored.update(
        asset_state=stored.pop("status"),
        upload_phase=stored.pop("phase"),
        media_type=READY.media.media_type,
        duration_ms=None,
        sample_rate_hz=None,
        channels=None,
    )
    stored.pop("media")
    return stored


def test_replay_resolves_original_before_current_workspace_or_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, cursor = adapter(monkeypatch)
    cursor.rows.return_value = [row()]
    result = metadata.reserve(
        INTENT,
        asset_id=RESERVED.asset_id,
        object_key=RESERVED.object_key,
        inspection=INSPECTION,
        policy=POLICY,
    )
    assert result.asset == READY and result.outcome == "replayed"
    cursor.workspace.assert_not_called()
    changed = replace(INTENT, bytes=5)
    assert (
        metadata.reserve(
            changed,
            asset_id=RESERVED.asset_id,
            object_key=RESERVED.object_key,
            inspection=INSPECTION,
            policy=POLICY,
        ).outcome
        == "conflict"
    )


def test_fresh_reservation_requires_workspace_and_keeps_rejected_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, cursor = adapter(monkeypatch)
    cursor.workspace.return_value = False
    with pytest.raises(AssetError, match="workspace_unavailable"):
        metadata.reserve(
            INTENT,
            asset_id=RESERVED.asset_id,
            object_key=RESERVED.object_key,
            inspection=INSPECTION,
            policy=POLICY,
        )
    cursor.event.assert_not_called()
    cursor.workspace.return_value = True
    result = metadata.reserve(
        INTENT,
        asset_id=RESERVED.asset_id,
        object_key=RESERVED.object_key,
        inspection=INSPECTION,
        policy=POLICY,
    )
    assert result.asset is not None and result.asset.phase == "reserved"
    assert result.outcome == "created"


def test_publication_cannot_rebind_another_version(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata, cursor = adapter(monkeypatch)
    cursor.proof.return_value = replace(PROOF, version_id="other-version")
    result = metadata.publish(PROOF)
    assert result.outcome == "conflict"
    cursor.save.assert_not_called()
    cursor.proof.return_value = None
    published = metadata.publish(PROOF)
    assert published == UploadResult("created", READY)
    cursor.save.assert_called_once()


def test_second_writer_is_never_authorized_from_an_in_progress_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, cursor = adapter(monkeypatch)
    assert metadata.begin_write(WRITING.asset_id, "writer-other").outcome == "in_progress"
    cursor.save.assert_not_called()
    cursor.asset.return_value = RESERVED
    assert metadata.begin_write(RESERVED.asset_id, "writer-test").asset == WRITING
    cursor.workspace.return_value = False
    with pytest.raises(AssetError, match="workspace_unavailable"):
        metadata.begin_write(RESERVED.asset_id, "writer-other")


@pytest.mark.parametrize("method", ["reserve", "begin", "publish"])
def test_unknown_commit_is_returned_without_repeating_transaction(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    metadata = PostgresAssetMetadata(cast(SpeechPool, Mock()))
    attempts = 0

    @contextmanager
    def uncertain(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        Mock(side_effect=AssetError("asset_metadata_unknown"))()
        yield

    monkeypatch.setattr(metadata, "_transaction", uncertain)
    if method == "reserve":
        result = metadata.reserve(
            INTENT,
            asset_id=RESERVED.asset_id,
            object_key=RESERVED.object_key,
            inspection=INSPECTION,
            policy=POLICY,
        )
    elif method == "begin":
        result = metadata.begin_write(RESERVED.asset_id, "writer-test")
    else:
        result = metadata.publish(PROOF)
    assert result.outcome == "unknown" and attempts == 1


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("private diagnostic"),
        psycopg.InterfaceError("private diagnostic"),
        psycopg.errors.InsufficientPrivilege("private diagnostic"),
    ],
)
def test_driver_failures_expose_only_safe_error_and_do_not_retry(error: Exception) -> None:
    pool = Mock()
    pool.connection.side_effect = error
    metadata = PostgresAssetMetadata(cast(SpeechPool, pool))
    with pytest.raises(AssetError) as caught:
        metadata.get("asset-test")
    assert "private" not in str(caught.value)
    assert pool.connection.call_count == 1


def test_read_clock_and_scans_are_bounded_and_unknown_identity_is_not_mutated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, cursor = adapter(monkeypatch)
    assert metadata.get("asset-test") == (WRITING, 3)
    assert metadata.object("asset-test") is None
    assert metadata.pending(10) == metadata.expired(10) == []
    for limit in (0, 101):
        with pytest.raises(ValueError, match="bounded"):
            metadata.pending(limit)
    cursor.asset.return_value = None
    with pytest.raises(AssetError, match="asset_not_found"):
        metadata.begin_write("asset-unknown", "writer-test")
    cursor.save.assert_not_called()
    assert not metadata.complete_deletion("ledger-unknown")
    with pytest.raises(ValueError, match="bounded"):
        PostgresAssetMetadata(cast(SpeechPool, Mock()), timeout_seconds=0)


def test_transaction_budget_uses_remaining_time_and_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = MagicMock()
    connection = pool.connection.return_value.__enter__.return_value
    raw = connection.cursor.return_value.__enter__.return_value
    raw.description = None
    metadata = PostgresAssetMetadata(cast(SpeechPool, pool))
    with metadata._transaction(readonly=True) as cursor:
        assert cursor.rows("SELECT 1") == []
    connection.execute.assert_called_once_with("SET TRANSACTION READ ONLY")
    connection.cursor.return_value.__exit__.assert_called_once()
    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.assets_postgres.time.monotonic",
        Mock(side_effect=[0, 1, 4]),
    )
    with (
        pytest.raises(AssetError, match="asset_metadata_deadline"),
        metadata._transaction() as cursor,
    ):
        cursor.rows("SELECT 1")


def test_poll_hint_advances_selected_originals_without_claiming_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, cursor = adapter(monkeypatch)
    cursor.rows.return_value = [{"asset_id": "asset-test"}]
    assert metadata.pending(1) == metadata.expired(1) == ["asset-test"]
    cursor.save.assert_not_called()
    cursor.event.assert_not_called()
