"""The running Asset surface preserves authoritative schemas and only real paths."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.interfaces.http.asset_contract import (
    asset_http_contract,
    public_asset,
)
from tests.unit.backend.test_health import _settings
from tests.unit.test_assets import READY

ROOT = Path(__file__).resolve().parents[2]
PATHS = {"/v1/nexus/assets", "/v1/nexus/assets/{asset_id}", "/v1/nexus/assets/{asset_id}/content"}


def test_packaged_http_surface_is_closed_and_matches_real_operations() -> None:
    document = asset_http_contract()
    validate(document)
    assert set(document["paths"]) == PATHS
    manifest = json.loads((ROOT / "contracts/bindings.json").read_text())
    assert set(manifest["http_surfaces"]["assets"]["paths"]) == PATHS
    source = yaml.safe_load((ROOT / "contracts/speech-openapi.yaml").read_text())
    actual = create_app(settings=_settings()).openapi()
    assert set(actual["paths"]) == PATHS | {
        "/healthz",
        "/readyz",
        "/v1/nexus/workspaces",
        "/v1/nexus/workspaces/{workspace_id}",
        "/v1/nexus/admin/profiles",
        "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}",
        "/v1/nexus/admin/model-bindings",
        "/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}",
        "/v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision}",
    }
    for path in PATHS:
        assert actual["paths"][path] == document["paths"][path]
        for method, operation in document["paths"][path].items():
            assert operation["operationId"] == source["paths"][path][method]["operationId"]
            assert set(operation["responses"]) == set(source["paths"][path][method]["responses"])

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert "$id" not in node
            if "$ref" in node:
                assert node["$ref"].startswith("#/components/")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    assert files("speechnexus_contracts").joinpath("assets.openapi.json").is_file()


def test_openapi_keeps_conditional_ready_and_private_field_rejections() -> None:
    document = asset_http_contract()
    validator = Draft202012Validator(
        {"$ref": "#/components/schemas/Asset", "components": document["components"]},
        format_checker=FormatChecker(),
    )
    wire = public_asset(READY)
    assert validator.is_valid(wire)
    for changes in (
        {"bytes": 0},
        {"purpose": "asr_input"},
        {"duration_ms": 10},
        {"object_key": "private/not-public"},
        {"storage_version_id": "private-version"},
    ):
        assert not validator.is_valid(wire | changes)


def test_openapi_callers_cannot_mutate_the_authoritative_cache() -> None:
    changed = create_app(settings=_settings()).openapi()
    changed["components"]["schemas"]["Asset"]["allOf"] = []
    untouched = create_app(settings=_settings()).openapi()
    assert untouched["components"]["schemas"]["Asset"]["allOf"]
    assert asset_http_contract()["components"]["schemas"]["Asset"]["allOf"]
