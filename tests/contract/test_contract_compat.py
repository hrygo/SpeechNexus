"""Behavioral checks for the OpenAI public-contract compatibility gate."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

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
    assert not issues  # Stronger output guarantees are backward compatible.


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
    base: dict[str, Any] = {
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


def test_compat_gate_resolves_equivalent_schema_refs() -> None:
    module = _load_compat_module()
    compare_openapi = cast(Any, module._compare_openapi)
    base: dict[str, Any] = {
        "paths": {
            "/v1/audio/speech": {
                "post": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "response_format": {
                                                "type": "string",
                                                "enum": ["mp3", "wav", "pcm"],
                                                "default": "mp3",
                                            }
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {"schemas": {}},
    }
    current = copy.deepcopy(base)
    current["components"]["schemas"]["AudioFormat"] = {
        "type": "string",
        "enum": ["mp3", "wav", "pcm"],
    }
    current["paths"]["/v1/audio/speech"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["properties"]["response_format"] = {
        "$ref": "#/components/schemas/AudioFormat",
        "default": "mp3",
    }

    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert not issues

    current["components"]["schemas"]["AudioFormat"]["enum"].append("flac")
    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert any("enum values added" in issue for issue in issues)


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

    current["components"]["schemas"]["ModelList"]["required"].remove("object")
    issues = compare_openapi(base, current, "speech-openapi.yaml")
    assert any("response required guarantees removed: ['object']" in issue for issue in issues)


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
    assert any("/properties/input: property removed" in issue for issue in issues)


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
    assert any("/properties/session/properties/type: property removed" in issue for issue in issues)


@pytest.mark.parametrize("location", ["header", "query"])
def test_new_required_parameter_is_breaking(location: str) -> None:
    module = _load_compat_module()
    base: dict[str, Any] = {"paths": {"/v1/audio/speech": {"post": {"responses": {}}}}}
    current = copy.deepcopy(base)
    current["paths"]["/v1/audio/speech"]["post"]["parameters"] = [
        {"name": "required-new", "in": location, "required": True, "schema": {"type": "string"}}
    ]
    assert module._compare_openapi(base, current, "speech-openapi.yaml")


def test_new_required_request_body_is_breaking() -> None:
    module = _load_compat_module()
    base: dict[str, Any] = {"paths": {"/v1/audio/speech": {"post": {"responses": {}}}}}
    current = copy.deepcopy(base)
    current["paths"]["/v1/audio/speech"]["post"]["requestBody"] = {"required": True, "content": {}}
    assert module._compare_openapi(base, current, "speech-openapi.yaml")


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("maxLength", 1),
        ("minLength", 1),
        ("minimum", 1),
        ("maximum", 1),
        ("maxItems", 1),
        ("minItems", 1),
        ("exclusiveMinimum", 1),
        ("exclusiveMaximum", 1),
        ("pattern", "^x$"),
        ("multipleOf", 2),
    ],
)
def test_new_request_bounds_and_lost_response_bounds_are_breaking(keyword: str, value: Any) -> None:
    module = _load_compat_module()
    issues: list[str] = []
    module._compare_schema({}, {keyword: value}, "request", issues)
    assert issues
    issues = []
    module._compare_schema({keyword: value}, {}, "response", issues, direction="response")
    assert issues
    issues = []
    module._compare_schema({keyword: value}, {}, "request", issues)
    assert not issues


@pytest.mark.parametrize(
    "direction,base,current",
    [
        ("request", {"enum": ["a", "b"]}, {"enum": ["a"]}),
        ("response", {"enum": ["a"]}, {"enum": ["a", "b"]}),
        ("response", {"required": ["text"]}, {"required": []}),
        ("request", {"type": ["string", "null"]}, {"type": "string"}),
        ("response", {"type": "string"}, {"type": ["string", "null"]}),
        ("request", {}, {"allOf": [{"maxLength": 1}]}),
    ],
)
def test_comparison_respects_input_and_output_direction(
    direction: str,
    base: dict[str, Any],
    current: dict[str, Any],
) -> None:
    module = _load_compat_module()
    issues: list[str] = []
    module._compare_schema(base, current, "schema", issues, direction=direction)
    assert issues


def test_ref_siblings_preserve_intersection_constraints() -> None:
    module = _load_compat_module()
    base: dict[str, Any] = {
        "paths": {
            "/v1/audio/speech": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Text", "maxLength": 10}
                            }
                        }
                    },
                    "responses": {},
                }
            }
        },
        "components": {"schemas": {"Text": {"type": "string", "maxLength": 20}}},
    }
    current = copy.deepcopy(base)
    current["components"]["schemas"]["Text"]["maxLength"] = 5
    assert module._compare_openapi(base, current, "speech-openapi.yaml")


def test_response_realtime_guarantee_cannot_disappear() -> None:
    module = _load_compat_module()
    base: dict[str, Any] = {
        "oneOf": [{"$ref": "#/$defs/TranscriptionCompleted"}],
        "$defs": {
            "TranscriptionCompleted": {
                "type": "object",
                "required": ["type", "transcript"],
                "properties": {
                    "type": {"const": "conversation.item.input_audio_transcription.completed"},
                    "transcript": {"type": "string"},
                },
            }
        },
    }
    current = copy.deepcopy(base)
    current["$defs"]["TranscriptionCompleted"]["required"].remove("transcript")
    assert module._compare_realtime_events_schema(base, current, "realtime-events.schema.json")


@pytest.mark.parametrize("mutation", ["removed", "required", "type"])
def test_response_header_guarantees_are_compared(mutation: str) -> None:
    module = _load_compat_module()
    base: dict[str, Any] = {
        "paths": {
            "/v1/audio/speech": {
                "post": {
                    "responses": {
                        "200": {
                            "headers": {
                                "X-Receipt": {"required": True, "schema": {"type": "string"}}
                            },
                            "content": {},
                        }
                    }
                }
            }
        }
    }
    current = copy.deepcopy(base)
    headers = current["paths"]["/v1/audio/speech"]["post"]["responses"]["200"]["headers"]
    if mutation == "removed":
        headers.clear()
    elif mutation == "required":
        headers["X-Receipt"]["required"] = False
    else:
        headers["X-Receipt"]["schema"]["type"] = "integer"
    assert module._compare_openapi(base, current, "speech-openapi.yaml")


def _openapi_with_schema(schema: dict[str, Any], direction: str) -> dict[str, Any]:
    content = {"application/json": {"schema": schema}}
    operation: dict[str, Any] = {"responses": {}}
    if direction == "request":
        operation["requestBody"] = {"content": content}
    else:
        operation["responses"]["200"] = {"content": content}
    return {"paths": {"/v1/audio/speech": {"post": operation}}}


@pytest.mark.parametrize("direction", ["request", "response"])
def test_null_const_cannot_silently_remove_accepted_values(direction: str) -> None:
    module = _load_compat_module()
    unconstrained = {"type": ["string", "null"]}
    constrained = {**unconstrained, "const": None}
    base, current = (
        (unconstrained, constrained) if direction == "request" else (constrained, unconstrained)
    )
    assert Draft202012Validator(unconstrained).is_valid("previously accepted")
    assert not Draft202012Validator(constrained).is_valid("previously accepted")
    assert module._compare_openapi(
        _openapi_with_schema(base, direction),
        _openapi_with_schema(current, direction),
        "speech-openapi.yaml",
    )


@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize("equivalent_enum", [False, True])
def test_null_const_equivalence_and_safe_relaxations(direction: str, equivalent_enum: bool) -> None:
    module = _load_compat_module()
    constrained = {"type": ["string", "null"], "const": None}
    other: dict[str, Any] = {"type": ["string", "null"]}
    if equivalent_enum:
        other["enum"] = [None]
    base, current = (constrained, other) if direction == "request" else (other, constrained)
    assert not module._compare_openapi(
        _openapi_with_schema(base, direction),
        _openapi_with_schema(current, direction),
        "speech-openapi.yaml",
    )


@pytest.mark.parametrize(
    "direction,additional,new_property,payload",
    [
        ("response", False, {"type": "integer"}, {"extra": 1}),
        ("response", {"type": "string"}, {"type": "integer"}, {"extra": 1}),
        ("request", True, {"type": "integer"}, {"extra": "previously accepted"}),
        ("request", {"type": "integer"}, {"maximum": 0}, {"extra": 1}),
    ],
)
def test_new_properties_respect_previous_additional_property_domain(
    direction: str, additional: Any, new_property: dict[str, Any], payload: dict[str, Any]
) -> None:
    module = _load_compat_module()
    base = {"type": "object", "additionalProperties": additional, "properties": {}}
    current = copy.deepcopy(base)
    current["properties"]["extra"] = new_property
    accepted, constrained = (base, current) if direction == "request" else (current, base)
    assert Draft202012Validator(accepted).is_valid(payload)
    assert not Draft202012Validator(constrained).is_valid(payload)
    assert module._compare_openapi(
        _openapi_with_schema(base, direction),
        _openapi_with_schema(current, direction),
        "speech-openapi.yaml",
    )


@pytest.mark.parametrize(
    "direction,additional,new_property",
    [
        ("request", False, {"type": "integer"}),
        ("response", True, {"type": "integer"}),
        ("request", {"type": "string", "minLength": 1}, {"type": "string"}),
        ("response", {"type": "string"}, {"type": "string", "minLength": 1}),
    ],
)
def test_new_properties_allow_compatible_additional_property_domains(
    direction: str, additional: Any, new_property: dict[str, Any]
) -> None:
    module = _load_compat_module()
    base = {"type": "object", "additionalProperties": additional, "properties": {}}
    current = copy.deepcopy(base)
    current["properties"]["extra"] = new_property
    assert not module._compare_openapi(
        _openapi_with_schema(base, direction),
        _openapi_with_schema(current, direction),
        "speech-openapi.yaml",
    )


@pytest.mark.parametrize("direction", ["request", "response"])
def test_oneof_overlap_changes_require_explicit_compatibility_proof(direction: str) -> None:
    module = _load_compat_module()
    disjoint = {
        "oneOf": [
            {"type": "integer", "minimum": 0, "maximum": 9},
            {"type": "integer", "minimum": 10, "maximum": 19},
        ]
    }
    overlapping = copy.deepcopy(disjoint)
    overlapping["oneOf"][0]["maximum"] = 19
    assert Draft202012Validator(disjoint).is_valid(10)
    assert not Draft202012Validator(overlapping).is_valid(10)
    base, current = (disjoint, overlapping) if direction == "request" else (overlapping, disjoint)
    assert module._compare_openapi(
        _openapi_with_schema(base, direction),
        _openapi_with_schema(current, direction),
        "speech-openapi.yaml",
    )


def test_oneof_equivalent_refs_annotations_and_optional_nexus_remain_compatible() -> None:
    module = _load_compat_module()
    variant: dict[str, Any] = {
        "type": "object",
        "required": ["kind"],
        "properties": {"kind": {"const": "example"}, "nexus": {"type": "object"}},
    }
    base = _openapi_with_schema({"oneOf": [{"$ref": "#/components/schemas/Variant"}]}, "request")
    base["components"] = {"schemas": {"Variant": variant}}
    current = copy.deepcopy(base)
    inline = copy.deepcopy(variant)
    inline["description"] = "An annotation does not change accepted values."
    inline["properties"]["nexus"] = {"type": "object", "required": ["new_extension"]}
    current["paths"]["/v1/audio/speech"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["oneOf"] = [inline]
    assert not module._compare_openapi(base, current, "speech-openapi.yaml")


@pytest.mark.parametrize("keyword", ["const", "enum"])
def test_oneof_data_values_are_not_mistaken_for_schema_annotations(keyword: str) -> None:
    module = _load_compat_module()
    old_value = {"description": "old", "nexus": {"value": 1}}
    new_value = {"description": "new", "nexus": {"value": 2}}
    base = {"oneOf": [{keyword: [old_value] if keyword == "enum" else old_value}]}
    current = {"oneOf": [{keyword: [new_value] if keyword == "enum" else new_value}]}
    assert Draft202012Validator(base).is_valid(old_value)
    assert not Draft202012Validator(current).is_valid(old_value)
    assert module._compare_openapi(
        _openapi_with_schema(base, "request"),
        _openapi_with_schema(current, "request"),
        "speech-openapi.yaml",
    )
