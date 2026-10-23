"""Behavioral checks for the OpenAI public-contract compatibility gate."""

from __future__ import annotations

import copy
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
    base: dict[str, Any] = {
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
    current: dict[str, Any] = {
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


def test_compat_gate_rejects_changed_public_defaults_and_formats() -> None:
    module = _load_compat_module()
    compare_json_schema = cast(Any, module._compare_json_schema)
    base: dict[str, Any] = {
        "type": "object",
        "properties": {
            "response_format": {"type": "string", "default": "json"},
            "created_at": {"type": "string", "format": "date-time"},
        },
    }
    current: dict[str, Any] = {
        "type": "object",
        "properties": {
            "response_format": {"type": "string", "default": "text"},
            "created_at": {"type": "string", "format": "date"},
        },
    }

    issues = compare_json_schema(base, current, "speech-openapi.yaml")
    assert any("default changed" in issue for issue in issues)
    assert any("format changed" in issue for issue in issues)

    current = copy.deepcopy(base)
    del current["properties"]["response_format"]["default"]
    issues = compare_json_schema(base, current, "speech-openapi.yaml")
    assert any("default presence changed" in issue for issue in issues)


def test_openai_compat_gate_ignores_unreferenced_nexus_extensions() -> None:
    module = _load_compat_module()
    compare_openapi = cast(Any, module._compare_openapi)
    base: dict[str, Any] = {
        "paths": {
            "/v1/models": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ModelList"}
                                }
                            }
                        }
                    }
                }
            },
            "/v1/nexus/jobs": {
                "post": {
                    "responses": {
                        "202": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Job"}}
                            }
                        }
                    }
                }
            },
        },
        "components": {
            "schemas": {
                "ModelList": {
                    "type": "object",
                    "required": ["object"],
                    "properties": {"object": {"const": "list"}},
                },
                "Job": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {"kind": {"type": "string"}},
                },
            }
        },
    }
    current = copy.deepcopy(base)
    current["paths"].pop("/v1/nexus/jobs")
    current["components"]["schemas"]["Job"]["required"].append("result")

    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert not issues

    current["components"]["schemas"]["ModelList"]["required"].append("data")
    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert any(
        "components/schemas/ModelList: new required fields: ['data']" in issue for issue in issues
    )


def test_openai_compat_gate_ignores_optional_nexus_nested_schemas() -> None:
    module = _load_compat_module()
    compare_openapi = cast(Any, module._compare_openapi)
    base: dict[str, Any] = {
        "paths": {
            "/v1/audio/speech": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SpeechRequest"}
                            }
                        },
                    },
                    "responses": {},
                }
            }
        },
        "components": {
            "schemas": {
                "SpeechRequest": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string"},
                        "nexus": {"$ref": "#/components/schemas/NexusOptions"},
                    },
                },
                "NexusOptions": {
                    "type": "object",
                    "properties": {"profile_revision": {"type": "string"}},
                },
            }
        },
    }
    current = copy.deepcopy(base)
    current["components"]["schemas"]["NexusOptions"] = {
        "type": "object",
        "required": ["new_nexus_option"],
        "properties": {"new_nexus_option": {"type": "string"}},
    }
    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert not issues

    del current["components"]["schemas"]["SpeechRequest"]["properties"]["input"]
    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert any(
        "components/schemas/SpeechRequest/properties/input: property removed" in issue
        for issue in issues
    )


def test_realtime_compat_gate_checks_only_standard_event_definitions() -> None:
    module = _load_compat_module()
    compare_realtime = cast(Any, module._compare_realtime_events_schema)
    base: dict[str, Any] = {
        "oneOf": [
            {"$ref": "#/$defs/SessionUpdate"},
            {"$ref": "#/$defs/NexusOperationDone"},
        ],
        "$defs": {
            "SessionUpdate": {
                "type": "object",
                "required": ["type"],
                "properties": {"type": {"const": "session.update"}},
            },
            "NexusOperationDone": {
                "allOf": [
                    {"type": "object", "properties": {"type": {"const": "nexus.operation.done"}}}
                ]
            },
        },
    }
    current = copy.deepcopy(base)
    current["$defs"]["NexusOperationDone"]["allOf"].append(
        {"type": "object", "required": ["receipt"]}
    )

    issues = compare_realtime(current=current, base=base, path="realtime-events.schema.json")
    assert not issues

    current["oneOf"] = [{"$ref": "#/$defs/NexusOperationDone"}]
    issues = compare_realtime(current=current, base=base, path="realtime-events.schema.json")
    assert any("standard event removed from public union" in issue for issue in issues)

    current = copy.deepcopy(base)
    del current["$defs"]["SessionUpdate"]
    issues = compare_realtime(current=current, base=base, path="realtime-events.schema.json")
    assert any("standard event definition removed" in issue for issue in issues)


def test_realtime_compat_gate_ignores_optional_nexus_nested_definitions() -> None:
    module = _load_compat_module()
    compare_realtime = cast(Any, module._compare_realtime_events_schema)
    base: dict[str, Any] = {
        "oneOf": [{"$ref": "#/$defs/SessionUpdate"}],
        "$defs": {
            "SessionUpdate": {
                "type": "object",
                "properties": {
                    "type": {"const": "session.update"},
                    "session": {"$ref": "#/$defs/SessionRequest"},
                },
                "required": ["type", "session"],
            },
            "SessionRequest": {
                "type": "object",
                "properties": {
                    "type": {"const": "transcription"},
                    "nexus": {"$ref": "#/$defs/NexusOptions"},
                },
                "required": ["type"],
            },
            "NexusOptions": {
                "type": "object",
                "properties": {"profile_revision": {"type": "string"}},
            },
        },
    }
    current = copy.deepcopy(base)
    current["$defs"]["NexusOptions"] = {
        "type": "object",
        "required": ["new_nexus_option"],
        "properties": {"new_nexus_option": {"type": "string"}},
    }
    issues = compare_realtime(current=current, base=base, path="realtime-events.schema.json")
    assert not issues

    del current["$defs"]["SessionRequest"]["properties"]["type"]
    issues = compare_realtime(current=current, base=base, path="realtime-events.schema.json")
    assert any(
        "$defs/SessionRequest/properties/type: property removed" in issue for issue in issues
    )
