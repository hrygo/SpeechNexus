"""Only the exact approved request bound narrowing is allowed through the gate."""

from __future__ import annotations

import copy
import importlib.util
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

from tests.contract.test_http_contract_changes import ROOT, _git, _write
from tests.contract.test_http_contract_changes import repository as repository

SOURCE = "contracts/speech-openapi.yaml"
PATH = "/v1/nexus/jobs"
OTHER_PATH = "/v1/nexus/jobs-batch"
METHOD = "post"
COMPONENT = "TtsJobInput"
# A second exclusive variant in the same request body: the channel the review
# found, because the kernel collapses every variant difference into one finding.
SIBLING = "VoicePreviewJobInput"
POINTER = "oneOf/0/properties/text"
FIELD = "text"
KEYWORD = "maxLength"
FROM_BOUND = 200000
TO_BOUND = 4096
VERSION = "0.1.0.dev18"
FINDING = (
    f"{SOURCE}:paths/{PATH}/{METHOD}/requestBody/application/json/oneOf: "
    "exclusive variants changed; compatibility needs explicit proof"
)

PROOF = (
    "Production admission assembly, real HTTP: 4096 characters created a Job "
    "(202) and 4097 characters were already refused (400, domain "
    "MAX_SPEECH_TEXT_CHARS=4096), so no text above 4096 was ever usable and "
    "the narrowing removes only unachievable promises. 4096 code points also "
    "stay within the 16384-byte gateway budget, so the character bound is the "
    "binding constraint."
)
ENTRY = (
    SOURCE,
    PATH,
    METHOD,
    VERSION,
    VERSION,
    COMPONENT,
    POINTER,
    FIELD,
    KEYWORD,
    FROM_BOUND,
    TO_BOUND,
)


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "http_change_cli_narrowings", ROOT / "scripts/dev-env/check-http-contract-changes.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def component(bound: int | None, *, field: str = FIELD) -> dict[str, Any]:
    text: dict[str, Any] = {"type": "string", "minLength": 1}
    if bound is not None:
        text[KEYWORD] = bound
    text["description"] = f"The inline {field} of one synthesis."
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": [field],
                "properties": {field: text},
            }
        ]
    }


def operation() -> dict[str, Any]:
    return {
        "operationId": "createJob",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            {"$ref": f"#/components/schemas/{COMPONENT}"},
                            {"$ref": f"#/components/schemas/{SIBLING}"},
                        ]
                    }
                }
            },
        },
        "responses": {"202": {"description": "Synthetic"}},
    }


def sibling(bound: int | None, *, extra: str | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "text": {"type": "string", "minLength": 1},
    }
    if bound is not None:
        properties["text"][KEYWORD] = bound
    if extra is not None:
        properties[extra] = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["text"],
        "properties": properties,
    }


def schemas_for(bound: int | None, sibling_bound: int | None = 2000) -> dict[str, Any]:
    return {COMPONENT: component(bound), SIBLING: sibling(sibling_bound)}


def document(
    bound: int | None,
    *,
    version: str = VERSION,
    paths: tuple[str, ...] = (PATH,),
    schemas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": version},
        "paths": {path: {METHOD: copy.deepcopy(operation())} for path in paths},
        "components": {"schemas": schemas or schemas_for(bound)},
    }


def analyze(
    base: dict[str, Any],
    current: dict[str, Any],
    *,
    registry: dict[Any, dict[str, str]] | None = None,
) -> tuple[list[str], list[str]]:
    loaded = module()
    findings = list(loaded._kernel()._compare_openapi(base, current, SOURCE, standard_only=False))
    if registry is not None:
        vars(loaded)["_APPROVED_REQUEST_BOUND_NARROWINGS"] = registry
    return cast(
        tuple[list[str], list[str]],
        loaded._apply_approved_request_bound_narrowings(
            SOURCE,
            base,
            current,
            base["info"]["version"],
            current["info"]["version"],
            findings,
        ),
    )


def variant(**changes: Any) -> dict[Any, dict[str, str]]:
    """The exact entry with one declared slot replaced, for negative cases."""

    slots = {
        "source": 0,
        "path": 1,
        "method": 2,
        "from_version": 3,
        "to_version": 4,
        "component": 5,
        "pointer": 6,
        "field": 7,
        "keyword": 8,
        "from_bound": 9,
        "to_bound": 10,
    }
    key = list(ENTRY)
    for name, value in changes.items():
        key[slots[name]] = value
    return {tuple(key): {"issue": "#690", "reason": "Synthetic.", "proof": PROOF}}


