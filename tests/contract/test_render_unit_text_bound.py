"""RenderUnit.text is the unit's source capacity, not one synthesis.

A render unit is the editing and provenance boundary of one narration segment,
so its stored text may legitimately be longer than one speech synthesis. One
synthesis still accepts at most `MAX_SPEECH_TEXT_CHARS` characters, the bound
`/v1/audio/speech` and `TtsJobInput.text` declare. The two bounds are therefore
different on purpose, and that difference has to stay readable in the contract:
every RenderUnit* schema reuses one shared `RenderUnitText` definition that says
why its 200000-character capacity is larger, and the dispatch path refuses an
over-bound unit instead of shortening it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from speechnexus_backend.speech.domain.speech_synthesis import MAX_SPEECH_TEXT_CHARS

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "contracts" / "workbench-openapi.yaml"

# Every schema that carries a render unit's stored source text, across both
# published workbench versions.
_RENDER_UNIT_TEXT_SITES = (
    "RenderUnit",
    "RenderUnitCreateRequest",
    "RenderUnitPatch",
    "RenderUnitV2",
    "RenderUnitCreateRequestV2",
    "RenderUnitPatchV2",
)

_SOURCE_TEXT_REF = {"$ref": "#/components/schemas/RenderUnitText"}


def _schemas() -> dict[str, Any]:
    document = yaml.safe_load(SOURCE.read_text())
    return cast(dict[str, Any], document["components"]["schemas"])


def test_every_render_unit_text_site_shares_one_declared_source_bound() -> None:
    schemas = _schemas()
    text = cast(dict[str, Any], schemas["RenderUnitText"])

    assert text["type"] == "string"
    assert text["minLength"] == 1
    assert text["maxLength"] == 200_000, (
        "RenderUnit.text is the unit's source capacity, not one synthesis; "
        "narrowing it to the one-synthesis bound would forbid the workbench from "
        "storing text it is meant to split into synthesizable units"
    )
    for site in _RENDER_UNIT_TEXT_SITES:
        declared = schemas[site]["properties"]["text"]
        assert declared == _SOURCE_TEXT_REF, (
            f"{site}.text must reuse the shared RenderUnitText definition so the "
            "source-capacity relation is stated once instead of copied per schema"
        )


def test_the_source_bound_is_documented_as_distinct_from_one_synthesis() -> None:
    """The contract must justify why the two bounds differ, not only declare them."""

    description = cast(str, _schemas()["RenderUnitText"]["description"])

    # It names the enforcing one-synthesis limit and its value, so a reader can
    # connect this field to the public TTS bound instead of assuming a typo.
    assert "MAX_SPEECH_TEXT_CHARS" in description
    assert str(MAX_SPEECH_TEXT_CHARS) in description
    # It says the larger bound is a source capacity, not the size of one synthesis.
    assert "source capacity" in description
    assert "one synthesis" in description
    # It states what actually happens to a unit above the synthesis bound.
    assert "render_submission_rejected" in description
    assert "422" in description


def test_the_declared_source_capacity_matches_the_domain_capacity() -> None:
    """The contract and the domain must agree on the source capacity they enforce."""

    schemas = _schemas()
    declared = cast(dict[str, Any], schemas["RenderUnitText"])["maxLength"]

    accepted_source = _source_text(declared)
    assert len(accepted_source) == declared

    # The domain refuses exactly one character past the declared capacity.
    assert _domain_rejects(declared + 1)


def _source_text(length: int) -> str:
    from speechnexus_backend.workbench.domain.render_units import (
        RenderDependencies,
        RevisionPin,
    )

    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text="中" * length,
    )
    return dependencies.text


def _domain_rejects(length: int) -> bool:
    try:
        _source_text(length)
    except ValueError:
        return True
    return False
