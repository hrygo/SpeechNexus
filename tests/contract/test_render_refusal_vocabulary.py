"""Every published dispatch-refusal code has exactly one consumer disposition (#750).

`generateRenderUnitV2` answers a refused dispatch with one stable `code` per operator
step, because the upstream Job contract shares one status class between a text over
its bound, an unpublished voice, an unprovisioned workspace, an unready asset, an
unpublished binding and an oversized body. That vocabulary is a promise to
consumers, so it is enumerated on both sides and compared here:

- the backend table (`workbench.interfaces.http_versions.ERRORS`) is the authority
  for which codes exist;
- the contract prose must name every one of them, so the public document cannot go
  stale;
- the panel's `render-refusals.ts` must list exactly the same set and state one
  operator step per code.

Without this comparison a code added on one side would silently fall through to the
panel's "结果尚未确认" alert, which is a worse answer than the generic refusal it
replaced. `Record<RenderRefusalCode, string>` in that module makes "listed but no
copy" a `pnpm typecheck` failure; this test makes "backend added, panel not updated"
a pytest failure.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml

from speechnexus_backend.speech.interfaces.http.job_errors import (
    _ERRORS as UPSTREAM_JOB_ERRORS,
)
from speechnexus_backend.workbench.interfaces.http_render_units import _REFUSAL_CODES
from speechnexus_backend.workbench.interfaces.http_versions import ERRORS

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "workbench-openapi.yaml"
PANEL_VOCABULARY = ROOT / "web" / "workbench" / "src" / "books" / "render-refusals.ts"
PANEL = ROOT / "web" / "workbench" / "src" / "books" / "RenderUnitPanel.tsx"

PREFIX = "render_submission_"
# The refusal vocabulary is stated on the dispatch operation and referenced by the
# RenderUnitText description; the operation's own prose is the single source here.
# The contract names codes in backticks and the panel as string literals, so the
# pattern matches the bare name in both and the quoting style stays free.
_CODE_LITERAL = re.compile(rf"(?P<code>{PREFIX}[a-z0-9_]+)")


def _backend_codes() -> set[str]:
    return {code for code in ERRORS if code.startswith(PREFIX)}


def _contract_codes() -> set[str]:
    document = yaml.safe_load(CONTRACT.read_text())
    operation = cast(
        dict[str, str],
        document["paths"]["/v2/workbench/render-units/{unit_id}/generations"]["post"],
    )
    return set(_CODE_LITERAL.findall(operation["description"]))


def _panel_codes() -> set[str]:
    source = PANEL_VOCABULARY.read_text()
    block = source.split("RENDER_REFUSAL_CODES = [", 1)[1].split("] as const", 1)[0]
    return set(_CODE_LITERAL.findall(block))


def test_the_backend_and_the_panel_agree_on_the_vocabulary() -> None:
    """A code on either side without the other is an unanswered refusal in the UI."""

    backend = _backend_codes()
    panel = _panel_codes()

    assert backend == panel, (
        "every render_submission_* code in the backend table must be listed in "
        "web/workbench/src/books/render-refusals.ts and vice versa: a code the panel "
        "does not know falls through to the '结果尚未确认' alert"
    )
    assert backend, "the dispatch refusal vocabulary must not be empty"


def test_the_contract_states_the_same_vocabulary() -> None:
    """The public document is the consumer's reference; it may not omit a code."""

    assert _contract_codes() == _backend_codes(), (
        "the generateRenderUnitV2 description must name exactly the codes the backend "
        "emits, so a consumer reading the contract is not surprised by a new one"
    )


def test_every_code_has_an_operator_step_and_the_panel_uses_the_list() -> None:
    """Listing a code is not handling it: the copy and the branching must exist too."""

    source = PANEL_VOCABULARY.read_text()
    for code in sorted(_backend_codes()):
        assert re.search(rf"^\s{{2}}{code}:", source, re.MULTILINE), (
            f"{code} is in the vocabulary but has no RENDER_REFUSAL_STEPS entry"
        )
    assert "refusedCode(" in PANEL.read_text(), (
        "the panel must resolve refusals through rendered-refusals' refusedCode(), "
        "otherwise the enumerated list is decorative and the copy unreachable"
    )


def test_the_mapping_only_names_codes_the_upstream_contract_declares() -> None:
    """A mapped code that upstream never sends would be an unreachable branch."""

    upstream = set(UPSTREAM_JOB_ERRORS)
    unknown = sorted(set(_REFUSAL_CODES) - upstream)
    assert unknown == [], f"{unknown} are not public upstream Job error codes"
    # The text bound is handled separately: only a named text parameter may blame it.
    assert "unsupported_parameter" not in _REFUSAL_CODES
    # The two pinned-resource codes share the one step they demand.
    assert (
        _REFUSAL_CODES["model_binding_not_published"]
        == _REFUSAL_CODES["profile_not_published"]
        == "render_submission_binding_unavailable"
    )