def test_the_allowlist_entry_is_frozen() -> None:
    assert module()._APPROVED_REQUEST_BOUND_NARROWINGS == {
        ENTRY: {
            "issue": "#690",
            "reason": (
                "One async tts Job performs exactly one synthesis, so its inline text "
                "bound must equal the synchronous input bound and the enforcing domain "
                "and gateway limits; 200000 described text the service never accepts."
            ),
            "proof": PROOF,
        }
    }


def test_the_exact_approved_narrowing_is_filtered() -> None:
    findings, approved = analyze(document(FROM_BOUND), document(TO_BOUND))

    assert findings == []
    assert len(approved) == 1
    assert approved[0].startswith(f"{SOURCE}:paths/{PATH}/{METHOD}/requestBody/{COMPONENT}/")
    assert "narrowed maxLength 200000 -> 4096" in approved[0]
    assert "#690" in approved[0]


def test_a_different_version_pair_still_rejects() -> None:
    findings, approved = analyze(
        document(FROM_BOUND), document(TO_BOUND), registry=variant(to_version="0.1.0.dev19")
    )

    assert approved == []
    assert FINDING in findings


def test_a_different_field_still_rejects() -> None:
    findings, approved = analyze(
        document(FROM_BOUND), document(TO_BOUND), registry=variant(field="body")
    )

    assert approved == []
    assert FINDING in findings
    assert any("does not end with properties/body" in finding for finding in findings)


def test_a_relaxation_direction_still_rejects() -> None:
    findings, approved = analyze(
        document(TO_BOUND),
        document(FROM_BOUND),
        registry=variant(from_bound=TO_BOUND, to_bound=FROM_BOUND),
    )

    assert approved == []
    assert any("is not a narrowing" in finding for finding in findings)
    assert any("exclusive variants changed" in finding for finding in findings)


def test_a_registration_for_another_operation_still_rejects() -> None:
    findings, approved = analyze(
        document(FROM_BOUND), document(TO_BOUND), registry=variant(path=OTHER_PATH)
    )

    assert approved == []
    assert FINDING in findings
    assert any("is not reachable from this operation request body" in f for f in findings)


def test_a_registration_only_covers_the_operation_it_names() -> None:
    base = document(FROM_BOUND, paths=(PATH, OTHER_PATH))
    current = document(TO_BOUND, paths=(PATH, OTHER_PATH))

    findings, approved = analyze(base, current, registry=variant(path=OTHER_PATH))

    # Both operations reach the changed component, but the entry names one of them.
    assert len(approved) == 1
    assert f"paths/{OTHER_PATH}/" in approved[0]
    assert FINDING in findings
    assert all(f"paths/{PATH}/" not in entry for entry in approved)


def test_an_unregistered_narrowing_still_rejects() -> None:
    findings, approved = analyze(document(FROM_BOUND), document(TO_BOUND), registry={})

    assert approved == []
    assert FINDING in findings


def test_an_entry_without_a_proof_is_a_defect() -> None:
    entries = variant()
    for metadata in entries.values():
        del metadata["proof"]

    findings, approved = analyze(document(FROM_BOUND), document(TO_BOUND), registry=entries)

    assert approved == []
    assert FINDING in findings
    assert any("issue, reason and proof" in finding for finding in findings)


def test_a_second_simultaneous_bound_change_still_rejects() -> None:
    base = document(FROM_BOUND)
    current = document(TO_BOUND)
    base["components"]["schemas"][COMPONENT]["oneOf"][0]["properties"]["instructions"] = {
        "type": "string",
        "maxLength": 4096,
    }
    current["components"]["schemas"][COMPONENT]["oneOf"][0]["properties"]["instructions"] = {
        "type": "string",
        "maxLength": 2048,
    }

    findings, approved = analyze(base, current)

    assert approved == []
    assert FINDING in findings
    assert any("2 bound differences" in finding for finding in findings)


