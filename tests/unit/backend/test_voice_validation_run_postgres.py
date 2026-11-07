"""Validation fact reads must restate only frozen draft/generation rows."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from speechnexus_backend.speech.domain.job_dispatch import DispatchJob
from speechnexus_backend.speech.infrastructure.voice_validation_run_postgres import (
    PostgresValidationRunFacts,
)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = iter(rows)
        self.statements: list[str] = []

    def execute(self, sql: str, parameters: tuple[Any, ...]) -> _Cursor:
        self.statements.append(sql)
        return self

    def fetchone(self) -> dict[str, Any]:
        return next(self._rows)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.transaction: list[str] = []

    def execute(self, sql: str) -> None:
        self.transaction.append(sql)

    @contextmanager
    def cursor(self, *, row_factory: object) -> Iterator[_Cursor]:
        assert row_factory is not None
        yield self._cursor

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[_Connection]:
        assert timeout > 0
        yield self._connection


def _job() -> DispatchJob:
    return DispatchJob(
        job_id="job-validation",
        workspace_id="workspace-default",
        kind="voice_validation",
        state="running",
        status_version=1,
        created_at_us=1,
        request_digest="sha256:" + "a" * 64,
        request_document="{}",
    )


def test_read_restates_the_intent_revision_and_ready_asset() -> None:
    rows: list[dict[str, Any]] = [
        {
            "request_document": json.dumps(
                {
                    "kind": "voice_validation",
                    "input": {
                        "voice_draft_id": "draft-1",
                        "draft_revision": "1",
                        "validation_set_revision": "1",
                    },
                }
            )
        },
        {
            "kind": "clone",
            "input_json": json.dumps(
                {"reference_audio_asset_id": "asset-1", "reference_text": "Reference text."}
            ),
            "binding_json": json.dumps({"id": "binding-1", "revision": "1"}),
            "workspace_id": "workspace-default",
            "generation_request": json.dumps({"input": {"preview_text": "Generation preview."}}),
        },
        {
            "asset_state": "ready",
            "purpose": "voice_reference",
            "sha256": "sha256:" + "b" * 64,
            "bytes": 100,
            "media_type": "audio/wav",
            "sample_rate_hz": 24_000,
            "channels": 1,
        },
    ]
    cursor = _Cursor(rows)
    connection = _Connection(cursor)
    facts = PostgresValidationRunFacts(cast(Any, _Pool(connection))).read(_job())
    assert connection.transaction == ["SET TRANSACTION READ ONLY"]
    assert facts.draft_id == "draft-1"
    assert facts.draft_revision == "1"
    assert facts.validation_set_revision == "1"
    assert facts.binding_id == "binding-1"
    assert facts.binding_revision == "1"
    assert facts.generation_preview_text == "Generation preview."
    assert facts.reference_text == "Reference text."
    assert facts.material.asset_id == "asset-1"
    assert len(cursor.statements) == 3
