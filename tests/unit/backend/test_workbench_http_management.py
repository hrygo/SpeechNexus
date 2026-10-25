"""C-owned full C1 app reports actual status and consumes generated management boundaries."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.domain.deletion import (
    ConfirmationMismatchError,
    DeletionDependencyConflictError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionPlanExpiredError,
    DeletionTarget,
)
from tests.fake.workbench_versions import MemoryVersionUnitOfWork

KEY = b"synthetic-cursor-key-32-bytes-only"
NOW = datetime(2026, 10, 24, tzinfo=UTC)


def app(versions: Any = None, deletions: Any = None, workspace: str = "workspace-one") -> Any:
    return create_app(
        versions,
        deletions,
        default_workspace_id=workspace,
        deployment_id="deployment-one",
        cursor_key=KEY,
        max_json_bytes=4096,
        body_timeout_seconds=0.1,
    )


def client(application: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://synthetic"
    )


async def test_status_reports_configuration_and_full_schema_without_fictitious_capabilities() -> (
    None
):
    async with client(app()) as c:
        status = await c.get("/v1/workbench/status")
        assert status.status_code == 200 and status.json() == {
            "object": "nexus.workbench_status",
            "workbench_enabled": False,
            "llm_enabled": False,
            "shared_deployment_notice": "shared_deployment",
            "capabilities": [],
        }
        assert (await c.get("/v1/workbench/projects")).status_code == 503
        assert (await c.get("/v1/workbench/deletions/deletion-one")).status_code == 503
        schema = (await c.get("/openapi.json")).json()
        # Both status versions, two voice reads and two public downloads stay mounted.
        assert len([op for item in schema["paths"].values() for op in item.values()]) == 18
        for path, operation in {
            "/v1/workbench/saved-assets/{asset_id}/content": "downloadSavedAsset",
            "/v1/workbench/exports/{export_id}/content": "downloadBookExport",
        }.items():
            declared = schema["paths"][path]["get"]
            assert declared["operationId"] == operation
            assert "416" in declared["responses"]
            actual = path.format(asset_id="asset-demo", export_id="export-demo")
            unavailable = await c.get(actual)
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "download_unavailable"
        for path in ["/docs", "/redoc", "/docs/oauth2-redirect"]:
            assert (await c.get(path)).status_code == 404


async def test_bound_project_pagination_preserves_ties_and_rejects_wrong_scope() -> None:
    memory = MemoryVersionUnitOfWork(now=NOW)
    service = WorkbenchVersions(memory)
    for _ in range(5):
        await service.create_project(workspace_id="workspace-one", name="合成项目")
    await service.create_project(workspace_id="workspace-two", name="其他作用域")
    async with client(app(service)) as c:
        assert (await c.get("/v1/workbench/status")).json()["workbench_enabled"] is True
        first = (await c.get("/v1/workbench/projects", params={"limit": "2"})).json()
        assert first["has_more"] and len(first["data"]) == 2 and first["next_cursor"]
        cursor = first["next_cursor"]
        bad = await c.get(
            "/v1/workbench/projects", params={"cursor": cursor, "workspace_id": "workspace-two"}
        )
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_cursor"
        rows = first["data"]
        while cursor:
            page = (
                await c.get("/v1/workbench/projects", params={"limit": "1", "cursor": cursor})
            ).json()
            assert page["has_more"] == (page["next_cursor"] is not None)
            rows += page["data"]
            cursor = page["next_cursor"]
        assert [r["id"] for r in rows] == sorted(
            (p.id for p in memory.projects.values() if p.workspace_id == "workspace-one"),
            reverse=True,
        )
        for query in [
            "limit=01",
            "limit=1.0",
            "limit=101",
            "cursor=private-synthetic",
            "limit=1&limit=2",
        ]:
            r = await c.get("/v1/workbench/projects?" + query)
            assert r.status_code == 400 and "private-synthetic" not in r.text


async def test_transcript_pages_are_metadata_only_and_cursor_is_not_a_project_cursor() -> None:
    memory = MemoryVersionUnitOfWork(now=NOW)
    service = WorkbenchVersions(memory)
    p = await service.create_project(workspace_id="workspace-one", name="合成")
    for _ in range(3):
        await service.create_manual_transcript(
            project_id=p.id, display_name="合成稿", text="private-synthetic-body"
        )
    async with client(app(service)) as c:
        path = f"/v1/workbench/projects/{p.id}/transcripts"
        one = (await c.get(path, params={"limit": "1"})).json()
        assert one["has_more"] and "private-synthetic" not in str(one) and "text" not in str(one)
        bad = await c.get("/v1/workbench/projects", params={"cursor": one["next_cursor"]})
        assert bad.status_code == 400
        assert (await c.get(path, params={"cursor": one["next_cursor"]})).json()[
            "has_more"
        ] is False
        assert (await c.get("/v1/workbench/projects/missing/transcripts")).status_code == 404


class Deletions(WorkbenchDeletions):
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.failure: Exception | None = None
        self.plan = DeletionPlan.create(
            plan_id="plan-one",
            workspace_id="workspace-two",
            target=DeletionTarget("workbench", "project", "project-one", 1),
            mode="ordinary",
            now=NOW,
            dependencies=(),
        )

    async def preview(self, **kw: Any) -> DeletionPlan:
        self.calls.append(kw)
        if self.failure:
            raise self.failure
        return self.plan


async def test_deletion_plan_generated_projection_and_explicit_workspace_key_mapping() -> None:
    d = Deletions()
    body = {
        "target": {
            "owner": "workbench",
            "kind": "project",
            "resource": {"id": "project-one", "revision": "1"},
        },
        "mode": "ordinary",
    }
    async with client(app(None, d)) as c:
        r = await c.post(
            "/v1/workbench/deletion-plans?workspace_id=workspace-two",
            json=body,
            headers={"Idempotency-Key": "p"},
        )
        assert (
            r.status_code == 200
            and r.json()["id"] == "plan-one"
            and r.json()["plan_digest"].startswith("sha256:")
        )
        assert d.calls == [
            {
                "workspace_id": "workspace-two",
                "target": d.plan.target,
                "mode": "ordinary",
                "key": "p",
            }
        ]
        for target, code in [
            (
                {
                    "owner": "speech",
                    "kind": "artifact",
                    "resource": {"id": "artifact-one", "revision": "1"},
                },
                "deletion_owner_mismatch",
            ),
            (
                {
                    "owner": "workbench",
                    "kind": "meeting",
                    "resource": {"id": "meeting-one", "revision": "1"},
                },
                "deletion_resource_unsupported",
            ),
        ]:
            denied = await c.post(
                "/v1/workbench/deletion-plans",
                json={**body, "target": target},
                headers={"Idempotency-Key": "p"},
            )
            assert denied.status_code == 422 and denied.json()["error"]["code"] == code
        missing = await c.post("/v1/workbench/deletion-plans", json=body)
        assert missing.status_code == 400 and len(d.calls) == 1


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (ConfirmationMismatchError(), 422, "deletion_confirmation_mismatch"),
        (DeletionPlanExpiredError(), 409, "deletion_plan_expired"),
        (DeletionPlanChangedError(), 409, "deletion_plan_changed"),
        (DeletionDependencyConflictError(), 409, "deletion_dependency_conflict"),
        (RuntimeError("private-synthetic"), 500, "internal_error"),
        (
            VersionStoreError(sqlstate="private-synthetic", commit_unknown=False),
            503,
            "store_unavailable",
        ),
        (
            VersionStoreError(sqlstate="private-synthetic", commit_unknown=True),
            503,
            "outcome_unknown",
        ),
    ],
)
async def test_deletion_errors_keep_declared_status_and_never_leak(
    failure: Exception, status: int, code: str
) -> None:
    d = Deletions()
    d.failure = failure
    body = {
        "target": {
            "owner": "workbench",
            "kind": "project",
            "resource": {"id": "project-one", "revision": "1"},
        },
        "mode": "ordinary",
    }
    async with client(app(None, d)) as c:
        r = await c.post(
            "/v1/workbench/deletion-plans", json=body, headers={"Idempotency-Key": "p"}
        )
        assert (
            r.status_code == status
            and r.json()["error"]["code"] == code
            and "private-synthetic" not in r.text
        )


async def test_405_allow_includes_all_registered_methods() -> None:
    async with client(app()) as c:
        r = await c.delete("/v1/workbench/projects/project-one")
        assert r.status_code == 405 and {x.strip() for x in r.headers["allow"].split(",")} == {
            "GET",
            "PATCH",
        }


async def test_list_default_limit_is_fifty_and_maximum_one_hundred_is_accepted() -> None:
    memory = MemoryVersionUnitOfWork(now=NOW)
    versions = WorkbenchVersions(memory)
    for _ in range(51):
        await versions.create_project(workspace_id="workspace-one", name="合成分页")
    async with client(app(versions)) as c:
        first = (await c.get("/v1/workbench/projects")).json()
        assert len(first["data"]) == 50 and first["has_more"]
        rest = (
            await c.get("/v1/workbench/projects", params={"cursor": first["next_cursor"]})
        ).json()
        assert len(rest["data"]) == 1 and rest["next_cursor"] is None
        maximum = await c.get("/v1/workbench/projects", params={"limit": "100"})
        assert maximum.status_code == 200 and len(maximum.json()["data"]) == 51


@pytest.mark.parametrize("path", ["deletion-plans", "deletions"])
@pytest.mark.parametrize(
    "content,media,status",
    [
        ("{", "application/json", 400),
        ('{"plan_id":"one","plan_id":"two"}', "application/json", 400),
        ("x" * 4097, "application/json", 413),
        ("{}", "text/plain", 415),
    ],
)
async def test_deletion_ingress_rejects_before_service_calls(
    path: str,
    content: str,
    media: str,
    status: int,
) -> None:
    deletions = Deletions()
    async with client(app(None, deletions)) as c:
        response = await c.post(
            "/v1/workbench/" + path,
            content=content,
            headers={"Content-Type": media, "Idempotency-Key": "key-original"},
        )
        assert response.status_code == status
        assert response.json()["error"]["retryable"] is False
        assert response.headers["x-should-retry"] == "false"
        assert deletions.calls == []
