"""Bounded retention decisions: expire elapsed rows, remove only their content."""

from __future__ import annotations

import json
import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from threading import Event

import pytest

from speechnexus_backend.artifact_retention_worker import run_retention
from speechnexus_backend.speech.application.artifact_retention import (
    ArtifactRetention,
    RetentionOutcome,
)
from speechnexus_backend.speech.application.artifacts import ArtifactService, ContentReader
from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord

NOW = 1_700_000_000_000_000
RETENTION_US = 30 * 24 * 60 * 60 * 1_000_000
ELAPSED_ID = "artifact-" + "1" * 48
RETAINED_ID = "artifact-" + "2" * 48


def record(artifact_id: str, *, expires_at_us: int | None, status: str = "ready") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        workspace_id="workspace-default",
        kind="audio",
        status=status,  # type: ignore[arg-type]
        media_type="audio/wav",
        bytes=32,
        sha256="sha256:" + "a" * 64,
        receipt_id="receipt-" + artifact_id,
        source_operation_id="operation-" + artifact_id,
        created_at_us=NOW - RETENTION_US,
        expires_at_us=expires_at_us,
        object_key="private/artifacts/" + artifact_id,
        storage_version_id="version-" + artifact_id,
    )


@dataclass
class Rows:
    """In-memory rows that apply the same retention predicate as the SQL."""

    rows: dict[str, ArtifactRecord] = field(default_factory=dict)
    now_us: int = NOW
    transitions: list[tuple[str, str]] = field(default_factory=list)
    # Simulates a widened SQL predicate: selection ignores the deadline. The
    # application re-check must then refuse on its own.
    widen: bool = False

    def expired(self, limit: int) -> tuple[int, tuple[ArtifactRecord, ...]]:
        selected = tuple(
            row
            for row in self.rows.values()
            if row.status == "ready"
            and row.expires_at_us is not None
            and row.expires_at_us <= self.now_us
        )[:limit]
        return self.now_us, selected

    def mark_expired(self, artifact_id: str) -> ArtifactRecord | None:
        row = self.rows.get(artifact_id)
        if (
            row is None
            or row.status != "ready"
            or row.expires_at_us is None
            or row.expires_at_us > self.now_us
        ):
            return None
        self.transitions.append((artifact_id, "expired"))
        self.rows[artifact_id] = replace(row, status="expired")
        return self.rows[artifact_id]

    def pending_removal(self, limit: int) -> tuple[int, tuple[ArtifactRecord, ...]]:
        selected = tuple(
            row
            for row in self.rows.values()
            if row.status == "expired"
            and row.expires_at_us is not None
            and (self.widen or row.expires_at_us <= self.now_us)
        )[:limit]
        return self.now_us, selected

    def mark_deleted(self, artifact_id: str) -> ArtifactRecord | None:
        row = self.rows.get(artifact_id)
        if row is None or row.status != "expired":
            return None
        if row.expires_at_us is None or row.expires_at_us > self.now_us:
            return None
        self.transitions.append((artifact_id, "deleted"))
        self.rows[artifact_id] = replace(row, status="deleted", deleted_at_us=self.now_us)
        return self.rows[artifact_id]

    def current(self, artifact_id: str) -> ArtifactRecord | None:
        return self.rows.get(artifact_id)


@dataclass
class Content:
    removed: list[str] = field(default_factory=list)
    unconfirmed: bool = False

    def delete_version(self, artifact: ArtifactRecord) -> None:
        if self.unconfirmed:
            raise ArtifactError("artifact_cleanup_unconfirmed")
        self.removed.append(artifact.artifact_id)


def retention(rows: Rows, content: Content, *, batch_size: int = 10) -> ArtifactRetention:
    return ArtifactRetention(rows, content, batch_size=batch_size)


