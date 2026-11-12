"""Compose forwards the read-only projection identities; the API decides from them.

``deploy/compose.yaml`` did not carry ``SPEECHNEXUS_AUDIT_READER_DATABASE_URL``,
``SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL``,
``SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL`` or
``SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL`` for the api service, so the
assembly conditions in :func:`create_app` could never be true in a Compose
deployment and the corresponding endpoints answered their documented 503 by
design::

    GET /v1/nexus/audit-events        -> 503 audit_service_unavailable
    GET /v1/models, /v1/models/{id}   -> 503 model_catalogue_unavailable
    GET /v1/nexus/artifacts/{id}      -> 503 artifact_service_unavailable
    GET /v1/nexus/admin/model-instances
    GET /v1/nexus/admin/model-instances/{id}/generations/{generation}
                                      -> 503 model_instance_service_unavailable

These tests pin the wiring itself, not the PostgreSQL authority:

- the api environment forwards each variable with the empty default, because
  ``:?`` would make ``docker compose config`` fail for every existing deployment
  that predates it; an unset value must still render and start;
- the empty value Compose forwards disables the projection instead of failing
  ``Settings``, so the documented 503 stays the unconfigured answer;
- the process assembles each runtime only when its own DSN is set; the Artifact
  runtime additionally keeps the paired object-storage keys it always required.

The role surfaces and the startup self-checks are covered by the audit, catalog,
artifact and model instance integration tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import cast

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
API_ENV = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]["api"]["environment"]

# variable, Settings field, endpoint that depends on it, error code when unset.
READERS = [
    (
        "SPEECHNEXUS_AUDIT_READER_DATABASE_URL",
        "audit_reader_database_url",
        "/v1/nexus/audit-events",
        "audit_service_unavailable",
    ),
    (
        "SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL",
        "model_catalog_reader_database_url",
        "/v1/models",
        "model_catalogue_unavailable",
    ),
    (
        "SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL",
        "artifact_reader_database_url",
        "/v1/nexus/artifacts/artifact-demo",
        "artifact_service_unavailable",
    ),
    (
        "SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL",
        "model_instance_reader_database_url",
        "/v1/nexus/admin/model-instances",
        "model_instance_service_unavailable",
    ),
]
IDS = [variable for variable, *_ in READERS]
DSNS = {
    "SPEECHNEXUS_AUDIT_READER_DATABASE_URL": (
        "postgresql://sn_audit_reader:placeholder@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL": (
        "postgresql://sn_catalog_reader:placeholder@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL": (
        "postgresql://sn_artifact_reader:placeholder@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL": (
        "postgresql://sn_model_instance_reader:placeholder@postgres:5432/speechnexus"
    ),
}

# docker compose refuses to render without the :? interpolation inputs. These
# placeholders are synthetic and exist only to make the render exit zero.
RENDER_INPUTS = {
    "POSTGRES_PASSWORD": "placeholder-owner",
    "MINIO_ROOT_PASSWORD": "placeholder-object",
    "SPEECHNEXUS_API_DATABASE_URL": "postgresql://sn_api:placeholder@postgres:5432/speechnexus",
    "SPEECHNEXUS_ENGINE_HEALTH_URL": "http://host.docker.internal:18100/health",
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "placeholder-component",
    "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL": (
        "postgresql://sn_gateway:placeholder@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL": (
        "postgresql://sn_gateway_query:placeholder@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_GATEWAY_LOCK_DIR": "/tmp/speechnexus-render-locks",
    "SPEECHNEXUS_GATEWAY_SPOOL_DIR": "/tmp/speechnexus-render-spool",
    "SPEECHNEXUS_MIGRATION_DATABASE_URL": "postgresql://owner:placeholder@postgres:5432/speechnexus",
    "SPEECHNEXUS_PRIVATE_CONFIG_DIR": "/tmp/speechnexus-render-private",
    "SPEECHNEXUS_REGISTRATION_DATABASE_URL": "postgresql://owner:placeholder@postgres:5432/speechnexus",
    "SPEECHNEXUS_RUNTIME_UID": "10001",
    "SPEECHNEXUS_RUNTIME_GID": "10001",
}


def _with(**changes: object) -> Settings:
    return Settings(**_settings().model_dump() | changes)


def _object_storage() -> dict[str, object]:
    return {
        "object_storage_access_key": SecretStr("test-key"),
        "object_storage_secret_key": SecretStr("test-secret"),
    }


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, timeout=60, check=False
        )
    except OSError, subprocess.SubprocessError:
        return False
    return probe.returncode == 0


def _render(dsn_lines: dict[str, str], directory: Path) -> dict[str, object]:
    """Render one deployment and return the api service environment."""
    env_file = directory / "compose.env"
    entries = list(RENDER_INPUTS.items()) + list(dsn_lines.items())
    env_file.write_text("".join(f"{name}={value}\n" for name, value in entries), encoding="utf-8")
    # The shell environment wins over --env-file during interpolation, so an
    # ambient SPEECHNEXUS_* value would silently take over the rendered value.
    environment = {name: value for name, value in os.environ.items() if "SPEECHNEXUS" not in name}
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), "--env-file", str(env_file), "config"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    document = cast(dict[str, object], yaml.safe_load(rendered.stdout))
    services = cast(dict[str, dict[str, object]], document["services"])
    return cast(dict[str, object], services["api"]["environment"])


@pytest.mark.parametrize("variable,field,path,code", READERS, ids=IDS)
def test_the_compose_api_service_forwards_the_reader_dsn(
    variable: str, field: str, path: str, code: str
) -> None:
    assert API_ENV[variable] == "${" + variable + ":-}"


@pytest.mark.parametrize("variable,field,path,code", READERS, ids=IDS)
def test_the_reader_dsn_defaults_to_empty_so_an_unconfigured_stack_still_renders(
    variable: str, field: str, path: str, code: str
) -> None:
    """``:?`` would refuse to render for deployments that never set this variable.

    The project chose an empty default plus a startup self-check instead; the
    empty value Compose forwards must disable the projection, not fail Settings.
    """
    reference = API_ENV[variable]
    assert ":?" not in reference
    assert re.fullmatch(r"\$\{" + variable + r":-[^}]*\}", reference)
    assert getattr(_with(**{field: ""}), field) is None


@pytest.mark.parametrize("variable,field,path,code", READERS, ids=IDS)
async def test_the_empty_value_compose_forwards_keeps_each_projection_at_503(
    variable: str, field: str, path: str, code: str
) -> None:
    app = app_module.create_app(settings=_with(**{field: ""}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "path",
    [
        "/v1/nexus/admin/model-instances",
        "/v1/nexus/admin/model-instances/instance-demo/generations/1",
    ],
)
async def test_the_empty_instance_reader_value_keeps_both_endpoints_at_503(path: str) -> None:
    """The list and the single-generation GET share one assembly condition.

    A reader identity that Compose forwards as empty must leave both endpoints on
    the documented 503, not silently serve an empty page or a fabricated 404.
    """
    app = app_module.create_app(settings=_with(model_instance_reader_database_url=""))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_instance_service_unavailable"


def test_no_runtime_is_assembled_while_every_reader_dsn_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[Settings]] = {
        "audit": [],
        "catalogue": [],
        "artifact": [],
        "instances": [],
    }

    def spy(runtime: str) -> type:
        class Spy:
            def __init__(self, settings: Settings) -> None:
                seen[runtime].append(settings)

        return Spy

    monkeypatch.setattr(app_module, "AuditReaderRuntime", spy("audit"))
    monkeypatch.setattr(app_module, "ModelCatalogRuntime", spy("catalogue"))
    monkeypatch.setattr(app_module, "ArtifactRuntime", spy("artifact"))
    monkeypatch.setattr(app_module, "ModelInstanceRuntime", spy("instances"))
    app_module.create_app(settings=_with())
    assert seen == {"audit": [], "catalogue": [], "artifact": [], "instances": []}


def test_each_runtime_is_assembled_from_its_own_reader_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readers = {field: SecretStr(DSNS[variable]) for variable, field, _, _ in READERS}
    seen: dict[str, list[Settings]] = {
        "audit": [],
        "catalogue": [],
        "artifact": [],
        "instances": [],
    }

    def spy(runtime: str) -> type:
        class Spy:
            def __init__(self, settings: Settings) -> None:
                seen[runtime].append(settings)

        return Spy

    monkeypatch.setattr(app_module, "AuditReaderRuntime", spy("audit"))
    monkeypatch.setattr(app_module, "ModelCatalogRuntime", spy("catalogue"))
    monkeypatch.setattr(app_module, "ArtifactRuntime", spy("artifact"))
    monkeypatch.setattr(app_module, "ModelInstanceRuntime", spy("instances"))
    app_module.create_app(settings=_with(**(_object_storage() | readers)))
    assert [settings.audit_reader_database_url for settings in seen["audit"]] == [
        readers["audit_reader_database_url"]
    ]
    assert [settings.model_catalog_reader_database_url for settings in seen["catalogue"]] == [
        readers["model_catalog_reader_database_url"]
    ]
    assert [settings.artifact_reader_database_url for settings in seen["artifact"]] == [
        readers["artifact_reader_database_url"]
    ]
    assert [settings.model_instance_reader_database_url for settings in seen["instances"]] == [
        readers["model_instance_reader_database_url"]
    ]


def test_the_artifact_reader_still_needs_the_paired_object_storage_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose always supplies the object keys; a DSN alone must not assemble reads.

    The Artifact runtime opens object storage for every content read, so the DSN
    is one of two conditions and the empty-default wiring does not weaken that.
    """
    assert "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY" in API_ENV
    assert "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY" in API_ENV
    seen: list[Settings] = []

    class Spy:
        def __init__(self, settings: Settings) -> None:
            seen.append(settings)

    monkeypatch.setattr(app_module, "ArtifactRuntime", Spy)
    dsn = SecretStr(DSNS["SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL"])
    app_module.create_app(settings=_with(artifact_reader_database_url=dsn))
    assert seen == []
    paired = _object_storage() | {"artifact_reader_database_url": dsn}
    app_module.create_app(settings=_with(**paired))
    assert [settings.artifact_reader_database_url for settings in seen] == [dsn]


@pytest.mark.parametrize("variable,field,path,code", READERS, ids=IDS)
def test_the_example_env_declares_the_reader_entry_blank(
    variable: str, field: str, path: str, code: str
) -> None:
    assert f"{variable}=" in EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "variable",
    [
        "SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD",
        "SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD",
        "SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD",
    ],
)
def test_the_example_env_names_the_optional_role_passwords(variable: str) -> None:
    """The optional identities follow the fixed roles' ``*_ROLE_PASSWORD`` style."""
    lines = EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith(f"{variable}=") for line in lines)


@pytest.mark.skipif(
    not _compose_available(), reason="docker compose is required to interpolate the file"
)
def test_compose_renders_each_reader_blank_then_configured(tmp_path: Path) -> None:
    """The empty default renders, and a configured DSN reaches the api service.

    This is the render-level counterpart of the static assertions above: a ``:?``
    reference would make the first render exit non-zero, and a dropped line would
    remove the key from the api environment in both renders.
    """
    unconfigured = _render({}, tmp_path)
    for variable, *_ in READERS:
        assert unconfigured[variable] == ""

    configured = _render(DSNS, tmp_path)
    for variable, value in DSNS.items():
        assert configured[variable] == value
