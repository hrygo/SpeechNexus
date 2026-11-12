"""Only exact approved parameter removals and response replacements pass the gate."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/admin-openapi.yaml"
AUDIT_PATH = "/v1/nexus/audit-events"
AUDIT_POINTER = f"{SOURCE}:paths/{AUDIT_PATH}/get"
CAPACITY_PATH = "/v1/nexus/admin/capacity-manifests/{manifest_id}"
CAPACITY_POINTER = f"{SOURCE}:paths/{CAPACITY_PATH}/get/responses/200"
BASE_VERSION = "0.1.0.dev7"
CURRENT_VERSION = "0.1.0.dev8"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "http_change_cli_narrowings", ROOT / "scripts/dev-env/check-http-contract-changes.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def audit_document(*, version: str, workspace: bool = True) -> dict[str, Any]:
    parameters: list[dict[str, Any]] = []
    if workspace:
        parameters.append(
            {
                "name": "workspace_id",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            }
        )
    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": version},
        "paths": {
            AUDIT_PATH: {
                "get": {
                    "operationId": "listAuditEvents",
                    "parameters": parameters,
                    "responses": {"200": {"description": "Synthetic"}},
                }
            }
        },
    }


def capacity_document(
    *,
    version: str,
    schema_ref: str = "./capacity-manifest.schema.json",
    description: str = "Capacity manifest.",
    header_type: str = "string",
) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": version},
        "paths": {
            CAPACITY_PATH: {
                "get": {
                    "operationId": "getCapacityManifest",
                    "responses": {
                        "200": {
                            "description": description,
                            "headers": {"X-Request-ID": {"schema": {"type": header_type}}},
                            "content": {"application/json": {"schema": {"$ref": schema_ref}}},
                        }
                    },
                }
            }
        },
    }


def apply_parameters(
    base: dict[str, Any], current: dict[str, Any], findings: list[str]
) -> tuple[list[str], list[str]]:
    return cast(
        tuple[list[str], list[str]],
        module()._apply_approved_parameter_removals(
            SOURCE,
            base,
            current,
            base["info"]["version"],
            current["info"]["version"],
            findings,
        ),
    )


def capacity_registry() -> dict[tuple[str, str, str, str, str, str], dict[str, str]]:
    return {
        (SOURCE, CAPACITY_PATH, "get", "200", BASE_VERSION, CURRENT_VERSION): {
            "issue": "#605",
            "reason": (
                "The admin surface exposes only the recorded evidence identity and "
                "release-evidence locator; the document body stays authoritative in "
                "the release evidence chain."
            ),
            "from_schema": "./capacity-manifest.schema.json",
            "to_schema": "#/components/schemas/CapacityManifestRecord",
        }
    }


def apply_response(
    monkeypatch: pytest.MonkeyPatch,
    base: dict[str, Any],
    current: dict[str, Any],
    findings: list[str],
) -> tuple[list[str], list[str]]:
    loaded = module()
    monkeypatch.setattr(loaded, "_APPROVED_RESPONSE_REPLACEMENTS", capacity_registry())
    return cast(
        tuple[list[str], list[str]],
        loaded._apply_approved_response_replacements(
            SOURCE,
            base,
            current,
            base["info"]["version"],
            current["info"]["version"],
            findings,
        ),
    )


def test_the_parameter_and_response_registries_are_frozen() -> None:
    loaded = module()
    assert loaded._APPROVED_PARAMETER_REMOVALS == {
        (SOURCE, AUDIT_PATH, "get", BASE_VERSION, CURRENT_VERSION, "query", "workspace_id"): {
            "issue": "#605",
            "reason": (
                "The audit table stores only model binding lifecycle events and has "
                "no workspace column; the declared filter could never match a row."
            ),
        }
    }
    assert loaded._APPROVED_RESPONSE_REPLACEMENTS == {
        (SOURCE, CAPACITY_PATH, "get", "200", BASE_VERSION, CURRENT_VERSION): {
            "issue": "#605",
            "reason": (
                "The admin surface exposes only the recorded evidence identity and "
                "release-evidence locator; the document body stays authoritative in "
                "the release evidence chain."
            ),
            "from_schema": "./capacity-manifest.schema.json",
            "to_schema": "#/components/schemas/CapacityManifestRecord",
        },
        (
            SOURCE,
            "/v1/nexus/admin/qualification-reports/{report_id}",
            "get",
            "200",
            BASE_VERSION,
            CURRENT_VERSION,
        ): {
            "issue": "#605",
            "reason": (
                "The admin surface exposes only the recorded evidence identity and "
                "release-evidence locator; the document body stays authoritative in "
                "the release evidence chain."
            ),
            "from_schema": "./qualification-report.schema.json",
            "to_schema": "#/components/schemas/QualificationReportRecord",
        },
    }


def test_the_exact_parameter_removal_is_filtered() -> None:
    base = audit_document(version=BASE_VERSION)
    current = audit_document(version=CURRENT_VERSION, workspace=False)
    findings = [f"{AUDIT_POINTER}: parameter removed: query workspace_id"]

    remaining, approved = apply_parameters(base, current, findings)

    assert remaining == []
    assert len(approved) == 1
    assert approved[0].startswith(f"{AUDIT_POINTER}: parameter removed: query workspace_id")
    assert "#605" in approved[0]


def test_a_parameter_removal_version_mismatch_leaves_the_finding() -> None:
    base = audit_document(version="0.1.0.dev6")
    current = audit_document(version=CURRENT_VERSION, workspace=False)
    findings = [f"{AUDIT_POINTER}: parameter removed: query workspace_id"]

    remaining, approved = apply_parameters(base, current, findings)

    assert remaining == findings
    assert approved == []


def test_a_parameter_removal_entry_does_not_cover_another_parameter() -> None:
    base = audit_document(version=BASE_VERSION)
    current = audit_document(version=CURRENT_VERSION, workspace=False)
    findings = [f"{AUDIT_POINTER}: parameter removed: query workspace"]

    remaining, approved = apply_parameters(base, current, findings)

    assert approved == []
    assert findings[0] in remaining
    assert any("approved parameter removal entry does not match" in item for item in remaining)


def test_a_parameter_removal_entry_rejects_when_the_parameter_still_exists() -> None:
    base = audit_document(version=BASE_VERSION)
    current = audit_document(version=CURRENT_VERSION, workspace=True)
    findings = [f"{AUDIT_POINTER}: parameter removed: query workspace_id"]

    remaining, approved = apply_parameters(base, current, findings)

    assert approved == []
    assert any("current contract still declares this parameter" in item for item in remaining)


def test_the_exact_response_replacement_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    base = capacity_document(version=BASE_VERSION)
    current = capacity_document(
        version=CURRENT_VERSION,
        schema_ref="#/components/schemas/CapacityManifestRecord",
        description="Recorded capacity manifest reference.",
    )
    findings = [
        f"{CAPACITY_POINTER}/application/json: response required guarantees removed: "
        "['topology_ref']",
        f"{CAPACITY_POINTER}/application/json/properties/topology_ref: property removed",
    ]

    remaining, approved = apply_response(monkeypatch, base, current, findings)

    assert remaining == []
    assert len(approved) == 1
    assert approved[0].startswith(f"{CAPACITY_POINTER}: response schema replaced")
    assert "#605" in approved[0]


def test_a_response_replacement_version_mismatch_leaves_the_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = capacity_document(version="0.1.0.dev6")
    current = capacity_document(
        version=CURRENT_VERSION, schema_ref="#/components/schemas/CapacityManifestRecord"
    )
    findings = [f"{CAPACITY_POINTER}/application/json: response required guarantees removed: []"]

    remaining, approved = apply_response(monkeypatch, base, current, findings)

    assert remaining == findings
    assert approved == []


def test_a_response_replacement_rejects_a_different_current_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = capacity_document(version=BASE_VERSION)
    current = capacity_document(
        version=CURRENT_VERSION, schema_ref="#/components/schemas/OtherRecord"
    )
    findings = [f"{CAPACITY_POINTER}/application/json: response required guarantees removed: []"]

    remaining, approved = apply_response(monkeypatch, base, current, findings)

    assert approved == []
    assert findings[0] in remaining
    assert any("current schema reference is not" in item for item in remaining)


def test_a_response_replacement_keeps_findings_outside_the_exact_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = capacity_document(version=BASE_VERSION)
    current = capacity_document(
        version=CURRENT_VERSION, schema_ref="#/components/schemas/CapacityManifestRecord"
    )
    response_finding = f"{CAPACITY_POINTER}/application/json/properties/id: property removed"
    request_finding = (
        f"{SOURCE}:paths/{CAPACITY_PATH}/get/requestBody/application/json: new required fields: []"
    )

    remaining, approved = apply_response(
        monkeypatch, base, current, [response_finding, request_finding]
    )

    assert remaining == [request_finding]
    assert len(approved) == 1


def test_a_response_replacement_rejects_changed_response_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = capacity_document(version=BASE_VERSION)
    current = capacity_document(
        version=CURRENT_VERSION,
        schema_ref="#/components/schemas/CapacityManifestRecord",
        header_type="integer",
    )
    findings = [f"{CAPACITY_POINTER}/application/json: response required guarantees removed: []"]

    remaining, approved = apply_response(monkeypatch, base, current, findings)

    assert approved == []
    assert findings[0] in remaining
    assert any("response metadata other than the schema" in item for item in remaining)


def test_a_response_replacement_does_not_cover_a_different_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = capacity_document(version=BASE_VERSION)
    current = copy.deepcopy(base)
    current["info"]["version"] = CURRENT_VERSION
    current["paths"][CAPACITY_PATH]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = {"$ref": "#/components/schemas/CapacityManifestRecord"}
    response_finding = (
        f"{CAPACITY_POINTER}/application/json: response required guarantees removed: []"
    )
    status_finding = f"{SOURCE}:paths/{CAPACITY_PATH}/get/responses/404: response status removed"

    remaining, approved = apply_response(
        monkeypatch, base, current, [response_finding, status_finding]
    )

    assert remaining == [status_finding]
    assert len(approved) == 1
