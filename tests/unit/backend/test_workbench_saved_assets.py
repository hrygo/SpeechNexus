"""Generated saved-asset HTTP boundaries with no database, object store or network."""

import io
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.saved_assets import (
    ObjectZoneUnavailableError,
    WorkbenchSavedAssets,
)
from speechnexus_backend.workbench.application.speech_sources import (
    ArtifactCopy,
    SpeechSourceError,
)
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.saved_assets import (
    SavedAsset,
    SavedAssetCopy,
    project_object_prefix,
    saved_object_key,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError
from speechnexus_backend.workbench.interfaces.http_saved_assets import create_saved_asset_router

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PROJECT = "project-demo"
ASSET = "asset-synthetic-one"
ARTIFACT = "artifact-synthetic-one"
RECEIPT = "receipt-synthetic-one"
DIGEST = "sha256:" + "b" * 64
CONTENT = b"synthetic audio bytes"
PATH = f"/v1/workbench/projects/{PROJECT}/saved-assets"
ASSET_PATH = f"/v1/workbench/saved-assets/{ASSET}"
KEY = "saved-asset-key-0001"


def copy(**changes: Any) -> SavedAssetCopy:
    values: dict[str, Any] = {
        "source_artifact_id": ARTIFACT,
        "source_receipt_id": RECEIPT,
        "media_type": "audio/wav",
        "bytes": len(CONTENT),
        "sha256": DIGEST,
        "display_name": "合成第一章",
    }
    values.update(changes)
    return SavedAssetCopy(**values)


def asset(**changes: Any) -> SavedAsset:
    values: dict[str, Any] = {
        "id": ASSET,
        "project_id": PROJECT,
        "object_key": saved_object_key(PROJECT, DIGEST),
        "copy": copy(),
        "created_at": NOW,
    }
    values.update(changes)
    return SavedAsset(**values)


class Store:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.saved: SavedAsset | None = None
        self.replay_asset: SavedAsset | None = None
        self.replay_failure: BaseException | None = None
        self.visibility_failure: BaseException | None = None
        self.visibilities: list[dict[str, Any]] = []
        self.commit_failure: BaseException | None = None
        self.replays: list[dict[str, Any]] = []
        self.commits: list[dict[str, Any]] = []

    async def get(self, **kw: Any) -> SavedAsset:
        if self.saved is None or kw["asset_id"] != self.saved.id:
            raise ResourceNotFoundError
        return self.saved

    async def check_project(self, **kw: Any) -> None:
        self.visibilities.append(kw)
        if self.visibility_failure is not None:
            raise self.visibility_failure

    async def replay(self, **kw: Any) -> SavedAsset | None:
        self.replays.append(kw)
        if self.replay_failure is not None:
            raise self.replay_failure
        return self.replay_asset

    async def commit(self, **kw: Any) -> SavedAsset:
        self.events.append("commit")
        self.commits.append(kw)
        if self.commit_failure is not None:
            raise self.commit_failure
        return SavedAsset(
            id=kw["asset_id"],
            project_id=kw["project_id"],
            object_key=kw["object_key"],
            copy=kw["copy"],
            created_at=NOW,
        )


class Source:
    def __init__(self) -> None:
        self.failure: BaseException | None = None
        self.bodies: list[io.BytesIO] = []

    async def copy(self, **kw: Any) -> ArtifactCopy:
        if self.failure is not None:
            raise self.failure
        body = io.BytesIO(CONTENT)
        self.bodies.append(body)
        return ArtifactCopy(
            artifact_id=str(kw["artifact_id"]),
            receipt_id=str(kw["receipt_id"]),
            media_type="audio/wav",
            bytes=len(CONTENT),
            sha256=DIGEST,
            body=body,
        )


class Zone:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.failure: BaseException | None = None
        self.puts: list[tuple[str, bytes, int]] = []

    async def put(self, *, key: str, body: Any, length: int) -> None:
        self.events.append("put")
        if self.failure is not None:
            raise self.failure
        self.puts.append((key, body.read(), length))

    async def erase_prefix(self, *, prefix: str) -> int:
        return 0


class Harness:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.store = Store(self.events)
        self.source = Source()
        self.zone = Zone(self.events)

    @property
    def service(self) -> WorkbenchSavedAssets:
        return WorkbenchSavedAssets(self.store, self.source, self.zone)


def app(harness: Harness | None) -> FastAPI:
    service = None if harness is None else harness.service
    value = create_app(
        None,
        saved_assets=service,
        default_workspace_id="workspace-demo",
        deployment_id="synthetic",
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.02,
    )
    if harness is None:
        value.include_router(
            create_saved_asset_router(
                None,
                default_workspace_id="workspace-demo",
                max_json_bytes=4096,
                body_timeout_seconds=0.02,
            )
        )
    return value


async def call(
    harness: Harness | None,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app(harness))
    async with httpx.AsyncClient(transport=transport, base_url="http://workbench.test") as client:
        return (
            await client.post(path, json=body, headers=headers)
            if body is not None
            else await client.get(path, headers=headers)
        )


def body(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"source_artifact_id": ARTIFACT, "source_receipt_id": RECEIPT}
    values.update(changes)
    return values


async def test_save_writes_the_object_before_publishing_the_row_and_closes_the_body() -> None:
    harness = Harness()
    response = await call(
        harness, PATH, body=body(display_name="合成第一章"), headers={"Idempotency-Key": KEY}
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["object"] == "nexus.saved_asset"
    assert payload["project_id"] == PROJECT
    assert payload["sha256"] == DIGEST
    assert payload["bytes"] == len(CONTENT)
    assert payload["source_receipt_id"] == RECEIPT
    assert payload["created_at"].startswith("2026-10-24")
    assert "object_key" not in payload and "staging" not in payload
    assert harness.events == ["put", "commit"]
    assert harness.zone.puts == [(saved_object_key(PROJECT, DIGEST), CONTENT, len(CONTENT))]
    assert harness.store.commits[0]["key"] == KEY
    assert harness.store.commits[0]["intent_digest"] is not None
    assert harness.source.bodies[0].closed


async def test_save_replays_without_downloading_or_writing_again() -> None:
    harness = Harness()
    harness.store.replay_asset = asset()
    response = await call(harness, PATH, body=body(), headers={"Idempotency-Key": KEY})

    assert response.status_code == 201
    assert response.json()["id"] == ASSET
    assert harness.store.replays[0]["key"] == KEY
    assert harness.source.bodies == [] and harness.zone.puts == []
    assert harness.events == []


async def test_save_without_a_key_never_consults_the_receipt() -> None:
    harness = Harness()
    response = await call(harness, PATH, body=body())

    assert response.status_code == 201
    assert harness.store.replays == []
    assert harness.store.commits[0]["key"] is None
    assert harness.store.commits[0]["intent_digest"] is None


async def test_replayed_key_with_another_intent_is_reported_as_a_conflict() -> None:
    harness = Harness()
    harness.store.replay_failure = IdempotencyConflictError()
    response = await call(harness, PATH, body=body(), headers={"Idempotency-Key": KEY})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "payload",
    [
        {"source_artifact_id": "../private", "source_receipt_id": RECEIPT},
        {"source_artifact_id": ARTIFACT, "source_receipt_id": ""},
        {"source_artifact_id": ARTIFACT},
        {"source_artifact_id": ARTIFACT, "source_receipt_id": RECEIPT, "private_path": "/etc"},
        {"source_artifact_id": ARTIFACT, "source_receipt_id": RECEIPT, "display_name": ""},
    ],
)
async def test_malformed_save_requests_are_rejected_before_any_io(payload: dict[str, Any]) -> None:
    harness = Harness()
    response = await call(harness, PATH, body=payload)

    assert response.status_code == 400
    assert harness.source.bodies == [] and harness.zone.puts == [] and harness.events == []


async def test_invalid_request_key_is_rejected_without_io() -> None:
    # A key shorter than the frozen minimum never reaches the application boundary.
    harness = Harness()
    response = await call(harness, PATH, body=body(), headers={"Idempotency-Key": "short"})

    assert response.status_code == 400
    assert harness.source.bodies == [] and harness.events == []

    with pytest.raises(InvalidVersionChangeError):
        await harness.service.save(
            project_id=PROJECT,
            workspace_id="workspace-demo",
            source_artifact_id=ARTIFACT,
            source_receipt_id=RECEIPT,
            key="short",
        )
    assert harness.source.bodies == [] and harness.events == []


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("source_expired", 410),
        ("source_invalid", 422),
        ("source_unsupported", 422),
        ("source_too_large", 413),
        ("source_unavailable", 503),
    ],
)
async def test_source_failures_keep_their_stable_status_and_write_nothing(
    code: str, status: int
) -> None:
    harness = Harness()
    harness.source.failure = SpeechSourceError(code)  # type: ignore[arg-type]
    response = await call(harness, PATH, body=body())

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert harness.zone.puts == [] and harness.events == []


