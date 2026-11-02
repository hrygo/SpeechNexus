"""Falsifiable request-only proof controls; validators supply acceptance witnesses."""

from __future__ import annotations

import copy
import itertools
from typing import Any, cast

import pytest
import yaml
from jsonschema import Draft7Validator, Draft202012Validator
from openapi_spec_validator import validate

from tests.contract.test_http_contract_changes import ROOT, document, kernel


def tagged() -> dict[str, Any]:
    return {
        "oneOf": [
            {
                "allOf": [
                    {"type": "object", "properties": {"mode": {"enum": ["read", "write"]}}},
                    {
                        "type": "object",
                        "required": ["mode", "payload"],
                        "properties": {
                            "mode": {"const": tag},
                            "payload": {"type": "string"},
                        },
                    },
                ]
            }
            for tag in ("read", "write")
        ],
        "unevaluatedProperties": False,
    }


def expanded(old: dict[str, Any]) -> dict[str, Any]:
    new = copy.deepcopy(old)
    new["oneOf"][1]["allOf"][1]["properties"]["selection"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["revision"],
        "properties": {"revision": {"type": "integer", "minimum": 1}},
    }
    return new


def findings(old: Any, new: Any, *, direction: str = "request") -> list[str]:
    args = {direction: old}, {direction: new}
    return cast(
        list[str],
        kernel()._compare_openapi(
            document("/synthetic/actions", **args[0]),
            document("/synthetic/actions", **args[1]),
            "contracts/synthetic.yaml",
            standard_only=False,
        ),
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_closed_tagged_requests_allow_optional_declarations_and_branch_reordering(
    reverse: bool,
) -> None:
    old = tagged()
    new = expanded(old)
    if reverse:
        new["oneOf"].reverse()
    for mode in ("read", "write"):
        value: dict[str, Any] = {"mode": mode, "payload": "synthetic"}
        assert Draft202012Validator(old).is_valid(value)
        assert Draft202012Validator(new).is_valid(value)
    value = {"mode": "write", "payload": "synthetic", "selection": {"revision": 1}}
    assert not Draft202012Validator(old).is_valid(value)
    assert Draft202012Validator(new).is_valid(value)
    assert findings(old, new) == []


@pytest.mark.parametrize("fault", ["remove", "rename", "overlap", "optional", "duplicate"])
def test_missing_changed_overlapping_or_optional_tags_remain_rejected(fault: str) -> None:
    old = tagged()
    new = expanded(old)
    if fault == "remove":
        new["oneOf"].pop(0)
    elif fault == "rename":
        new["oneOf"][0]["allOf"][1]["properties"]["mode"]["const"] = "other"
    elif fault == "overlap":
        new["oneOf"][0]["allOf"][1]["properties"]["mode"]["const"] = "write"
    elif fault == "duplicate":
        new["oneOf"].append(copy.deepcopy(new["oneOf"][0]))
    else:
        for schema in (old, new):
            for branch in schema["oneOf"]:
                branch["allOf"][1]["required"].remove("mode")
    assert findings(old, new)


@pytest.mark.parametrize("where", ["parent", "branch"])
@pytest.mark.parametrize(
    "constraint",
    [
        {"patternProperties": {"^selection$": {"type": "string"}}},
        {"pattern": "^unsafe$"},
        {"additionalProperties": {"type": "string"}},
        {"additionalProperties": False},
        {"unevaluatedProperties": False},
        {"allOf": [{"properties": {"selection": True}}]},
        {"propertyNames": {"maxLength": 3}},
        {"dependentRequired": {"payload": ["selection"]}},
        {"dependentSchemas": {"payload": {"required": ["selection"]}}},
        {"if": {"required": ["payload"]}, "then": {"required": ["selection"]}},
        {"not": {"required": ["selection"]}},
        {"minProperties": 3},
        {"maxProperties": 2},
        {"unevaluatedItems": False},
        {"$dynamicRef": "#unknown"},
        {"unknownValidationKeyword": True},
    ],
)
def test_unsupported_object_constraints_fail_closed_even_when_unchanged(
    where: str, constraint: dict[str, Any]
) -> None:
    old = tagged()
    if where == "parent" and "unevaluatedProperties" in constraint:
        constraint = {"unevaluatedProperties": True}
    if where == "parent":
        old.update(constraint)
    else:
        old["oneOf"][1]["allOf"][1].update(constraint)
    new = expanded(old)
    assert findings(old, new)


def test_actual_disjoint_finite_enum_tags_are_supported_without_discriminator_metadata() -> None:
    old = tagged()
    for branch, tag in zip(old["oneOf"], ("read", "write"), strict=True):
        branch["allOf"][1]["properties"]["mode"] = {"enum": [tag]}
    assert findings(old, expanded(old)) == []


@pytest.mark.parametrize("tags", [(True, 1), (False, 0), (1, 1.0)])
def test_nonstring_tags_do_not_get_a_proof_from_python_literal_equality(
    tags: tuple[Any, Any],
) -> None:
    old = tagged()
    for branch, tag in zip(old["oneOf"], tags, strict=True):
        branch["allOf"][0]["properties"]["mode"] = True
        branch["allOf"][1]["properties"]["mode"] = {"const": tag}
    assert findings(old, expanded(old))


@pytest.mark.parametrize("closure", [None, True, {"type": "string"}])
def test_only_proven_false_parent_closure_is_supported(closure: Any) -> None:
    old = tagged()
    if closure is None:
        old.pop("unevaluatedProperties")
    else:
        old["unevaluatedProperties"] = closure
    new = expanded(old)
    assert findings(old, new)


@pytest.mark.parametrize("fault", ["required", "existing-field", "declared-elsewhere", "parent"])
def test_added_constraints_and_redeclarations_do_not_use_optional_extension_proof(
    fault: str,
) -> None:
    old = tagged()
    if fault == "declared-elsewhere":
        old["oneOf"][1]["allOf"][0]["properties"]["selection"] = True
    new = expanded(old)
    if fault == "required":
        new["oneOf"][1]["allOf"][1]["required"].append("selection")
    elif fault == "existing-field":
        new["oneOf"][1]["allOf"][1]["properties"]["payload"]["minLength"] = 1
    elif fault == "parent":
        new["unevaluatedProperties"] = {"type": "string"}
    assert findings(old, new)


def test_open_old_object_has_an_independent_narrowing_witness() -> None:
    old = tagged()
    old.pop("unevaluatedProperties")
    new = expanded(old)
    value = {"mode": "write", "payload": "synthetic", "selection": "previously accepted"}
    assert Draft202012Validator(old).is_valid(value)
    assert not Draft202012Validator(new).is_valid(value)
    assert findings(old, new)


def test_old_input_inclusion_has_bounded_independent_validator_controls() -> None:
    old = tagged()
    new = expanded(old)
    before, after = Draft202012Validator(old), Draft202012Validator(new)
    absent = object()
    for mode, payload, selection, unknown in itertools.product(
        [absent, "read", "write", "other", True],
        [absent, "synthetic", 1],
        [absent, {"revision": 1}, {"revision": 0}, "legacy"],
        [absent, True],
    ):
        value = {
            name: item
            for name, item in zip(
                ["mode", "payload", "selection", "unknown"],
                [mode, payload, selection, unknown],
                strict=True,
            )
            if item is not absent
        }
        if before.is_valid(value):
            assert after.is_valid(value), value
    assert findings(old, new) == []


def test_optional_extension_does_not_relax_response_direction() -> None:
    old = tagged()
    new = expanded(old)
    value = {"mode": "write", "payload": "synthetic", "selection": {"revision": 1}}
    assert Draft202012Validator(new).is_valid(value)
    assert not Draft202012Validator(old).is_valid(value)
    assert findings(old, new, direction="response")


def test_original_openai_policy_keeps_its_conservative_oneof_check() -> None:
    old = tagged()
    assert kernel()._compare_openapi(
        document("/v1/audio/speech", request=old),
        document("/v1/audio/speech", request=expanded(old)),
        "contracts/synthetic.yaml",
    )


@pytest.mark.parametrize("side", ["old", "new", "both"])
@pytest.mark.parametrize(
    "dialect",
    ["http://json-schema.org/draft-07/schema#", "https://example.invalid/unknown-dialect"],
)
def test_document_dialects_cannot_supply_an_unproven_closure(side: str, dialect: str) -> None:
    old = tagged()
    new = expanded(old)
    before = document("/synthetic/actions", request=old)
    after = document("/synthetic/actions", request=new)
    for name, doc in (("old", before), ("new", after)):
        if side in (name, "both"):
            doc["jsonSchemaDialect"] = dialect
    if dialect == "http://json-schema.org/draft-07/schema#" and side == "both":
        # Valid OAS documents, but Draft 7 ignores the alleged closing keyword.
        for doc in (before, after):
            validate(doc)
        witness = {"mode": "write", "payload": "synthetic", "selection": "legacy"}
        assert Draft7Validator(old).is_valid(witness)
        assert not Draft7Validator(new).is_valid(witness)
    assert kernel()._compare_openapi(before, after, "contracts/synthetic.yaml", standard_only=False)


@pytest.mark.parametrize("side", ["old", "new", "both"])
@pytest.mark.parametrize("location", ["inline", "local-target", "local-ancestor"])
@pytest.mark.parametrize(
    "dialect",
    ["http://json-schema.org/draft-07/schema#", "https://example.invalid/unknown-dialect"],
)
def test_schema_dialect_overrides_survive_reference_and_ancestor_boundaries(
    side: str,
    location: str,
    dialect: str,
) -> None:
    docs = []
    for name, schema in (("old", tagged()), ("new", expanded(tagged()))):
        doc = document("/synthetic/actions", request=schema)
        if side in (name, "both"):
            if location == "local-ancestor":
                resource = {
                    "$schema": dialect,
                    "$defs": {"Command": schema},
                }
                pointer = "#/components/schemas/Resource/$defs/Command"
            else:
                schema["$schema"] = dialect
                resource = schema
                pointer = "#/components/schemas/Resource"
            if location != "inline":
                doc["components"] = {"schemas": {"Resource": resource}}
                doc["paths"]["/synthetic/actions"]["post"]["requestBody"]["content"][
                    "application/json"
                ]["schema"] = {"$ref": pointer}
        docs.append(doc)
    assert kernel()._compare_openapi(*docs, "contracts/synthetic.yaml", standard_only=False)


@pytest.mark.parametrize("side", ["old", "new", "both"])
@pytest.mark.parametrize("container", ["json-schema", "openapi"])
@pytest.mark.parametrize(
    "unsupported",
    ["http://json-schema.org/draft-07/schema#", "https://example.invalid/unknown-dialect"],
)
def test_external_reference_uses_target_document_dialect_before_pointer_resolution(
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    container: str,
    unsupported: str,
) -> None:
    module = kernel()
    resources = {}
    for ref, name, schema in (("fixed-base", "old", tagged()), (None, "new", expanded(tagged()))):
        dialect = (
            unsupported
            if side in (name, "both")
            else "https://json-schema.org/draft/2020-12/schema"
        )
        resources[ref] = (
            {"$schema": dialect, "$defs": {"Command": schema}}
            if container == "json-schema"
            else {
                "openapi": "3.1.0",
                "jsonSchemaDialect": dialect,
                "components": {"schemas": {"Command": schema}},
            }
        )

    def load(path: str, ref: str | None) -> dict[str, Any]:
        assert path == "contracts/shared/synthetic.json"
        return resources[ref]

    monkeypatch.setattr(module, "_load_schema_document", load)
    fragment = "/$defs/Command" if container == "json-schema" else "/components/schemas/Command"
    reference_schema = {"$ref": f"shared/synthetic.json#{fragment}"}
    assert module._compare_openapi(
        document("/synthetic/actions", request=reference_schema),
        document("/synthetic/actions", request=reference_schema),
        "contracts/synthetic.yaml",
        base_git_ref="fixed-base",
        standard_only=False,
    )


@pytest.mark.parametrize("dialect", [None, "https://json-schema.org/draft/2020-12/schema"])
def test_external_schema_default_is_not_inherited_from_referring_oas(
    monkeypatch: pytest.MonkeyPatch,
    dialect: str | None,
) -> None:
    module = kernel()

    def load(path: str, ref: str | None) -> dict[str, Any]:
        assert path == "contracts/shared/synthetic.json"
        schema = tagged() if ref else expanded(tagged())
        resource: dict[str, Any] = {"$defs": {"Command": schema}}
        if dialect is not None:
            resource["$schema"] = dialect
        return resource

    monkeypatch.setattr(module, "_load_schema_document", load)
    schema = {"$ref": "shared/synthetic.json#/$defs/Command"}
    issues = module._compare_openapi(
        document("/synthetic/actions", request=schema),
        document("/synthetic/actions", request=schema),
        "contracts/synthetic.yaml",
        base_git_ref="fixed-base",
        standard_only=False,
    )
    assert bool(issues) == (dialect is None)


def test_transitive_reference_keeps_ancestor_dialect_even_if_ref_sibling_claims_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = kernel()

    def load(path: str, ref: str | None) -> dict[str, Any]:
        if path == "contracts/shared/bridge.json":
            return {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$defs": {
                    "Command": {
                        "$ref": "target.json#/$defs/Resource/$defs/Command",
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                    }
                },
            }
        assert path == "contracts/shared/target.json"
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "Resource": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$defs": {"Command": tagged() if ref else expanded(tagged())},
                }
            },
        }

    monkeypatch.setattr(module, "_load_schema_document", load)
    schema = {"$ref": "shared/bridge.json#/$defs/Command"}
    assert module._compare_openapi(
        document("/synthetic/actions", request=schema),
        document("/synthetic/actions", request=schema),
        "contracts/synthetic.yaml",
        base_git_ref="fixed-base",
        standard_only=False,
    )


