"""Fixed text exports through actual isolated PG roles and public synthetic import."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from tests.integration.test_workbench_listings_postgres import delete
from tests.integration.test_workbench_transcript_import_postgres import (
    PublicService,
    create_parent,
    import_one,
)
from tests.integration.test_workbench_transcript_import_postgres import public_http as public_http
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application import transcript_exports as module
from speechnexus_backend.workbench.application.transcript_exports import TranscriptTextExports
from speechnexus_backend.workbench.application.versions import (
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.deletion import DeletionTarget
from speechnexus_backend.workbench.domain.versions import TranscriptRevision
from speechnexus_backend.workbench.infrastructure.public_transcript_source import (
    PublicTranscriptSource,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("json_source", [False, True])
async def test_public_import_and_corrected_versions_export_exact_bytes_without_rereading_source(
    store: Store, public_http: tuple[PublicService, PublicTranscriptSource], json_source: bool
) -> None:
    state, source = public_http
    text = "\ufeff合成原文\r\n😀e\u0301\n"
    if json_source:
        import json

        state.media_type = "application/json"
        state.content = json.dumps({"text": text}, ensure_ascii=False).encode()
    else:
        state.content = text.encode()
    app = WorkbenchVersions(store.uow, source_reader=source)
    parent = await create_parent(store)
    original = await import_one(app, parent.id)
    await app.revise_transcript(transcript_id=original.id, expected_revision=1, text="校对\r\n")
    exports = TranscriptTextExports(app)
    sealed = await exports.export_revision(original.id, 1)
    corrected = await exports.export_revision(original.id, 2)
    assert sealed.content == text.encode() and sealed.status == "sealed"
    assert corrected.content == "校对\r\n".encode() and corrected.status == "draft"
    assert (
        corrected.source_revision == 1 and corrected.source == sealed.source == original.head.source
    )
    await app.revise_transcript(transcript_id=original.id, expected_revision=2, text="")
    assert (await exports.export_revision(original.id, 3)).content == b""
    assert await exports.export_revision(original.id, 1) == sealed
    assert await exports.export_revision(original.id, 2) == corrected
    assert state.reads == 2
    with pytest.raises(ResourceNotFoundError):
        await exports.export_revision(original.id, 99)


@pytest.mark.parametrize("kind", ["project", "transcript"])
async def test_new_exports_are_denied_after_delete_acceptance_before_body_cleanup(
    store: Store, kind: str
) -> None:
    parent = await create_parent(store)
    original = await store.app.create_manual_transcript(
        project_id=parent.id, display_name="合成", text="合成待删除正文"
    )
    exports = TranscriptTextExports(store.app)
    accepted = await exports.export_revision(original.id, 1)
    identity = parent.id if kind == "project" else original.id
    record = await delete(store, parent, DeletionTarget("workbench", kind, identity, 1))  # type: ignore[arg-type]
    assert record.result is None
    with pytest.raises(ResourceNotFoundError):
        await exports.export_revision(original.id, 1)
    # Already-returned bytes cannot be revoked; no durable export was published.
    assert accepted.content == "合成待删除正文".encode()


async def test_utf8_encoding_and_digest_hold_no_runtime_pg_connection(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = await create_parent(store)
    original = await store.app.create_manual_transcript(
        project_id=parent.id, display_name="合成", text="合成事务外编码"
    )
    phases: list[str] = []

    def check(phase: str) -> None:
        with psycopg.connect(store.owner_dsn, autocommit=True) as owner:
            row = owner.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND usename = %s",
                (store.role,),
            ).fetchone()
        assert row == (0,)
        phases.append(phase)

    class CheckedText(str):
        def encode(self, *args: Any, **kwargs: Any) -> bytes:
            check("encoding")
            return super().encode(*args, **kwargs)

    class CheckedReader:
        async def get_revision(self, transcript_id: str, revision: int) -> TranscriptRevision:
            saved = await store.app.get_revision(transcript_id, revision)
            return replace(saved, text=CheckedText(saved.text))

    def checked_digest(content: bytes) -> Any:
        check("digest")
        return hashlib.sha256(content)

    monkeypatch.setattr(module, "sha256", checked_digest)
    assert (await TranscriptTextExports(CheckedReader()).export_revision(original.id, 1)).content
    assert phases == ["encoding", "digest"]