@pytest.mark.parametrize(
    ("object_failure", "commit_failure", "status"),
    [
        (ObjectZoneUnavailableError(), None, 503),
        (None, ResourceNotFoundError(), 404),
        (None, IdempotencyConflictError(), 409),
    ],
)
async def test_failed_publication_closes_the_copied_body(
    object_failure: BaseException | None, commit_failure: BaseException | None, status: int
) -> None:
    harness = Harness()
    harness.zone.failure = object_failure
    harness.store.commit_failure = commit_failure
    response = await call(harness, PATH, body=body(), headers={"Idempotency-Key": KEY})

    assert response.status_code == status
    assert harness.source.bodies[0].closed


async def test_get_returns_the_committed_asset_then_reports_absence() -> None:
    harness = Harness()
    harness.store.saved = asset()
    found = await call(harness, ASSET_PATH)
    assert found.status_code == 200
    assert found.json()["id"] == ASSET

    missing = await call(Harness(), ASSET_PATH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "resource_not_found"


async def test_invalid_asset_path_is_rejected_without_touching_the_store() -> None:
    harness = Harness()
    response = await call(harness, "/v1/workbench/saved-assets/UPPERCASE-ID")

    assert response.status_code == 400
    # No layer may have been reached: a dispatch would have recorded a visibility check.
    assert harness.events == []
    assert harness.store.visibilities == []


async def test_save_refuses_an_invisible_project_before_fetching_bytes() -> None:
    harness = Harness()
    harness.store.visibility_failure = ResourceNotFoundError()
    response = await call(harness, PATH, body=body(), headers={"Idempotency-Key": KEY})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    # A tombstoned or foreign project must not download, stage or publish anything.
    assert harness.source.bodies == []
    assert harness.zone.puts == []
    assert harness.store.commits == []


async def test_unconfigured_saved_assets_report_a_stable_unavailability() -> None:
    response = await call(None, PATH, body=body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workbench_unavailable"


def test_domain_rejects_inconsistent_or_unsafe_copy_facts() -> None:
    with pytest.raises(ValueError):
        copy(sha256="b" * 64)
    with pytest.raises(ValueError):
        copy(bytes=0)
    with pytest.raises(ValueError):
        copy(media_type="")
    with pytest.raises(ValueError):
        copy(source_receipt_id="")
    with pytest.raises(ValueError):
        saved_object_key(PROJECT, "sha256:" + "B" * 64)
    with pytest.raises(ValueError):
        asset(object_key="other-project/" + "b" * 64)
    assert project_object_prefix(PROJECT) == PROJECT + "/"
    assert saved_object_key(PROJECT, DIGEST) == PROJECT + "/" + "b" * 64


@pytest.mark.parametrize("bad", ["../private", "", "UPPERCASE", "a"])
async def test_application_rejects_unsafe_request_facts_before_any_io(bad: str) -> None:
    harness = Harness()
    with pytest.raises(InvalidVersionChangeError):
        await harness.service.save(
            project_id=PROJECT,
            workspace_id="workspace-demo",
            source_artifact_id=bad,
            source_receipt_id=RECEIPT,
        )
    assert harness.source.bodies == [] and harness.events == []