@pytest.mark.parametrize(
    "dialect",
    [
        "https://spec.openapis.org/oas/3.1/dialect/base",
        "https://json-schema.org/draft/2020-12/schema",
    ],
)
def test_explicit_supported_document_dialects_keep_positive_control(dialect: str) -> None:
    before = document("/synthetic/actions", request=tagged())
    after = document("/synthetic/actions", request=expanded(tagged()))
    for doc in (before, after):
        doc["jsonSchemaDialect"] = dialect
    assert (
        kernel()._compare_openapi(before, after, "contracts/synthetic.yaml", standard_only=False)
        == []
    )


def test_resolved_references_and_annotations_use_the_same_generic_proof() -> None:
    old = tagged()
    new = expanded(old)
    base_doc = document("/synthetic/actions", request=old)
    new_doc = document("/synthetic/actions", request=copy.deepcopy(new))
    new_doc["components"] = {"schemas": {"NewBranch": new["oneOf"][1]}}
    new_doc["paths"]["/synthetic/actions"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["oneOf"][1] = {"$ref": "#/components/schemas/NewBranch", "description": "note"}
    assert (
        kernel()._compare_openapi(
            base_doc, new_doc, "contracts/synthetic.yaml", standard_only=False
        )
        == []
    )


def test_actual_speech_source_old_requests_and_strict_new_pin_acceptance() -> None:
    from speechnexus_contracts import parse

    source = "contracts/speech-openapi.yaml"
    current = yaml.safe_load((ROOT / source).read_text())
    old = copy.deepcopy(current)
    del old["components"]["schemas"]["TtsJobCreate"]["allOf"][1]["properties"]["model_binding"]
    module = kernel()
    validators = [
        Draft202012Validator(
            module._dereference_schema(
                {"$ref": "#/components/schemas/JobCreateRequest"},
                module._SchemaSide(doc, source, None, {}, False),
            )
        )
        for doc in (old, current)
    ]
    samples: list[dict[str, Any]] = [
        {"kind": "asr", "model": "model-demo", "input": {"audio_asset_id": "asset-demo"}},
        {
            "kind": "tts",
            "model": "model-demo",
            "input": {"text": "synthetic"},
            "voice": {"id": "voice-demo", "revision": "1"},
        },
        {
            "kind": "tts",
            "model": "model-demo",
            "input": {"text_asset_id": "asset-demo"},
            "voice": {"id": "voice-demo", "revision": "1"},
        },
        {
            "kind": "voice_design",
            "model": "model-demo",
            "input": {"description": "synthetic", "preview_text": "synthetic"},
        },
        {
            "kind": "voice_clone",
            "model": "model-demo",
            "input": {"reference_audio_asset_id": "asset-demo", "source_use_confirmed": True},
        },
        {
            "kind": "voice_preview",
            "input": {"voice_draft_id": "draft-demo", "draft_revision": "1", "text": "synthetic"},
        },
        {
            "kind": "voice_validation",
            "input": {
                "voice_draft_id": "draft-demo",
                "draft_revision": "1",
                "validation_set_revision": "1",
            },
        },
    ]
    for value in samples:
        assert validators[0].is_valid(value), value["kind"]
        assert validators[1].is_valid(value), value["kind"]
        parse("JobCreateRequest", value)
    for sample in samples[1:3]:
        pinned = {**sample, "model_binding": {"id": "binding-demo", "revision": "1"}}
        assert not validators[0].is_valid(pinned)
        assert validators[1].is_valid(pinned)
        parse("JobCreateRequest", pinned)
    for pin in (
        {"id": "binding-demo", "revision": 1},
        {"id": "binding-demo", "revision": "0"},
        {"revision": "1"},
        {"id": "binding-demo", "revision": "1", "extra": True},
    ):
        assert not validators[1].is_valid({**samples[1], "model_binding": pin})
    for sample in [samples[0], *samples[3:]]:
        assert not validators[1].is_valid(
            {**sample, "model_binding": {"id": "binding-demo", "revision": "1"}}
        )
    for sample in samples:
        assert not validators[1].is_valid({**sample, "unknown": True})
    missing = copy.deepcopy(samples[1])
    del missing["voice"]
    assert not validators[1].is_valid(missing)
    assert module._compare_openapi(old, current, source, standard_only=False) == []
