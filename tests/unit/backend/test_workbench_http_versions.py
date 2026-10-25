"""Actual ASGI requests consume generated HTTP validation and content use cases."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from speechnexus_contracts.workbench_http import validate_response

from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.infrastructure.postgres_versions import VersionStoreError
from speechnexus_backend.workbench.interfaces.http_versions import create_workbench_app
from tests.fake.workbench_versions import MemoryVersionUnitOfWork


def client(service: WorkbenchVersions | None, *, limit: int = 4096) -> httpx.AsyncClient:
    app = create_workbench_app(
        service,
        default_workspace_id="workspace-synthetic",
        max_json_bytes=limit,
        body_timeout_seconds=0.05,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic")


def setup() -> tuple[MemoryVersionUnitOfWork, WorkbenchVersions]:
    store = MemoryVersionUnitOfWork(now=datetime(2026, 10, 24, tzinfo=UTC))
    return store, WorkbenchVersions(store)


def error(response: httpx.Response, status: int, code: str, operation: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert body["error"]["retryable"] is False
    assert response.headers["cache-control"] == "no-store"
    validate_response(operation, status, {"body": body, "headers": dict(response.headers)})
    assert "private-synthetic" not in response.text


async def test_http_original_keys_cas_history_exact_unicode_and_generated_response() -> None:
    store, service = setup()
    async with client(service) as c:
        req = {"name": " 合成😀 ", "description": None}
        first = await c.post(
            "/v1/workbench/projects",
            json=req,
            headers={
                "Idempotency-Key": "project-original",
                "X-Request-ID": "synthetic-correlation",
            },
        )
        assert first.status_code == 201 and first.headers["x-request-id"] == "synthetic-correlation"
        p = first.json()
        assert p["workspace_id"] == "workspace-synthetic" and p["name"] == req["name"]
        path = f"/v1/workbench/projects/{p['id']}"
        changed = await c.patch(
            path,
            json={"expected_metadata_revision": 1, "name": "新名"},
            headers={"Idempotency-Key": "project-update"},
        )
        assert changed.status_code == 200 and changed.json()["metadata_revision"] == 2
        replay = await c.post(
            "/v1/workbench/projects", json=req, headers={"Idempotency-Key": "project-original"}
        )
        assert replay.json() == p
        assert (await c.get(path)).json() == changed.json()
        error(
            await c.patch(path, json={"expected_metadata_revision": 1, "name": "冲突"}),
            409,
            "revision_conflict",
            "updateProject",
        )
        error(
            await c.post(
                "/v1/workbench/projects",
                json={"name": "不同"},
                headers={"Idempotency-Key": "project-original"},
            ),
            409,
            "idempotency_conflict",
            "createProject",
        )
        t = await c.post(
            path + "/transcripts",
            json={"display_name": "合成稿", "source": {"kind": "manual", "text": " e\u0301\n😀 "}},
            headers={"Idempotency-Key": "transcript-original"},
        )
        assert t.status_code == 201
        old = t.json()
        tp = f"/v1/workbench/transcripts/{old['transcript']['id']}"
        revised = await c.post(
            tp + "/revisions",
            json={"expected_revision": "1", "text": ""},
            headers={"Idempotency-Key": "revision-original"},
        )
        assert revised.status_code == 201 and revised.json()["revision"] == "2"
        assert revised.json()["text"] == "" and revised.json()["source_revision"] == "1"
        current = await c.get(tp)
        assert (
            current.json()["transcript"]["head_revision"] == "2"
            and current.json()["revision"] == revised.json()
        )
        historical = await c.get(tp, params={"revision": "1"})
        assert historical.json()["revision"] == old["revision"]
        assert historical.json()["transcript"]["head_revision"] == "2"
        assert (await c.get(tp + "/revisions/1")).json() == old["revision"]
        replay = await c.post(
            tp + "/revisions",
            json={"expected_revision": "1", "text": ""},
            headers={"Idempotency-Key": "revision-original"},
        )
        assert replay.json() == revised.json()
        error(
            await c.get(tp, params={"revision": "99"}), 404, "resource_not_found", "getTranscript"
        )
        for op, r in [
            ("createProject", first),
            ("updateProject", changed),
            ("createTranscript", t),
            ("getTranscript", historical),
            ("createTranscriptRevision", revised),
        ]:
            validate_response(op, r.status_code, {"body": r.json(), "headers": dict(r.headers)})
    assert len(store.projects) == 1 and len(store.transcripts) == 1 and len(store.revisions) == 2


@pytest.mark.parametrize(
    "content,headers,status,code",
    [
        (b'{"name":"one","name":"private-synthetic"}', {}, 400, "malformed_request"),
        (b'{"name":NaN}', {}, 400, "malformed_request"),
        (b'{"name":"\xff"}', {}, 400, "malformed_request"),
        (b'{"name":', {}, 400, "malformed_request"),
        (b"[]", {}, 400, "invalid_request"),
        (b'{"name":"ok","unknown":"private-synthetic"}', {}, 400, "invalid_request"),
        (b'{"name":"ok"}', {"Content-Type": "text/plain"}, 415, "unsupported_media_type"),
        (
            b'{"name":"ok"}',
            {"Content-Type": "application/json; charset=latin1"},
            415,
            "unsupported_media_type",
        ),
        (b'{"name":"ok"}', {"Content-Encoding": "gzip"}, 415, "unsupported_media_type"),
        (b'{"name":"ok"}', {"Idempotency-Key": "short"}, 400, "invalid_request"),
        (b'{"name":"ok"}', {"X-Request-ID": "x" * 129}, 400, "invalid_request"),
        (b'{"name":"' + b"x" * 100 + b'"}', {}, 413, "request_too_large"),
        (b'{"name":"\\ud800"}', {}, 400, "malformed_request"),
    ],
)
async def test_rejected_ingress_never_mutates_or_leaks(
    content: bytes, headers: dict[str, str], status: int, code: str
) -> None:
    store, service = setup()
    async with client(service, limit=64) as c:
        r = await c.post(
            "/v1/workbench/projects",
            content=content,
            headers={"Content-Type": "application/json", **headers},
        )
        error(r, status, code, "createProject")
    assert not store.projects and not store.receipts


@pytest.mark.parametrize(
    "path",
    [
        "/v1/workbench/projects/project-one?unknown=private-synthetic",
        "/v1/workbench/transcripts/transcript-one?revision=1&revision=2",
        "/v1/workbench/transcripts/transcript-one?revision=01",
        "/v1/workbench/transcripts/transcript-one/revisions/1.0",
    ],
)
async def test_invalid_query_and_path_are_rejected_before_lookup(path: str) -> None:
    _, service = setup()
    async with client(service) as c:
        r = await c.get(path)
    assert r.status_code == 400 and "private-synthetic" not in r.text


async def test_duplicate_semantic_headers_and_get_body_are_rejected() -> None:
    store, service = setup()
    async with client(service) as c:
        for name, value in [
            ("Idempotency-Key", "original-key"),
            ("X-Request-ID", "req-one"),
            ("Content-Type", "application/json"),
        ]:
            r = await c.post(
                "/v1/workbench/projects",
                content='{"name":"合成"}',
                headers=[("Content-Type", "application/json"), (name, value), (name, value)],
            )
            error(r, 400, "malformed_request", "createProject")
        error(
            await c.request(
                "GET", "/v1/workbench/projects/project-one", content="private-synthetic"
            ),
            400,
            "invalid_request",
            "getProject",
        )
    assert not store.projects


@pytest.mark.parametrize(
    "source_code,status",
    [
        ("source_invalid", 422),
        ("source_unsupported", 422),
        ("source_too_large", 413),
        ("source_expired", 410),
        ("source_unavailable", 503),
    ],
)
async def test_source_failures_keep_identity_and_safe_declared_status(
    source_code: Any, status: int
) -> None:
    store, _ = setup()

    class Source:
        async def read(self, **kw: str) -> str:
            assert not store._lock.locked()
            raise TranscriptSourceError(source_code)

    service = WorkbenchVersions(store, source_reader=Source())
    project = await service.create_project(workspace_id="workspace-synthetic", name="合成")
    async with client(service) as c:
        r = await c.post(
            f"/v1/workbench/projects/{project.id}/transcripts",
            json={
                "display_name": "合成",
                "source": {
                    "kind": "speech_artifact",
                    "artifact_id": "artifact-one",
                    "receipt_id": "receipt-one",
                },
            },
            headers={"Idempotency-Key": "source-original"},
        )
        error(r, status, source_code, "createTranscript")
    assert not store.transcripts and not store.receipts


async def test_disabled_service_and_commit_unknown_never_retry_or_leak() -> None:
    async with client(None) as c:
        error(
            await c.post("/v1/workbench/projects", json={"name": "合成"}),
            503,
            "workbench_unavailable",
            "createProject",
        )

    class Failed(WorkbenchVersions):
        calls = 0

        async def create_project(self, **kw: Any) -> Any:
            self.calls += 1
            raise VersionStoreError(sqlstate="08006", commit_unknown=True)

    store, _ = setup()
    service = Failed(store)
    async with client(service) as c:
        error(
            await c.post("/v1/workbench/projects", json={"name": "合成"}),
            503,
            "outcome_unknown",
            "createProject",
        )
    assert service.calls == 1


async def test_bad_producer_and_unexpected_error_are_sanitized_and_cancel_propagates() -> None:
    store, _ = setup()

    class Failed(WorkbenchVersions):
        failure: Any = None

        async def get_project(self, project_id: str) -> Any:
            if self.failure:
                raise self.failure
            return {"private-synthetic": "bad response"}

    service = Failed(store)
    async with client(service) as c:
        error(
            await c.get("/v1/workbench/projects/project-one"), 500, "internal_error", "getProject"
        )
        service.failure = RuntimeError("private-synthetic")
        error(
            await c.get("/v1/workbench/projects/project-one"), 500, "internal_error", "getProject"
        )
        service.failure = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await c.get("/v1/workbench/projects/project-one")


async def test_streaming_limit_and_deadline_precede_dispatch() -> None:
    store, service = setup()

    async def stream() -> Any:
        yield b'{"name":"'
        yield b"x" * 80
        raise AssertionError("intake must stop without asking for more bytes")

    async def slow() -> Any:
        yield b"{"
        await asyncio.sleep(0.2)
        yield b"}"

    async with client(service, limit=64) as c:
        error(
            await c.post(
                "/v1/workbench/projects",
                content=stream(),
                headers={"Content-Type": "application/json"},
            ),
            413,
            "request_too_large",
            "createProject",
        )
        error(
            await c.post(
                "/v1/workbench/projects",
                content=slow(),
                headers={"Content-Type": "application/json"},
            ),
            408,
            "request_timeout",
            "createProject",
        )
    assert not store.projects


def test_openapi_advertises_only_registered_seven_operations_and_is_isolated() -> None:
    _, service = setup()
    app = create_workbench_app(
        service,
        default_workspace_id="workspace-synthetic",
        max_json_bytes=4096,
        body_timeout_seconds=1,
    )
    doc = app.openapi()
    ops = {op["operationId"] for path in doc["paths"].values() for op in path.values()}
    assert ops == {
        "createProject",
        "getProject",
        "updateProject",
        "createTranscript",
        "getTranscript",
        "createTranscriptRevision",
        "getTranscriptRevision",
    }
    assert "/v1/workbench/status" not in doc["paths"]
    doc["paths"].clear()
    assert app.openapi()["paths"]


async def test_source_import_preserves_workspace_and_replays_without_io() -> None:
    store, _ = setup()
    calls: list[dict[str, str]] = []

    class Source:
        async def read(self, **kw: str) -> str:
            assert not store._lock.locked()
            calls.append(kw)
            return " 合成😀\n "

    service = WorkbenchVersions(store, source_reader=Source())
    project = await service.create_project(workspace_id="workspace-synthetic", name="合成")
    path = f"/v1/workbench/projects/{project.id}/transcripts"
    body = {
        "display_name": "合成稿",
        "source": {
            "kind": "speech_artifact",
            "artifact_id": "artifact-one",
            "receipt_id": "receipt-one",
        },
    }
    async with client(service) as c:
        first = await c.post(path, json=body, headers={"Idempotency-Key": "import-original"})
        assert first.status_code == 201
        replay = await c.post(path, json=body, headers={"Idempotency-Key": "import-original"})
        assert replay.json() == first.json()
    assert first.json()["revision"]["text"] == " 合成😀\n "
    assert first.json()["revision"]["source_artifact_id"] == "artifact-one"
    assert first.json()["transcript"]["source_receipt_id"] == "receipt-one"
    assert calls == [
        {
            "artifact_id": "artifact-one",
            "receipt_id": "receipt-one",
            "workspace_id": "workspace-synthetic",
        }
    ]


@pytest.mark.parametrize(
    "byte_limit,deadline", [(0, 1), (-1, 1), (True, 1), (1, 0), (1, True), (1, float("inf"))]
)
def test_invalid_intake_policy_cannot_enable_app(byte_limit: Any, deadline: Any) -> None:
    with pytest.raises(ValueError):
        create_workbench_app(
            None,
            default_workspace_id="workspace-synthetic",
            max_json_bytes=byte_limit,
            body_timeout_seconds=deadline,
        )


async def test_unregistered_routes_and_methods_use_safe_errors_without_redirects() -> None:
    _, service = setup()
    async with client(service) as c:
        assert (await c.post("/v1/workbench/projects/", json={"name": "合成"})).status_code == 404
        r = await c.delete("/v1/workbench/projects/project-one")
        assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
        assert "GET" in r.headers["allow"] or "PATCH" in r.headers["allow"]


async def test_default_docs_cannot_load_assets_outside_deployment_network() -> None:
    _, service = setup()
    async with client(service) as c:
        for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
            response = await c.get(path)
            assert response.status_code == 404, response.text
            assert "https://" not in response.text and "http://" not in response.text
        schema = await c.get("/openapi.json")
        assert schema.status_code == 200 and schema.json()["paths"]