# The kernel emits one finding per request body and inlines every exclusive
# variant first, so a registration that proved only its own component would let
# any sibling variant change ride on it. Each case below changes a sibling only.
def test_a_sibling_variant_narrowing_still_rejects() -> None:
    base = document(FROM_BOUND)
    current = document(TO_BOUND, schemas=schemas_for(TO_BOUND, sibling_bound=100))

    findings, approved = analyze(base, current)

    assert approved == []
    assert FINDING in findings
    assert any("2 bound differences" in finding for finding in findings)


def test_a_sibling_variant_relaxation_still_rejects() -> None:
    base = document(FROM_BOUND)
    current = document(TO_BOUND, schemas=schemas_for(TO_BOUND, sibling_bound=5000))

    findings, approved = analyze(base, current)

    assert approved == []
    assert FINDING in findings
    assert any("2 bound differences" in finding for finding in findings)


def test_a_sibling_variant_added_property_still_rejects() -> None:
    base = document(FROM_BOUND)
    current = document(
        TO_BOUND,
        schemas={COMPONENT: component(TO_BOUND), SIBLING: sibling(2000, extra="prompt")},
    )

    findings, approved = analyze(base, current)

    assert approved == []
    assert FINDING in findings
    assert any(
        "another structural change exists in the same operation request body" in f for f in findings
    )


def test_a_dormant_registration_does_not_cover_a_sibling_change() -> None:
    base = document(TO_BOUND)
    current = document(TO_BOUND, schemas=schemas_for(TO_BOUND, sibling_bound=5000))

    findings, approved = analyze(base, current)

    assert approved == []
    assert any("is not the registered field" in finding for finding in findings)


def test_a_structural_change_beside_the_narrowing_still_rejects() -> None:
    base = document(FROM_BOUND)
    current = document(TO_BOUND)
    current["components"]["schemas"][COMPONENT]["oneOf"][0]["properties"]["speed"] = {
        "type": "number"
    }

    findings, approved = analyze(base, current)

    assert approved == []
    assert any("another structural change" in finding for finding in findings)


def test_a_bound_already_narrowed_on_both_sides_stays_dormant() -> None:
    findings, approved = analyze(document(TO_BOUND), document(TO_BOUND))

    assert findings == []
    assert approved == []


def test_a_reverted_contract_makes_the_registration_unbacked() -> None:
    findings, approved = analyze(document(TO_BOUND), document(FROM_BOUND))

    assert approved == []
    assert any("does not match the documents" in finding for finding in findings)


def revert_bound(root: Path) -> None:
    value = yaml.safe_load((root / SOURCE).read_text())
    branches = value["components"]["schemas"][COMPONENT]["oneOf"]
    inline = [branch for branch in branches if FIELD in branch.get("properties", {})]
    assert len(inline) == 1
    inline[0]["properties"][FIELD][KEYWORD] = FROM_BOUND
    _write(root / SOURCE, value)


def test_the_real_speech_narrowing_is_the_only_filtered_change(
    repository: tuple[ModuleType, Path, str],
) -> None:
    loaded, root, _ = repository
    shutil.copytree(ROOT / "contracts", root / "contracts", dirs_exist_ok=True)
    revert_bound(root)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Frozen 200000 baseline")
    base = _git(root, "rev-parse", "HEAD")
    # The working tree keeps the real contract, so the comparison is the real one.
    shutil.copyfile(ROOT / SOURCE, root / SOURCE)

    report = loaded.inspect_sources(base, surface="speech")

    assert report["findings"] == []
    assert len(report["approved_request_bound_narrowings"]) == 1
    assert "narrowed maxLength 200000 -> 4096" in report["approved_request_bound_narrowings"][0]


def test_the_real_speech_report_is_rejected_when_the_registration_is_removed(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, root, _ = repository
    shutil.copytree(ROOT / "contracts", root / "contracts", dirs_exist_ok=True)
    revert_bound(root)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Frozen 200000 baseline")
    base = _git(root, "rev-parse", "HEAD")
    shutil.copyfile(ROOT / SOURCE, root / SOURCE)
    monkeypatch.setattr(loaded, "_APPROVED_REQUEST_BOUND_NARROWINGS", cast(Any, {}))

    report = loaded.inspect_sources(base, surface="speech")

    assert any("exclusive variants changed" in finding for finding in report["findings"])
    assert report["approved_request_bound_narrowings"] == []