def test_elapsed_row_is_expired_then_removed_and_retained_row_is_untouched() -> None:
    """Both directions in one pass: elapsed content goes, in-window content stays."""
    rows = Rows(
        rows={
            ELAPSED_ID: record(ELAPSED_ID, expires_at_us=NOW - 1),
            RETAINED_ID: record(RETAINED_ID, expires_at_us=NOW + 1),
        }
    )
    content = Content()

    outcomes = retention(rows, content).sweep()

    assert outcomes == (
        RetentionOutcome(ELAPSED_ID, "expired", NOW - 1, NOW),
        RetentionOutcome(ELAPSED_ID, "deleted", NOW - 1, NOW, "", NOW),
    )
    assert content.removed == [ELAPSED_ID]
    assert rows.transitions == [(ELAPSED_ID, "expired"), (ELAPSED_ID, "deleted")]
    assert rows.rows[RETAINED_ID].status == "ready"
    assert rows.rows[RETAINED_ID].expires_at_us == NOW + 1
    assert rows.rows[ELAPSED_ID].status == "deleted"
    # The deadline that justified the decision stays recorded.
    assert rows.rows[ELAPSED_ID].expires_at_us == NOW - 1


class RacedRows(Rows):
    """Rows where another sweep commits the same transition first."""

    def mark_deleted(self, artifact_id: str) -> ArtifactRecord | None:
        row = self.rows[artifact_id]
        self.rows[artifact_id] = replace(row, status="deleted", deleted_at_us=self.now_us)
        return None


class VanishedRows(Rows):
    """Rows where the selected row disappears before the transition."""

    def mark_deleted(self, artifact_id: str) -> ArtifactRecord | None:
        self.rows.pop(artifact_id, None)
        return None


def test_a_refused_transition_is_explained_by_the_durable_row() -> None:
    """The sweep must not report "content already removed" for a refusal.

    A concurrent pass and a re-checked deadline are different facts. Each answer
    is derived from the row as it now stands, and only a durable removal record is
    reported as a completed removal.
    """
    inside = replace(record(ELAPSED_ID, expires_at_us=NOW + 1), status="expired")
    outcomes = retention(Rows(rows={ELAPSED_ID: inside}, widen=True), Content()).sweep()
    assert outcomes == (
        RetentionOutcome(ELAPSED_ID, "blocked", NOW + 1, NOW, "retention_not_elapsed"),
    )

    elapsed = replace(record(RETAINED_ID, expires_at_us=NOW - 1), status="expired")
    raced = retention(RacedRows(rows={RETAINED_ID: elapsed}), Content()).sweep()
    assert raced == (
        RetentionOutcome(RETAINED_ID, "deleted", NOW - 1, NOW, "recorded_by_another_pass", NOW),
    )


def test_a_row_that_vanished_between_passes_is_not_reported_as_removed() -> None:
    elapsed = replace(record(ELAPSED_ID, expires_at_us=NOW - 1), status="expired")
    outcomes = retention(VanishedRows(rows={ELAPSED_ID: elapsed}), Content()).sweep()
    assert outcomes == (RetentionOutcome(ELAPSED_ID, "blocked", NOW - 1, NOW, "row_missing"),)


def test_never_expiring_row_is_never_selected() -> None:
    rows = Rows(rows={"artifact-" + "3" * 48: record("artifact-" + "3" * 48, expires_at_us=None)})
    content = Content()

    assert retention(rows, content).sweep() == ()
    assert content.removed == [] and rows.transitions == []


def test_unconfirmed_content_removal_keeps_the_durable_expired_state() -> None:
    rows = Rows(rows={ELAPSED_ID: record(ELAPSED_ID, expires_at_us=NOW - 1)})
    content = Content(unconfirmed=True)

    outcomes = retention(rows, content).sweep()

    assert outcomes == (
        RetentionOutcome(ELAPSED_ID, "expired", NOW - 1, NOW),
        RetentionOutcome(ELAPSED_ID, "blocked", NOW - 1, NOW, "content_remove_unconfirmed"),
    )
    assert rows.rows[ELAPSED_ID].status == "expired"
    assert rows.transitions == [(ELAPSED_ID, "expired")]

    # The next pass retries the same row and completes the cleanup.
    content.unconfirmed = False
    second = retention(rows, content).sweep()
    assert second == (RetentionOutcome(ELAPSED_ID, "deleted", NOW - 1, NOW, "", NOW),)
    assert rows.rows[ELAPSED_ID].status == "deleted"


