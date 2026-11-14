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
  operator step per code;
- the retryability promise is compared too, because it is the one property a status
  alone gets backwards: a 429 is a retryable refusal and a 401 is not, a cleanup wait
  is retryable and a spent idempotency key is not (#754, #755).

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

import pytest
import yaml

from speechnexus_backend.speech.interfaces.http.job_errors import (
    _ERRORS as UPSTREAM_JOB_ERRORS,
)
from speechnexus_backend.workbench.application.render_units import RenderRejection
from speechnexus_backend.workbench.infrastructure.public_speech_render_jobs import (
    _REFUSAL_STATUSES,
)
from speechnexus_backend.workbench.interfaces.http_render_units import (
    _CONFLICT_CODES,
    _REFUSAL_CODES,
    _RETRYABLE_REFUSAL_CODES,
    refusal_code,
    refusal_retryable,
    retry_after_seconds,
)
from speechnexus_backend.workbench.interfaces.http_versions import ERRORS

# The adapter's own refusal set, imported rather than copied: adding a status there
# must widen this guard in the same commit, which is what the parametrization below
# promises. A hard-coded tuple would silently keep the old set.
REFUSAL_STATUSES = sorted(_REFUSAL_STATUSES)

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
    unknown = sorted((set(_REFUSAL_CODES) | set(_CONFLICT_CODES)) - upstream)
    assert unknown == [], f"{unknown} are not public upstream Job error codes"
    # The text bound is handled separately: only a named text parameter may blame it.
    assert "unsupported_parameter" not in _REFUSAL_CODES
    # The two pinned-resource codes share the one step they demand.
    assert (
        _REFUSAL_CODES["model_binding_not_published"]
        == _REFUSAL_CODES["profile_not_published"]
        == "render_submission_binding_unavailable"
    )
    # #755: the three 409 meanings are distinct steps, and only the cleanup wait is
    # retryable, so no two of them may collapse onto one code.
    assert len({*_CONFLICT_CODES.values(), "render_submission_conflict"}) == 3


def test_every_mapping_value_names_a_published_code() -> None:
    """A mapping value that is not a declared code makes the route raise, not answer.

    The three-way vocabulary comparison above only pins the code *set*; it says
    nothing about the values the mapping may produce. Adding a mapping entry is the
    most common edit here, and a typo in its value turns a refusal into a `KeyError`
    inside `_error` — a 500 with no stated cause, exactly the defect #750 closes.
    """

    produced = set(_REFUSAL_CODES.values()) | set(_CONFLICT_CODES.values())
    undeclared = sorted(produced - set(ERRORS))
    assert undeclared == [], (
        f"{undeclared} are produced by the mapping but declared nowhere in ERRORS; "
        "the route would raise instead of answering"
    )
    assert refusal_code(None) in ERRORS, "the fallback code must itself be declared"


def _panel_retryable() -> set[str]:
    source = PANEL_VOCABULARY.read_text()
    block = source.split("RENDER_REFUSAL_RETRYABLE = [", 1)[1].split("] as const", 1)[0]
    return set(_CODE_LITERAL.findall(block))


def test_the_retryable_dispositions_agree_on_all_three_sides() -> None:
    """The retry promise is published three times and must say the same thing.

    The backend decides the envelope's `error.retryable`, the contract prose tells a
    consumer which codes may be retried, and the panel states the operator step. Let
    any one of them drift and the operator is told to retry a spent key, or told to
    give up on a capacity refusal that only needed a wait -- the exact pair of
    mistakes #754 and #755 fixed.
    """

    backend = set(_RETRYABLE_REFUSAL_CODES)
    panel = _panel_retryable()
    assert backend == panel, (
        "the panel's RENDER_REFUSAL_RETRYABLE must list exactly the backend's "
        "retryable dispositions"
    )
    assert backend <= _backend_codes(), (
        "a retryable disposition must also be published in the refusal vocabulary"
    )
    assert backend, "at least the capacity refusal and the cleanup wait are retryable"
    # The contract prose states the retry promise per code, so each retryable code
    # must be named there like any other disposition.
    assert backend <= _contract_codes()


def test_the_retryability_of_every_status_and_upstream_code_is_the_stated_one() -> None:
    """One grid, both directions: nothing else may answer `retryable: true`.

    A 429 admission refusal and a 409 ``execution_cleanup_pending`` are the only two
    refusals whose next step is "wait, then dispatch again". Every other status and
    code -- in particular a 401/403/415 and a spent idempotency key -- must be
    terminal, and the four status families must never borrow each other's answer.
    """

    retryable_cases = 0
    for status in REFUSAL_STATUSES:
        for upstream in sorted(UPSTREAM_JOB_ERRORS):
            for param in (None, "input.text"):
                code = refusal_code(
                    RenderRejection.upstream(status_code=status, code=upstream, param=param)
                )
                expected = status == 429 or (
                    status == 409 and upstream == "execution_cleanup_pending"
                )
                assert refusal_retryable(code) is expected, (
                    f"{upstream} at {status} (param={param!r}) produced {code!r}, whose "
                    f"retryability must be {expected}"
                )
                retryable_cases += 1 if expected else 0
    assert retryable_cases, "the grid must contain the retryable dispositions it checks"


@pytest.mark.parametrize(
    ("status", "upstream", "expected_code", "expected_retryable"),
    [
        (429, "job_capacity_exhausted", "render_submission_capacity_exhausted", True),
        (409, "execution_cleanup_pending", "render_submission_cleanup_pending", True),
        (409, "idempotency_conflict", "render_submission_idempotency_conflict", False),
        (409, "job_retry_not_allowed", "render_submission_conflict", False),
        (401, "invalid_job_request", "render_submission_credentials_rejected", False),
        (403, "invalid_job_request", "render_submission_permission_denied", False),
        (415, "invalid_job_request", "render_submission_media_unsupported", False),
    ],
)
def test_the_named_retry_directions_are_pinned(
    status: int, upstream: str, expected_code: str, expected_retryable: bool
) -> None:
    """The seven named cases, stated explicitly so a refactor cannot reorder them."""

    rejection = RenderRejection.upstream(status_code=status, code=upstream, param=None)
    assert refusal_code(rejection) == expected_code
    assert refusal_retryable(expected_code) is expected_retryable


def test_only_a_retryable_disposition_may_state_a_retry_delay() -> None:
    """A delay on a terminal refusal would promise a retry the operator cannot use."""

    delayed = RenderRejection.upstream(
        status_code=429,
        code="job_capacity_exhausted",
        param=None,
        retry_after_seconds=4,
    )
    assert retry_after_seconds(delayed, refusal_code(delayed)) == 4
    # The same observed delay on a terminal disposition is not republished.
    terminal = RenderRejection.upstream(
        status_code=401,
        code="invalid_job_request",
        param=None,
        retry_after_seconds=4,
    )
    assert retry_after_seconds(terminal, refusal_code(terminal)) is None
    assert retry_after_seconds(None, refusal_code(None)) is None


@pytest.mark.parametrize("status", REFUSAL_STATUSES)
def test_every_refusal_code_and_status_maps_to_a_published_disposition(status: int) -> None:
    """The mapping is total: no upstream code at a refusal status may escape it."""

    for upstream in sorted(UPSTREAM_JOB_ERRORS):
        for param in (None, "input.text", "voice.revision"):
            code = refusal_code(
                RenderRejection.upstream(status_code=status, code=upstream, param=param)
            )
            assert code in ERRORS, (
                f"{upstream} at {status} (param={param!r}) produced {code!r}, "
                "which is not a declared workbench error code"
            )
