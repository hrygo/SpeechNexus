"""Execution-side guards that the schema cannot reach by itself.

The composite foreign key on ``speech_content_deletions`` already forbids a pin
that does not exist, so these two checks are exercised where they can actually
fire: a fake cursor and a fake transaction. Both refuse to confirm a content
deletion against an object version the accepted plan never pinned.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.assets import VerifiedObject
from speechnexus_backend.speech.domain.deletion import DeletionLedgerUnavailableError
from speechnexus_backend.speech.infrastructure.assets_postgres import complete_content_deletion
from speechnexus_backend.speech.infrastructure.deletion_executions_postgres import (
    _LEDGER_ROW_COLUMNS,
    _DeletionExecution,
    _LedgerRow,
)
from tests.unit.test_assets import READY

NOW = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)


def _ledger_row(**overrides: Any) -> _LedgerRow:
    columns = [column.strip() for column in _LEDGER_ROW_COLUMNS.split(",")]
    row: dict[str, Any] = dict.fromkeys(columns)
    row.update(
        id="dl-resume-1",
        workspace_id="workspace-default",
        plan_id="dp-resume-1",
        plan_revision="1",
        plan_digest="sha256:" + "a" * 64,
        target_owner="speech",
        target_kind="asset",
        target_id="asset-resume",
        target_revision="1",
        idempotency_key_digest="b" * 64,
        intent_digest="c" * 64,
        deleted=[["speech", "asset", "asset-resume", "1"]],
        retained=[],
        status="queued",
        cleanup_state="pending",
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        content_ledger_id="ledger-resume",
    )
    row.update(overrides)
    return _LedgerRow(row)


class _Transaction:
    """Only the reads ``_resume`` performs, answered in order."""

    def __init__(
        self, reply: list[list[dict[str, Any]]], *, claim: dict[str, Any] | None = None
    ) -> None:
        self._reply = list(reply)
        self._claim = claim

    def rows(self, statement: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        return self._reply.pop(0)

    def claim_purge(self, ledger_id: str) -> dict[str, Any] | None:
        return self._claim

    def now(self) -> datetime:
        return NOW


def _object(version: str) -> dict[str, Any]:
    return {
        "asset_id": "asset-resume",
        "object_key": "private/assets/asset-resume/" + "0" * 32,
        "storage_version_id": version,
        "bytes": 16,
        "sha256": "sha256:" + "d" * 64,
        "writer_id": "writer-1",
    }


def test_resume_accepts_the_pinned_object_version() -> None:
    row = _ledger_row().row
    transaction = _Transaction(
        [[{"deletion_state": "queued", "storage_version_id": "v-1"}], [_object("v-1")]],
        claim=row,
    )
    accepted = _DeletionExecution(cast(Any, transaction), "workspace-default")._resume(
        _ledger_row()
    )
    assert accepted.record.status == "queued"
    assert accepted.pending_object is not None
    assert accepted.pending_object.version_id == "v-1"


def test_resume_does_not_purge_when_another_request_holds_the_claim() -> None:
    """Losing the claim means the caller waits, not that it purges again."""
    transaction = _Transaction(
        [[{"deletion_state": "queued", "storage_version_id": "v-1"}], [_object("v-1")]],
        claim=None,
    )
    accepted = _DeletionExecution(cast(Any, transaction), "workspace-default")._resume(
        _ledger_row()
    )
    assert accepted.record.status == "queued"
    assert accepted.pending_object is None


def test_resume_refuses_a_live_version_that_was_never_pinned() -> None:
    transaction = _Transaction(
        [[{"deletion_state": "queued", "storage_version_id": "v-1"}], [_object("v-2")]]
    )
    with pytest.raises(DeletionLedgerUnavailableError):
        _DeletionExecution(cast(Any, transaction), "workspace-default")._resume(_ledger_row())


class _AssetCursor:
    def __init__(self, *, state: str, pin: str | None, version: str | None) -> None:
        self._state, self._pin, self._version = state, pin, version
        self.updated = False
        self.saved: list[tuple[str, str]] = []

    def rows(self, statement: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        if "FROM speech_content_deletions" in statement:
            return [{"deletion_state": self._state, "storage_version_id": self._pin}]
        if "FROM speech_asset_references" in statement:
            return []
        if statement.startswith("UPDATE speech_content_deletions"):
            self.updated = True
            return []
        raise AssertionError(statement)

    def proof(self, asset_id: str) -> VerifiedObject | None:
        if self._version is None:
            return None
        return VerifiedObject(
            asset_id,
            "private/assets/asset-resume/" + "0" * 32,
            self._version,
            16,
            "sha256:" + "d" * 64,
            "writer-1",
        )

    def clock(self) -> int:
        return 1_760_000_000_000_000

    def save(self, asset: Any, kind: str, now: int) -> None:
        self.saved.append((kind, asset.status))


def test_content_completion_requires_the_pinned_version() -> None:
    expired = replace(READY, status="expired", phase="settled")
    matching = _AssetCursor(state="queued", pin="v-1", version="v-1")
    assert complete_content_deletion(cast(Any, matching), expired, "ledger-resume") is True
    assert matching.updated is True

    mismatched = _AssetCursor(state="queued", pin="v-1", version="v-2")
    assert complete_content_deletion(cast(Any, mismatched), expired, "ledger-resume") is False
    assert mismatched.updated is False
    assert mismatched.saved == []

    missing = _AssetCursor(state="queued", pin="v-1", version=None)
    assert complete_content_deletion(cast(Any, missing), expired, "ledger-resume") is False
