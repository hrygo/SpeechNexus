"""RenderUnit.text is the unit's source capacity, not a request bound.

A render unit is the editing and provenance boundary of one narration segment,
so its stored text may legitimately be longer than one dispatch request. Dispatch
inlines the unit text into one public Job request body whose inline bound is
`MAX_SPEECH_TEXT_CHARS`, the same bound `/v1/audio/speech` and `TtsJobInput.text`
declare. The two bounds are therefore different on purpose, and that difference
has to stay readable in the contract: every schema carrying render-unit source
text reuses one shared `RenderUnitText` definition that says why its
200000-character capacity is larger, and the dispatch path refuses an over-bound
unit instead of shortening it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from speechnexus_backend.speech.domain.speech_synthesis import MAX_SPEECH_TEXT_CHARS
from speechnexus_backend.workbench.infrastructure.render_units_schema import (
    render_units_migration_sql,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "contracts" / "workbench-openapi.yaml"
ARCHITECTURE = ROOT / "docs" / "architecture" / "speech-api.md"

# Every schema that names a render unit and carries its stored source text,
# across both published workbench versions.
_RENDER_UNIT_TEXT_SITES = (
    "RenderUnit",
    "RenderUnitCreateRequest",
    "RenderUnitPatch",
    "RenderUnitV2",
    "RenderUnitCreateRequestV2",
    "RenderUnitPatchV2",
)

# Every other schema with a top-level `text` property, keyed to its capacity.
# These are storage-shaped text fields, not a render unit's source text. Both
# groups together must equal the scanned set: a new `text` property anywhere is a
# reviewed addition, never an unnoticed seventh copy of a render-unit bound.
_OTHER_TEXT_CAPACITIES = {
    "ChapterCreateRequest": 5_000_000,
    "MeetingSealRequest": 1_000_000,
    "TranscriptRevision": 1_000_000,
    "TranscriptRevisionCreateRequest": 1_000_000,
}

_SOURCE_TEXT_REF = {"$ref": "#/components/schemas/RenderUnitText"}

# The only registered dispatch operation; v1 `generateRenderUnit` has no route.
_DISPATCH_OPERATION = "generateRenderUnitV2"
_DISPATCH_PATH = "/v2/workbench/render-units/{unit_id}/generations"


def _schemas() -> dict[str, Any]:
    document = yaml.safe_load(SOURCE.read_text())
    return cast(dict[str, Any], document["components"]["schemas"])


def _text_bearing_schemas() -> dict[str, Any]:
    """Scan the contract instead of trusting a hand-kept list of known fields."""

    return {
        name: schema["properties"]["text"]
        for name, schema in _schemas().items()
        if isinstance(schema, dict)
        and isinstance(schema.get("properties"), dict)
        and "text" in schema["properties"]
    }


def test_every_text_bearing_schema_is_classified() -> None:
    """A new `text` property must be classified, so a seventh site cannot slip in."""

    declared = _text_bearing_schemas()
    expected = set(_RENDER_UNIT_TEXT_SITES) | set(_OTHER_TEXT_CAPACITIES)

    assert set(declared) == expected, (
        "a schema carrying `text` must be classified: render-unit source text "
        "reuses the shared RenderUnitText definition, and every other capacity is "
        "listed explicitly. An unclassified field would leave one bound unguarded"
    )


def test_every_render_unit_text_site_shares_one_declared_source_bound() -> None:
    schemas = _schemas()
    text = cast(dict[str, Any], schemas["RenderUnitText"])

    assert text["type"] == "string"
    assert text["minLength"] == 1
    assert text["maxLength"] == 200_000, (
        "RenderUnit.text is the unit's source capacity, not a request bound; "
        "narrowing it to the inline bound would forbid the workbench from storing "
        "text it is meant to split into dispatches"
    )
    for site in _RENDER_UNIT_TEXT_SITES:
        declared = schemas[site]["properties"]["text"]
        assert declared == _SOURCE_TEXT_REF, (
            f"{site}.text must reuse the shared RenderUnitText definition so the "
            "source-capacity relation is stated once instead of copied per schema"
        )


def test_the_source_bound_is_documented_as_a_request_bound_difference() -> None:
    """The contract must explain the two bounds mechanically, not just declare them."""

    description = cast(str, _schemas()["RenderUnitText"]["description"])

    # It names the shared inline bound and its value, so a reader can connect this
    # field to the public TTS bound instead of assuming a typo.
    assert "MAX_SPEECH_TEXT_CHARS" in description
    assert str(MAX_SPEECH_TEXT_CHARS) in description
    # It frames the larger bound as source capacity against a request bound, which
    # stays true when a long-text capability adds another dispatch mode.
    assert "source capacity" in description
    assert "request bound" in description
    assert "inline" in description
    # It states what actually happens to a unit above the inline bound.
    assert "render_submission_rejected" in description
    assert "422" in description


def test_the_documentation_names_the_registered_dispatch_operation() -> None:
    """A contract pointer must name an operation that actually serves.

    v1 `generateRenderUnit` declares the path but the router registers only the v2
    operation, so pointing a consumer at the v1 name sends it to a 404. The
    description and the cross-entry table must name the same registered target.
    """

    description = cast(str, _schemas()["RenderUnitText"]["description"])

    assert _DISPATCH_OPERATION in description
    assert _DISPATCH_PATH in description
    # No bare v1 operationId: strip the registered name and nothing may remain.
    assert "generateRenderUnit`" not in description.replace(f"{_DISPATCH_OPERATION}`", "")

    table_rows = [
        line
        for line in ARCHITECTURE.read_text().splitlines()
        if line.startswith("|") and "render_submission_rejected" in line
    ]
    assert len(table_rows) == 1, "the cross-entry table must state one render dispatch row"
    assert _DISPATCH_OPERATION in table_rows[0]
    assert _DISPATCH_PATH in table_rows[0]


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


def test_the_declared_source_capacity_matches_the_stored_check_constraint() -> None:
    """The table check is the storage half of the same capacity; it must agree."""

    declared = cast(dict[str, Any], _schemas()["RenderUnitText"])["maxLength"]
    ddl = str(render_units_migration_sql("synthetic_schema"))

    assert f"length(text) <= {declared}" in ddl, (
        "the render_units CHECK constraint must accept exactly the capacity the "
        "contract declares, so a stored unit is never wider than its schema"
    )
