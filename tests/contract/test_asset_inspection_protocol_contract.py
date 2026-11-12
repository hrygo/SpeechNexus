"""The frozen asset-inspection protocol and its public error vocabulary."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from speechnexus_contracts import parse
from speechnexus_contracts.audio_inspection import parse as parse_inspection

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "contracts/internal/asset-inspection.openapi.yaml"
CANONICAL = ROOT / "contracts/internal/audio-inspection.schema.json"
PUBLIC = ROOT / "contracts/speech-openapi.yaml"
SURFACE = "asset_inspection"
PATH = "/internal/v1/inspection/audio"
OPERATION = "inspectAssetAudio"


def load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def resolve(document: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        reference = node["$ref"]
        assert reference.startswith("#/"), reference
        node = document
        for token in reference[2:].split("/"):
            node = node[token]
    return node


def generated_artifact() -> dict[str, Any]:
    path = files("speechnexus_contracts").joinpath(f"{SURFACE}.openapi.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_success_body_reuses_the_canonical_definition_and_generated_consumer() -> None:
    protocol = load(PROTOCOL)
    operation = protocol["paths"][PATH]["post"]
    assert protocol["security"] == [{"InspectionBearer": []}]
    assert operation["requestBody"]["required"] is True
    assert set(operation["requestBody"]["content"]) == {"application/octet-stream"}
    source_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert source_ref == "./audio-inspection.schema.json#/$defs/AudioInspectionSuccess"

    artifact = generated_artifact()
    generated = artifact["paths"][PATH]["post"]
    reference = generated["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert reference.startswith("#/components/schemas/")
    name = reference.rsplit("/", 1)[-1]
    assert "AudioInspectionSuccess" in name
    definition = artifact["components"]["schemas"][name]
    canonical = json.loads(CANONICAL.read_text(encoding="utf-8"))["$defs"]["AudioInspectionSuccess"]
    assert definition["required"] == canonical["required"]
    assert set(definition["properties"]) == set(canonical["properties"])
    assert "oneOf" not in definition and "$defs" not in definition

    example = generated["responses"]["200"]["content"]["application/json"]["example"]
    assert parse_inspection("AudioInspectionSuccess", example) is example
    Draft202012Validator({"$ref": reference, "components": artifact["components"]}).validate(
        example
    )


def test_failure_vocabulary_has_a_valid_positive_example_for_every_category() -> None:
    protocol = load(PROTOCOL)
    operation = protocol["paths"][PATH]["post"]
    assert {"400", "401", "413", "415", "422", "429", "503", "504"} <= set(operation["responses"])
    failure = protocol["components"]["schemas"]["AssetInspectionFailure"]
    validator = Draft202012Validator(failure)
    observed: dict[str, str] = {}
    for raw in operation["responses"].values():
        response = resolve(protocol, raw)
        content = response["content"]["application/json"]
        for key, example in content.get("examples", {}).items():
            value = example["value"]
            validator.validate(value)
            observed[key] = value["status"]

    # One positive example per required failure category, plus every declared
    # vocabulary member in the closed schema.
    assert observed["invalid_media"] == "invalid_media"
    assert observed["media_limit_exceeded"] == "media_limit_exceeded"
    assert observed["busy"] == "busy"
    assert observed["unavailable"] == "unavailable"
    assert observed["deadline_exceeded"] == "deadline_exceeded"
    assert set(failure["properties"]["status"]["enum"]) == set(observed.values())
    assert observed["bad_request"] == "bad_request"
    assert observed["unauthorized"] == "unauthorized"
    assert observed["encoded_too_large"] == "encoded_too_large"
    assert observed["unsupported_media"] == "unsupported_media"


def test_public_asset_errors_freeze_content_and_retry_semantics() -> None:
    public = load(PUBLIC)
    operation = public["paths"]["/v1/nexus/assets"]["post"]

    unavailable = resolve(public, operation["responses"]["503"])
    retry_codes = unavailable["x-nexus-error-codes"]
    assert set(retry_codes) == {
        "asset_inspection_unavailable",
        "asset_inspection_busy",
    }
    assert retry_codes["asset_inspection_unavailable"]["retryable"] is False
    assert retry_codes["asset_inspection_busy"]["retryable"] is True
    busy_text = " ".join(retry_codes["asset_inspection_busy"]["description"].split())
    unavailable_text = " ".join(retry_codes["asset_inspection_unavailable"]["description"].split())
    assert "same request may succeed" in busy_text
    assert "cannot succeed" in unavailable_text
    assert "X-Should-Retry` remains `false`" in " ".join(unavailable["description"].split())
    assert unavailable["headers"]["X-Should-Retry"]["$ref"].endswith("/NoOrdinaryRetry")
    unavailable_examples = unavailable["content"]["application/json"]["examples"]
    for code, entry in retry_codes.items():
        assert entry["description"].strip()
        wire = unavailable_examples[code]["value"]
        assert parse("ErrorResponse", wire) is wire
        assert wire["error"]["code"] == code
        assert wire["error"]["retryable"] is entry["retryable"]

    content = resolve(public, operation["responses"]["422"])
    content_codes = content["x-nexus-error-codes"]
    assert set(content_codes) == {"invalid_asset_audio", "asset_audio_too_long"}
    content_examples = content["content"]["application/json"]["examples"]
    for code, entry in content_codes.items():
        assert entry["description"].strip()
        assert entry["retryable"] is False
        assert "No Asset row" in " ".join(entry["description"].split())
        wire = content_examples[code]["value"]
        assert parse("ErrorResponse", wire) is wire
        assert wire["error"]["code"] == code
        assert wire["error"]["retryable"] is False
    assert content["headers"]["X-Should-Retry"]["$ref"].endswith("/NoOrdinaryRetry")
    assert "No `speech_assets` row" in content["description"]

    text = PUBLIC.read_text(encoding="utf-8")
    for code in (
        "invalid_asset_audio",
        "asset_audio_too_long",
        "asset_inspection_busy",
    ):
        assert code in text


def test_binding_manifest_registers_the_internal_surface() -> None:
    manifest = json.loads((ROOT / "contracts/bindings.json").read_text(encoding="utf-8"))
    surface = manifest["http_surfaces"][SURFACE]
    assert surface["source"] == "internal/asset-inspection.openapi.yaml"
    assert surface["paths"] == [PATH]
    assert surface["operations"] == [OPERATION]
    assert surface.get("validate") is None
    assert generated_artifact()["paths"][PATH]["post"]["operationId"] == OPERATION


def test_an_unsupported_media_type_is_a_content_verdict_not_an_outage() -> None:
    """The ingress bounds bytes only, so the caller's encoding reaches the recipe.

    A format the recipe does not accept is therefore the caller's file, and the
    public answer must be the permanent `422` family. A `503` here would tell the
    caller to retry bytes that can never succeed, which is the exact confusion the
    protocol separates.
    """
    protocol = load(PROTOCOL)
    unsupported = protocol["components"]["responses"]["UnsupportedMedia"]
    text = " ".join(unsupported["description"].split())
    assert "422 invalid_asset_audio" in text
    assert "503 asset_inspection_unavailable" not in text
    assert "permanent content verdict" in text
    assert "No Asset row" in text

    # The byte cap keeps the opposite meaning: the ingress already refuses an
    # oversized upload, so a `413` from the inspector proves a limit mismatch.
    too_large = protocol["components"]["responses"]["EncodedTooLarge"]
    oversized = " ".join(too_large["description"].split())
    assert "503 asset_inspection_unavailable" in oversized
    assert "422 invalid_asset_audio" not in oversized


def test_public_unavailability_no_longer_lists_a_media_type_refusal() -> None:
    public = load(PUBLIC)
    operation = public["paths"]["/v1/nexus/assets"]["post"]
    unavailable = resolve(public, operation["responses"]["503"])
    description = " ".join(unavailable["description"].split()).lower()
    # The retired cause: a media type is content, so it left the 503 vocabulary.
    assert "media type" not in description
    for entry in unavailable["x-nexus-error-codes"].values():
        assert "media type" not in entry["description"].lower()

    content = resolve(public, operation["responses"]["422"])
    content_text = " ".join(content["description"].split()).lower()
    assert "media type" in content_text
    code_text = " ".join(
        content["x-nexus-error-codes"]["invalid_asset_audio"]["description"].split()
    ).lower()
    assert "media type" in code_text
