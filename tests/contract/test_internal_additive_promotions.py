"""The internal additive promotion is one exact, literal shape change."""

from __future__ import annotations

from typing import Any, cast

from tests.contract.test_http_contract_changes import document, kernel

SOURCE = "contracts/internal/execution-control.openapi.yaml"
COORDINATION_SOURCE = "contracts/internal/coordination-transactions.schema.json"
COORDINATION_POINTER = "$defs/create_submit/properties/execution_request/properties/command"
PATH = "/internal/v1/executions/submit"
REFERENCE_TEXT = {
    "oneOf": [
        {"type": "string", "minLength": 1, "maxLength": 10000},
        {"type": "null"},
    ]
}


def parameters(
    *,
    reference_text: object | None = None,
    required: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {"speed": {"type": "integer"}}
    required_names = ["speed"]
    if reference_text is not None:
        properties["reference_text"] = reference_text
        if required:
            required_names.append("reference_text")
    if extra:
        properties.update(extra)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required_names,
        "properties": properties,
    }


def command_union(
    *,
    tts: dict[str, Any],
    voicevalidation: dict[str, Any],
    voiceclone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    variants = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "parameters"],
            "properties": {"kind": {"const": kind}, "parameters": params},
        }
        for kind, params in (
            ("tts", tts),
            ("voiceclone", voiceclone or parameters()),
            ("voicevalidation", voicevalidation),
        )
    ]
    return {"oneOf": variants}


def command_document(
    *,
    tts: dict[str, Any],
    voicevalidation: dict[str, Any],
    voiceclone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = {
        "type": "object",
        "additionalProperties": False,
        "required": ["command"],
        "properties": {
            "command": command_union(
                tts=tts,
                voicevalidation=voicevalidation,
                voiceclone=voiceclone,
            )
        },
    }
    return document(PATH, request=request)


def findings(
    base: dict[str, Any],
    current: dict[str, Any],
    *,
    source: str = SOURCE,
) -> list[str]:
    return cast(
        list[str],
        kernel()._compare_openapi(base, current, source, standard_only=False),
    )


def test_the_allowlist_entry_is_frozen() -> None:
    promotion = {
        "container_path": ("parameters",),
        "property_schema": REFERENCE_TEXT,
    }
    assert kernel()._INTERNAL_ADDITIVE_PROMOTIONS == {  # noqa: SIM300
        (
            SOURCE,
            "paths//internal/v1/executions/submit/post/requestBody/application/json/"
            "properties/command",
            ("tts", "voicevalidation"),
            "reference_text",
            None,
            ("string", "null"),
            "append-optional-property",
        ): promotion,
        (
            COORDINATION_SOURCE,
            COORDINATION_POINTER,
            ("tts", "voicevalidation"),
            "reference_text",
            None,
            ("string", "null"),
            "append-optional-property",
        ): promotion,
    }


def test_appending_the_exact_optional_property_is_accepted() -> None:
    base = command_document(tts=parameters(), voicevalidation=parameters())
    current = command_document(
        tts=parameters(reference_text=REFERENCE_TEXT),
        voicevalidation=parameters(reference_text=REFERENCE_TEXT),
    )

    module = kernel()
    assert module._compare_openapi(base, current, SOURCE, standard_only=False) == []
    notices = module._INTERNAL_ADDITIVE_NOTICES
    assert len(notices) == 1
    assert "'tts', 'voicevalidation'" in notices[0]
    assert "reference_text" in notices[0]


def test_adding_the_property_to_another_variant_is_still_rejected() -> None:
    base = command_document(tts=parameters(), voicevalidation=parameters())
    current = command_document(
        tts=parameters(),
        voicevalidation=parameters(),
        voiceclone=parameters(reference_text=REFERENCE_TEXT),
    )

    assert findings(base, current)


def test_making_the_property_required_is_still_rejected() -> None:
    base = command_document(tts=parameters(), voicevalidation=parameters())
    current = command_document(
        tts=parameters(reference_text=REFERENCE_TEXT, required=True),
        voicevalidation=parameters(reference_text=REFERENCE_TEXT, required=True),
    )

    assert findings(base, current)


def test_another_internal_schema_does_not_inherit_the_promotion() -> None:
    base = command_document(tts=parameters(), voicevalidation=parameters())
    current = command_document(
        tts=parameters(reference_text=REFERENCE_TEXT),
        voicevalidation=parameters(reference_text=REFERENCE_TEXT),
    )

    assert findings(
        base,
        current,
        source="contracts/internal/runtime-control.openapi.yaml",
    )


def test_changing_the_property_type_is_still_rejected() -> None:
    base = command_document(tts=parameters(), voicevalidation=parameters())
    current = command_document(
        tts=parameters(reference_text={"type": "integer"}),
        voicevalidation=parameters(reference_text={"type": "integer"}),
    )

    assert findings(base, current)


def test_another_property_name_does_not_inherit_the_promotion() -> None:
    base = command_document(tts=parameters(), voicevalidation=parameters())
    current = command_document(
        tts=parameters(extra={"other": REFERENCE_TEXT}),
        voicevalidation=parameters(extra={"other": REFERENCE_TEXT}),
    )

    assert findings(base, current)


def test_the_embedded_coordination_command_uses_the_same_promotion() -> None:
    module = kernel()
    base = command_union(tts=parameters(), voicevalidation=parameters())
    current = command_union(
        tts=parameters(reference_text=REFERENCE_TEXT),
        voicevalidation=parameters(reference_text=REFERENCE_TEXT),
    )
    issues: list[str] = []
    module._compare_schema(
        base,
        current,
        f"{COORDINATION_SOURCE}:{COORDINATION_POINTER}",
        issues,
        direction="request",
        base_side=module._SchemaSide({}, COORDINATION_SOURCE, None, {}, False),
        current_side=module._SchemaSide({}, COORDINATION_SOURCE, None, {}, False),
    )

    assert issues == []


def test_another_coordination_operation_does_not_inherit_the_promotion() -> None:
    module = kernel()
    base = command_union(tts=parameters(), voicevalidation=parameters())
    current = command_union(
        tts=parameters(reference_text=REFERENCE_TEXT),
        voicevalidation=parameters(reference_text=REFERENCE_TEXT),
    )
    issues: list[str] = []
    module._compare_schema(
        base,
        current,
        f"{COORDINATION_SOURCE}:$defs/create_job/properties/execution_request/properties/command",
        issues,
        direction="request",
        base_side=module._SchemaSide({}, COORDINATION_SOURCE, None, {}, False),
        current_side=module._SchemaSide({}, COORDINATION_SOURCE, None, {}, False),
    )

    assert issues
