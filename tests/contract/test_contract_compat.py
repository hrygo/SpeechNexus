"""Behavioral checks for the OpenAI public-contract compatibility gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "dev-env" / "check-openai-contract-compat.py"


def _load_compat_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("speechnexus_contract_compat", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compat_gate_rejects_removed_public_contract_surface() -> None:
    module = _load_compat_module()
    compare_openapi = cast(Any, module._compare_openapi)

    base: dict[str, Any] = {
        "paths": {
            "/v1/audio/speech": {
                "post": {
                    "parameters": [],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["audio"],
                                        "properties": {"audio": {"type": "string"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
    }
    current: dict[str, Any] = {
        "paths": {
            "/v1/audio/speech": {
                "post": {
                    "parameters": [],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["audio"],
                                        "properties": {},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
    }

    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert any("property removed" in issue for issue in issues)

    current["paths"]["/v1/audio/speech"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["properties"] = {"audio": {"type": "string"}}
    current["paths"]["/v1/audio/speech"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["required"] = ["audio", "metadata"]
    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert any("new required fields" in issue for issue in issues)


def test_compat_gate_rejects_removed_event_type() -> None:
    module = _load_compat_module()
    compare_json_schema = cast(Any, module._compare_json_schema)
    base = {
        "$defs": {
            "Event": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"type": {"const": "session.update"}},
                    },
                    {
                        "type": "object",
                        "properties": {"type": {"const": "error"}},
                    },
                ]
            }
        }
    }
    current = {
        "$defs": {
            "Event": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"type": {"const": "session.update"}},
                    }
                ]
            }
        }
    }

    issues = compare_json_schema(base, current, "realtime-events.schema.json")
    assert any("event types removed" in issue for issue in issues)


def test_compat_gate_covers_shared_public_domains() -> None:
    module = _load_compat_module()
    compare_json_schema = cast(Any, module._compare_json_schema)
    base = {
        "$defs": {
            "ErrorType": {
                "type": "string",
                "enum": ["invalid_request_error", "rate_limit_error", "api_error"],
            }
        }
    }
    current = {
        "$defs": {
            "ErrorType": {
                "type": "string",
                "enum": ["invalid_request_error", "api_error"],
            }
        }
    }

    issues = compare_json_schema(base, current, "shared/errors.schema.json")
    assert any("enum values removed" in issue for issue in issues)