def test_a_row_marked_expired_inside_its_window_is_not_deleted() -> None:
    """A widened selection cannot delete content whose retention has not elapsed."""
    inside = replace(record(ELAPSED_ID, expires_at_us=NOW + 1), status="expired")
    rows = Rows(rows={ELAPSED_ID: inside}, widen=True)
    content = Content()

    outcomes = retention(rows, content).sweep()

    assert outcomes == (
        RetentionOutcome(ELAPSED_ID, "blocked", NOW + 1, NOW, "retention_not_elapsed"),
    )
    assert content.removed == []
    assert rows.rows[ELAPSED_ID].status == "expired"


def test_batch_size_is_the_only_bound_and_must_be_explicit() -> None:
    rows, content = Rows(), Content()
    for value in (0, -1, 101, True, 1.5):
        with pytest.raises(ValueError, match="must be bounded"):
            ArtifactRetention(rows, content, batch_size=value)  # type: ignore[arg-type]


class ServiceMetadata:
    def __init__(self, rows: Rows, now_us: int) -> None:
        self._rows, self._now_us = rows, now_us

    def get(self, artifact_id: str, workspace_id: str) -> tuple[ArtifactRecord | None, int]:
        row = self._rows.rows.get(artifact_id)
        if row is None or row.workspace_id != workspace_id:
            return None, self._now_us
        return row, self._now_us


class ServiceObjects:
    def __init__(self, content: Content) -> None:
        self._content = content

    def open(
        self, artifact: ArtifactRecord, offset: int, length: int
    ) -> AbstractContextManager[ContentReader]:
        # Reaching here would mean content was served for a row that is not
        # servable.
        raise AssertionError("content must not be opened for an unserveable row")


def test_read_path_and_sweep_agree_on_the_same_rows() -> None:
    """The public answer changes exactly when the sweep commits its decision."""
    rows = Rows(
        rows={
            ELAPSED_ID: record(ELAPSED_ID, expires_at_us=NOW - 1),
            RETAINED_ID: record(RETAINED_ID, expires_at_us=NOW + 1),
        }
    )
    content = Content()
    service = ArtifactService(
        ServiceMetadata(rows, NOW), ServiceObjects(content), "workspace-default"
    )

    with pytest.raises(ArtifactError) as elapsed:
        service.download(ELAPSED_ID)
    assert elapsed.value.code == "artifact_expired"
    # Before the sweep the elapsed row is still `ready`, so its state is readable.
    assert service.metadata(ELAPSED_ID).status == "expired"

    retention(rows, content).sweep()

    with pytest.raises(ArtifactError) as removed:
        service.download(ELAPSED_ID)
    assert removed.value.code == "artifact_deleted"
    assert service.metadata(RETAINED_ID).status == "ready"


def test_one_pass_logs_each_decision_and_a_summary(caplog: pytest.LogCaptureFixture) -> None:
    rows = Rows(
        rows={
            ELAPSED_ID: record(ELAPSED_ID, expires_at_us=NOW - 1),
            RETAINED_ID: record(RETAINED_ID, expires_at_us=NOW + 1),
        }
    )
    content = Content()
    caplog.set_level(logging.INFO)

    # The default sink writes one reviewable JSON line per decision plus a summary.
    run_retention(retention(rows, content), Event(), interval_seconds=1, once=True)

    lines = [json.loads(item.message) for item in caplog.records if item.message.startswith("{")]
    assert lines == [
        {
            "artifact_id": ELAPSED_ID,
            "decision": "expired",
            "event": "artifact_retention",
            "expires_at_us": NOW - 1,
            "observed_at_us": NOW,
            "reason": "",
        },
        {
            "artifact_id": ELAPSED_ID,
            "decision": "deleted",
            "event": "artifact_retention",
            "expires_at_us": NOW - 1,
            "observed_at_us": NOW,
            "reason": "",
            "deleted_at_us": NOW,
        },
        {
            "event": "artifact_retention_pass",
            "batch_size": 10,
            "decisions": 2,
            "blocked": 0,
            "deleted": 1,
            "expired": 1,
        },
    ]
    assert content.removed == [ELAPSED_ID]
