"""The served v1/v2 boundary never invents a binding or leaks a v2 field into v1."""

from dataclasses import replace
from typing import Any

import httpx
import pytest

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.listings import ListingPage
from speechnexus_backend.workbench.application.render_units import WorkbenchRenderUnits
from speechnexus_backend.workbench.domain.render_units import RenderUnit, RenderUnitSummary
from tests.unit.backend.test_workbench_http_meetings import Service as Meetings
from tests.unit.backend.test_workbench_render_units import LATER, NOW, unit


class Renders(WorkbenchRenderUnits):
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    async def create_unit(self, **value: Any) -> RenderUnit:
        self.created.append(value)
        return unit()

    async def get_unit(self, **value: Any) -> RenderUnit:
        return unit()

    async def update_unit(self, **value: Any) -> RenderUnit:
        self.updated.append(value)
        current = unit()
        return current.revise(
            expected_revision=value["expected_revision"],
            dependencies=replace(current.dependencies, text=value["text"]),
            now=LATER,
        )

    async def list_units(self, **value: Any) -> ListingPage[RenderUnitSummary]:
        return ListingPage((RenderUnitSummary("unit", NOW, unit()),), None)


def client(service: Renders) -> httpx.AsyncClient:
    app = create_app(
        None,
        meetings=Meetings(),
        render_units=service,
        default_workspace_id="workspace-demo",
        deployment_id="synthetic",
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.1,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic")


BODY: dict[str, Any] = {
    "ordinal": 0,
    "text": "Synthetic narration.",
    "voice": {"id": "voice", "revision": "3"},
    "model": "synthetic",
}


async def test_v1_binding_free_create_is_honestly_unavailable_without_any_write() -> None:
    service = Renders()
    async with client(service) as http:
        response = await http.post("/v1/workbench/chapters/chapter/render-units", json=BODY)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "workbench_unavailable"
    assert service.created == []


async def test_v2_requires_and_forwards_the_explicit_binding() -> None:
    service = Renders()
    async with client(service) as http:
        missing = await http.post("/v2/workbench/chapters/chapter/render-units", json=BODY)
        assert missing.status_code == 400, missing.text
        assert service.created == []
        created = await http.post(
            "/v2/workbench/chapters/chapter/render-units",
            json={**BODY, "model_binding": {"id": "binding", "revision": "4"}},
        )
    assert created.status_code == 201, created.text
    assert created.json()["model_binding"] == {"id": "binding", "revision": "4"}
    assert service.created[0]["model_binding_id"] == "binding"
    assert service.created[0]["model_binding_revision"] == 4


async def test_render_reads_serialize_the_matching_contract_version() -> None:
    async with client(Renders()) as http:
        old = await http.get("/v1/workbench/render-units/unit")
        new = await http.get("/v2/workbench/render-units/unit")
    assert old.status_code == new.status_code == 200, (old.text, new.text)
    assert "model_binding" not in old.json()
    assert new.json().pop("model_binding") == {"id": "binding", "revision": "4"}
    assert old.json() == {key: value for key, value in new.json().items() if key != "model_binding"}


async def test_status_discovery_only_exposes_the_profile_in_v2() -> None:
    async with client(Renders()) as http:
        old = await http.get("/v1/workbench/status")
        new = await http.get("/v2/workbench/status")
    assert old.status_code == new.status_code == 200, (old.text, new.text)
    assert "minutes_profile" not in old.json()
    assert new.json()["minutes_profile"] == {
        "template_revision": "1",
        "llm_model": "synthetic-model",
    }


async def test_patch_and_listing_keep_each_version_of_the_same_fixed_binding() -> None:
    async with client(Renders()) as http:
        for version in (1, 2):
            patched = await http.patch(
                f"/v{version}/workbench/render-units/unit",
                json={"expected_revision": "1", "text": "Revised synthetic narration."},
            )
            assert patched.status_code == 200, patched.text
            assert (patched.json()["revision"], patched.json()["status"]) == ("2", "stale")
            assert ("model_binding" in patched.json()) is (version == 2)
            listed = await http.get(f"/v{version}/workbench/chapters/chapter/render-units")
            assert listed.status_code == 200, listed.text
            assert ("model_binding" in listed.json()["data"][0]) is (version == 2)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("field", ["pronunciation", "profile"])
async def test_unsupported_create_configuration_is_rejected_before_any_write(
    version: int, field: str
) -> None:
    service = Renders()
    body = {**BODY, field: {"id": "synthetic-option", "revision": "2"}}
    if version == 2:
        body["model_binding"] = {"id": "binding", "revision": "4"}
    async with client(service) as http:
        response = await http.post(
            f"/v{version}/workbench/chapters/chapter/render-units", json=body
        )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_change"
    assert service.created == service.updated == []


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("field", ["pronunciation", "profile"])
@pytest.mark.parametrize("clear", [False, True])
async def test_unsupported_patch_configuration_cannot_be_silently_dropped(
    version: int, field: str, clear: bool
) -> None:
    service = Renders()
    body = {
        "expected_revision": "1",
        "text": "A text change must not conceal an unsupported option.",
        field: None if clear else {"id": "synthetic-option", "revision": "2"},
    }
    async with client(service) as http:
        response = await http.patch(f"/v{version}/workbench/render-units/unit", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_change"
    assert service.created == service.updated == []
