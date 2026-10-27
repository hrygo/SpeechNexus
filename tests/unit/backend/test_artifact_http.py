"""Public Artifact metadata and downloads over synthetic committed facts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.artifacts import ArtifactService
from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord
from tests.unit.backend.test_health import _settings

CONTENT = b"synthetic artifact bytes"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
CREATED = 1_700_000_000_000_000
ARTIFACT_ID = "artifact-demo-1"
WORKSPACE = "workspace-default"


def record(**changes: object) -> ArtifactRecord:
    values: dict[str, object] = {
        "artifact_id": ARTIFACT_ID,
        "workspace_id": WORKSPACE,
        "kind": "audio",
        "status": "ready",
        "media_type": "audio/wav",
        "bytes": len(CONTENT),
        "sha256": DIGEST,
        "receipt_id": "receipt-demo-1",
        "source_operation_id": "operation-demo-1",
        "created_at_us": CREATED,
        "expires_at_us": None,
        "object_key": "private/artifacts/artifact-demo-1/object",
        "storage_version_id": "version-demo-1",
    }
    values.update(changes)
    return ArtifactRecord(**values)  # type: ignore[arg-type]


class Metadata:
    def __init__(self, value: ArtifactRecord | None = None, *, now: int = CREATED + 1) -> None:
        self.value = record() if value is None else value
        self.now = now
        self.calls: list[tuple[str, str]] = []

    def get(self, artifact_id: str, workspace_id: str) -> tuple[ArtifactRecord | None, int]:
        self.calls.append((artifact_id, workspace_id))
        if self.value.artifact_id != artifact_id or self.value.workspace_id != workspace_id:
            return None, self.now
        return self.value, self.now


class Reader:
    def __init__(self, body: bytes, *, failure: Exception | None = None) -> None:
        self._body, self._failure, self.position, self.closed = body, failure, 0, False

    def read(self, size: int) -> bytes:
        if self._failure is not None:
            raise self._failure
        chunk = self._body[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


class Objects:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[int, int]] = []
        self.readers: list[Reader] = []

    @contextmanager
    def open(self, artifact: ArtifactRecord, offset: int, length: int) -> Iterator[Reader]:
        self.calls.append((offset, length))
        reader = Reader(CONTENT[offset : offset + length], failure=self.failure)
        self.readers.append(reader)
        try:
            yield reader
        finally:
            reader.closed = True


def service(
    value: ArtifactRecord | None = None, *, now: int = CREATED + 1
) -> tuple[ArtifactService, Metadata, Objects]:
    metadata, objects = Metadata(value, now=now), Objects()
    return ArtifactService(metadata, objects, WORKSPACE), metadata, objects


def client(artifact_service: ArtifactService | None) -> httpx.AsyncClient:
    app = create_app(settings=_settings(), artifacts=artifact_service)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic")


async def test_metadata_is_contract_shaped_and_hides_storage_identity() -> None:
    artifacts, metadata, objects = service()
    async with client(artifacts) as http:
        reply = await http.get(f"/v1/nexus/artifacts/{ARTIFACT_ID}")
        matched = await http.get(
            f"/v1/nexus/artifacts/{ARTIFACT_ID}",
            headers={"If-None-Match": "W/" + reply.headers["etag"]},
        )
    assert reply.status_code == 200
    assert reply.json() == {
        "id": ARTIFACT_ID,
        "object": "nexus.artifact",
        "workspace_id": WORKSPACE,
        "kind": "audio",
        "status": "ready",
        "media_type": "audio/wav",
        "bytes": len(CONTENT),
        "sha256": DIGEST,
        "receipt_id": "receipt-demo-1",
        "source_operation_id": "operation-demo-1",
        "created_at": "2023-11-14T22:13:20.000000Z",
    }
    assert reply.headers["etag"].startswith('"sha256:')
    assert "private" not in reply.text and "version-demo-1" not in reply.text
    assert metadata.calls == [(ARTIFACT_ID, WORKSPACE)] * 2
    assert not objects.calls
    assert matched.status_code == 304 and matched.content == b""
    assert matched.headers["etag"] == reply.headers["etag"]


async def test_downloads_use_committed_length_digest_and_safe_filename() -> None:
    artifacts, _, objects = service()
    async with client(artifacts) as http:
        reply = await http.get(f"/v1/nexus/artifacts/{ARTIFACT_ID}/content")
        matched = await http.get(
            f"/v1/nexus/artifacts/{ARTIFACT_ID}/content",
            headers={"If-None-Match": '"unrelated", ' + '"' + DIGEST + '"'},
        )
    assert reply.status_code == 200
    assert reply.content == CONTENT
    assert reply.headers["content-type"] == "application/octet-stream"
    assert reply.headers["content-length"] == str(len(CONTENT))
    assert reply.headers["content-disposition"] == ('attachment; filename="artifact-demo-1.wav"')
    assert reply.headers["etag"] == '"' + DIGEST + '"'
    assert reply.headers["accept-ranges"] == "bytes"
    assert "/" not in reply.headers["content-disposition"]
    assert matched.status_code == 304 and matched.content == b""
    assert objects.calls == [(0, len(CONTENT))]
    assert all(reader.closed for reader in objects.readers)


async def test_ranges_are_exact_and_invalid_ranges_have_a_stable_416() -> None:
    artifacts, _, objects = service()
    async with client(artifacts) as http:
        partial = await http.get(
            f"/v1/nexus/artifacts/{ARTIFACT_ID}/content", headers={"Range": "bytes=3-8"}
        )
        duplicate = await http.get(
            f"/v1/nexus/artifacts/{ARTIFACT_ID}/content",
            headers=[("Range", "bytes=0-1"), ("Range", "bytes=2-3")],
        )
    assert partial.status_code == 206
    assert partial.content == CONTENT[3:9]
    assert partial.headers["content-range"] == f"bytes 3-8/{len(CONTENT)}"
    assert partial.headers["content-length"] == "6"
    assert duplicate.status_code == 416
    assert duplicate.headers["content-range"] == f"bytes */{len(CONTENT)}"
    assert duplicate.json()["error"]["code"] == "artifact_range_not_satisfiable"
    assert objects.calls == [(3, 6)]


async def test_unknown_foreign_staging_and_unavailable_content_are_hidden() -> None:
    artifacts, _, objects = service(record(workspace_id="workspace-other"))
    expired = record(status="expired")
    deleted = record(status="deleted")
    past = record(expires_at_us=CREATED + 10)
    async with client(artifacts) as http:
        unknown = await http.get("/v1/nexus/artifacts/artifact-missing")
        foreign = await http.get(f"/v1/nexus/artifacts/{ARTIFACT_ID}")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "artifact_not_found"
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "artifact_not_found"
    for value, expected_status in ((expired, "expired"), (deleted, "deleted"), (past, "expired")):
        artifacts, _, objects = service(value, now=CREATED + 100)
        async with client(artifacts) as http:
            reply = await http.get(f"/v1/nexus/artifacts/{ARTIFACT_ID}/content")
            metadata = await http.get(f"/v1/nexus/artifacts/{ARTIFACT_ID}")
        assert reply.status_code == 410
        assert reply.json()["error"]["code"] == "artifact_unavailable"
        assert metadata.status_code == 200
        assert metadata.json()["status"] == expected_status
        assert not objects.calls
    assert not objects.calls


async def test_storage_failure_never_echoes_object_details() -> None:
    metadata = Metadata()
    objects = Objects(failure=ArtifactError("object-key-private"))
    artifacts = ArtifactService(metadata, objects, WORKSPACE)
    async with client(artifacts) as http:
        reply = await http.get(f"/v1/nexus/artifacts/{ARTIFACT_ID}/content")
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "artifact_storage_unavailable"
    assert "object-key-private" not in reply.text
    assert "private/artifacts" not in reply.text


async def test_unconfigured_and_wrong_methods_use_stable_errors() -> None:
    async with client(None) as http:
        missing = await http.get(f"/v1/nexus/artifacts/{ARTIFACT_ID}")
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "artifact_service_unavailable"
    artifacts, _, _ = service()
    async with client(artifacts) as http:
        wrong = await http.post(f"/v1/nexus/artifacts/{ARTIFACT_ID}")
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "artifact_method_not_allowed"
