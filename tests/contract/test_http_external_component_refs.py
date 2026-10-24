"""External OpenAPI transport objects keep their actual component namespaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openapi_spec_validator import validate

from tests.contract.test_binding_entrypoints import generator

ROOT = Path(__file__).resolve().parents[2]


def test_external_parameter_and_header_with_same_name_keep_distinct_namespaces() -> None:
    config = json.loads((ROOT / "contracts/bindings.json").read_text())["http_surfaces"][
        "binding_defaults"
    ]
    result = generator().bundle_http_surface(config)
    validate(result)
    components = result["components"]
    parameters = {k for k in components["parameters"] if k.endswith("_XRequestId")}
    headers = {k for k in components["headers"] if k.endswith("_XRequestId")}
    assert len(parameters) == len(headers) == 1 and parameters == headers
    post = result["paths"]["/v1/nexus/admin/model-bindings"]["post"]
    name = next(iter(parameters))
    assert {"$ref": f"#/components/parameters/{name}"} in post["parameters"]
    assert post["responses"]["201"]["headers"]["X-Request-ID"] == {
        "$ref": f"#/components/headers/{name}"
    }
    assert post["responses"]["503"]["$ref"].startswith("#/components/responses/")
    assert components["securitySchemes"]["SharedServiceKey"]["$ref"].startswith(
        "#/components/securitySchemes/"
    )


@pytest.mark.parametrize(
    "values",
    [({"type": "string"}, {"type": "integer"}), ({"const": True}, {"const": 1})],
    ids=["different-types", "boolean-and-number"],
)
def test_external_same_namespace_name_collision_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    module = generator()
    monkeypatch.setattr(module, "CONTRACTS", tmp_path)
    external = {
        "$defs": {
            "A": {"properties": {"value": values[0]}},
            "B": {"properties": {"value": values[1]}},
        }
    }
    (tmp_path / "external.schema.json").write_text(json.dumps(external))
    references = [
        {"$ref": f"external.schema.json#/$defs/{name}/properties/value"} for name in ("A", "B")
    ]
    source = {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic collision fixture", "version": "1"},
        "paths": {
            "/synthetic": {
                "get": {
                    "operationId": "getSynthetic",
                    "responses": {
                        "200": {
                            "description": "Synthetic value",
                            "content": {"application/json": {"schema": {"oneOf": references}}},
                        }
                    },
                }
            }
        },
    }
    (tmp_path / "api.openapi.yaml").write_text(json.dumps(source))
    with pytest.raises(ValueError, match="Conflicting HTTP surface component"):
        module.bundle_http_surface({"source": "api.openapi.yaml", "paths": ["/synthetic"]})
