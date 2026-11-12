"""Compose forwards the workspace writer identity; the API decides from it.

``deploy/compose.yaml`` did not carry ``SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL``
for the api service, so the assembly condition in :func:`create_app` could never
be true in a Compose deployment: both writes answered
``503 workspace_service_unavailable`` by design. These tests pin the wiring
itself, not the PostgreSQL authority:

- the api environment forwards the variable with the empty default, because
  ``:?`` would make ``docker compose config`` fail for every existing deployment
  that predates it; an unset value must still render and start;
- the process assembles the write port only when the writer DSN *and* the asset
  runtime exist, because the writer wraps the asset runtime's reads.

The role's granted surface and the startup self-check are covered by
``test_workspaces`` and the integration write tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

import speechnexus_backend.app as app_module
from speechnexus_backend.settings import Settings
from tests.unit.backend.test_health import _settings

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = ROOT / "deploy/compose.yaml"
EXAMPLE_PATH = ROOT / "deploy/compose.env.example"
WRITER = "SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL"
FIELD = "workspace_writer_database_url"
OPTIONAL_REFERENCE = "${" + WRITER + ":-}"
PREFIX = "/v1/nexus/workspaces"
API_ENV = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]["api"]["environment"]


def _with(**changes: object) -> Settings:
    return Settings(**_settings().model_dump() | changes)


def _object_storage() -> dict[str, object]:
    return {
        "object_storage_access_key": SecretStr("test-key"),
        "object_storage_secret_key": SecretStr("test-secret"),
    }


def test_the_compose_api_service_forwards_the_workspace_writer_dsn() -> None:
    assert API_ENV[WRITER] == OPTIONAL_REFERENCE


def test_the_writer_dsn_defaults_to_empty_so_an_unconfigured_stack_still_renders() -> None:
    """``:?`` would refuse to render for deployments that never set this variable.

    The project chose an empty default plus a startup self-check instead; the
    empty value Compose forwards must disable writes, not fail Settings.
    """
    assert API_ENV[WRITER] == OPTIONAL_REFERENCE
    assert ":?" not in API_ENV[WRITER]
    assert re.fullmatch(r"\$\{" + WRITER + r":-[^}]*\}", API_ENV[WRITER])
    assert _with(**{FIELD: ""}).workspace_writer_database_url is None


async def test_the_empty_value_compose_forwards_keeps_both_writes_at_503() -> None:
    app = app_module.create_app(settings=_with(**{FIELD: ""}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json={"display_name": "合成分组"})
        patched = await client.patch(
            PREFIX + "/workspace-demo",
            json={"expected_metadata_revision": 2, "display_name": "合成改名"},
        )
    assert created.status_code == patched.status_code == 503
    for response in (created, patched):
        assert response.json()["error"]["code"] == "workspace_service_unavailable"


def test_the_api_assembles_the_writer_only_with_the_dsn_and_the_asset_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DSN alone is not enough: writes wrap the asset runtime's reads.

    Compose always supplies the paired object-storage keys, so the only operator
    action left in that topology is provisioning the writer role.
    """
    assert "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY" in API_ENV
    assert "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY" in API_ENV
    dsn = SecretStr("postgresql://sn_workspace_writer@127.0.0.1/speechnexus")
    seen: list[Settings] = []

    class Spy:
        def __init__(self, settings: Settings) -> None:
            seen.append(settings)

    monkeypatch.setattr(app_module, "WorkspaceWriterRuntime", Spy)
    # No object-storage keys means no asset runtime, so there is nothing to wrap.
    app_module.create_app(settings=_with(**{FIELD: dsn}))
    assert seen == []
    app_module.create_app(settings=_with(**(_object_storage() | {FIELD: dsn})))
    assert [settings.workspace_writer_database_url for settings in seen] == [dsn]


def test_the_example_env_declares_the_writer_entry_blank() -> None:
    assert f"{WRITER}=" in EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
