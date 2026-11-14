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

import ast
import functools
import re
from pathlib import Path
from typing import cast

import pytest
import yaml

from speechnexus_backend.speech.application.public_jobs import (
    _ADMISSION_REJECTION_CODES,
)
from speechnexus_backend.speech.interfaces.http.job_errors import (
    _ERRORS as UPSTREAM_JOB_ERRORS,
)
from speechnexus_backend.workbench.application.render_units import RenderRejection
from speechnexus_backend.workbench.infrastructure.public_speech_render_jobs import (
    _REFUSAL_STATUSES,
)
from speechnexus_backend.workbench.interfaces.http_render_units import (
    _CONFLICT_CODES,
    _CONFLICT_CODES_RESERVED,
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
# The public Job handler that answers this adapter's create call. Reachability is a
# property of *that* table, not of the vocabulary it draws from.
JOBS_HTTP = (
    ROOT / "backend" / "src" / "speechnexus_backend" / "speech" / "interfaces" / "http" / "jobs.py"
)
# The whole speech package, because `ModelBindingError` is constructed by both the
# application service and its storage adapter.
SPEECH_SRC = ROOT / "backend" / "src" / "speechnexus_backend" / "speech"

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


def test_every_mapped_code_is_declared_in_the_upstream_vocabulary() -> None:
    """Every mapped code is a declared public Job code.

    This is the *vocabulary* check only: it says a mapped code is spelled like a real
    upstream code, not that this ingress can receive it. Reachability is a different
    and narrower set (`cancel` and `retry` add codes no creation request produces), so
    it is derived and asserted in
    `test_the_unreachable_mapping_is_declared_and_still_unreachable` below. Claiming
    "upstream never sends it would be unreachable" here would be the very guard
    defect the #755 review found: a check whose docstring promises something larger
    than what it tests.
    """

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


def _except_type_names(handler: ast.ExceptHandler) -> list[str]:
    node = handler.type
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple):
        return [element.id for element in node.elts if isinstance(element, ast.Name)]
    return []


def _job_error_calls(node: ast.AST) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "job_error"
        and call.args
    ]


def _literal_code(call: ast.Call) -> str | None:
    """The code this call states outright, or None when it forwards one at runtime.

    None is not "no code": it means the code is decided at runtime and the guard must
    resolve it from the surrounding `except` table. Treating an unrecognised spelling
    as "no dynamic code" is how a guard silently stops checking anything, so every
    non-literal shape below either resolves or fails loudly.
    """
    argument = call.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


@functools.cache
def _model_binding_raised_codes() -> frozenset[str]:
    """The literal codes `ModelBindingError` is constructed with anywhere in speech.

    Derived from the constructor sites rather than assumed from the family prefix:
    an assumed family would keep a stale mapping key passing after a family member
    loses its raise site (MF-2). A construction without a literal code fails loudly,
    because this derivation cannot see through it.
    """
    codes: set[str] = set()
    for path in sorted(SPEECH_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ModelBindingError"
            ):
                continue
            argument = node.args[0] if len(node.args) == 1 else None
            if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                raise AssertionError(
                    f"{path}: ModelBindingError is constructed without a literal code "
                    f"at line {node.lineno}; this guard derives its code set from those "
                    "literals and cannot resolve a runtime value"
                )
            codes.add(argument.value)
    assert codes, "the ModelBindingError constructor sites must be discoverable"
    return frozenset(codes)


def _dynamic_code_set(exception_name: str) -> frozenset[str]:
    """The codes one exception class can forward through a non-literal `job_error`."""
    if exception_name == "JobAdmissionRejectedError":
        # The admission store answers with a fixed, declared rejection vocabulary.
        return _ADMISSION_REJECTION_CODES
    if exception_name == "ModelBindingError":
        # The class carries one of the codes it is actually constructed with, so the
        # derived set is exact; `test_the_model_binding_family_is_exactly_what_is_raised`
        # pins the derived set to the declared family, which is what makes "exact"
        # falsifiable when the family grows.
        return _model_binding_raised_codes()
    raise AssertionError(
        f"the create handler forwards a runtime code from {exception_name}; teach this "
        "guard that class's code set instead of letting the derived set silently "
        "under-approximate"
    )


def _create_route_codes() -> frozenset[str]:
    """The upstream codes the createJob route can answer with, derived from its source.

    The public Job vocabulary is not this set. `execution_cleanup_pending`,
    `job_retry_not_allowed` and `job_retry_unavailable` are raised by the cancel and
    retry handlers, which a creation request never reaches, so comparing the workbench
    mapping against the whole vocabulary passes for a branch this ingress cannot
    receive -- exactly the guard defect the #755 review found (M1).
    """
    tree = ast.parse(JOBS_HTTP.read_text(encoding="utf-8"), filename=str(JOBS_HTTP))
    create = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_create"
    )
    handlers = [node for node in ast.walk(create) if isinstance(node, ast.ExceptHandler)]
    owner: dict[int, ast.ExceptHandler] = {}
    for handler in handlers:
        for call in _job_error_calls(handler):
            owner[id(call)] = handler
    codes: set[str] = set()
    for call in _job_error_calls(create):
        literal = _literal_code(call)
        if literal is not None:
            codes.add(literal)
            continue
        # The code is decided at runtime. Never skip it because the spelling looks
        # unfamiliar: `error.code`, `str(error.code)`, `error.reason` and a bare
        # variable all forward a code this derivation cannot read, and the default
        # answer is a loud failure rather than a quietly smaller set (MF-1).
        owner_handler = owner.get(id(call))
        if owner_handler is None:
            raise AssertionError(
                f"line {call.lineno}: job_error() in the create handler takes a "
                "runtime code outside any except handler; this guard cannot derive "
                "which codes that branch can answer with"
            )
        exception_names = _except_type_names(owner_handler)
        if not exception_names:
            raise AssertionError(
                f"line {call.lineno}: the except clause forwarding a runtime code has no "
                "readable exception name; this guard cannot resolve its code set"
            )
        for exception_name in exception_names:
            codes |= _dynamic_code_set(exception_name)
    return frozenset(codes)


def test_the_model_binding_family_is_exactly_what_is_raised() -> None:
    """The derived binding code set and the declared family may not drift apart (MF-2).

    The reachable derivation reads `ModelBindingError`'s constructor literals instead
    of assuming "every `model_binding_*` code is raised". That is only exact while the
    two sets are equal: a declared family member that lost its raise site would
    otherwise keep a stale mapping key green, and a raised code outside the family
    would be an undeclared public error. Comparing them here turns either drift into a
    red test *before* it can widen the reachable set silently.
    """

    family = frozenset(code for code in UPSTREAM_JOB_ERRORS if code.startswith("model_binding_"))
    raised = _model_binding_raised_codes()
    assert raised == family, (
        "ModelBindingError must be constructed with exactly its declared family: "
        f"raised-but-undeclared={sorted(raised - family)}, "
        f"declared-but-never-raised={sorted(family - raised)}"
    )


def test_the_unreachable_mapping_is_declared_and_still_unreachable() -> None:
    """A mapped code must be reachable from createJob, or explicitly reserved (M1).

    `execution_cleanup_pending` is retained because a caller that later reaches the
    upstream cancel and retry endpoints should not need a vocabulary change. The
    retention is only honest while the public contract says the code is reserved
    rather than served, and only while that sentence stays falsifiable: this test
    re-derives the reachable set from the Job handler, so the moment the code becomes
    reachable the contract paragraph is wrong and this test fails until both the
    annotation and the prose are corrected.
    """
    reachable = _create_route_codes()
    reserved = set(_CONFLICT_CODES_RESERVED)

    # The derivation must be real: the create vocabulary is wide and certainly
    # contains the code that names a spent key.
    assert len(reachable) >= 20, reachable
    assert "idempotency_conflict" in reachable

    # Every mapped key is a literal in the handler or an admission/`ModelBindingError`
    # code, and the family test below pins that class's raised set to its declared
    # family, so this comparison is exact in both directions rather than assuming it.
    mapped = set(_REFUSAL_CODES) | set(_CONFLICT_CODES)
    unknown = sorted(mapped - reachable - reserved)
    assert unknown == [], (
        f"{unknown} are mapped but no createJob answer can produce them; either the "
        "mapping went stale or the code belongs in _CONFLICT_CODES_RESERVED with "
        "matching contract prose"
    )
    assert reserved <= set(UPSTREAM_JOB_ERRORS), (
        "a reserved code must still be a declared public Job code"
    )
    assert not (reserved & reachable), (
        f"{sorted(reserved & reachable)} became reachable from createJob while the "
        "contract still says this operation does not answer with it; update the "
        "contract paragraph and _CONFLICT_CODES_RESERVED together with this test"
    )
    assert reserved == {"execution_cleanup_pending"}, (
        "the reserved set is a reviewed decision; changing it requires the contract "
        "prose and the panel copy to change in the same edit"
    )
    # The contract is the consumer-facing half of the same claim.
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    description = cast(
        dict[str, str],
        document["paths"]["/v2/workbench/render-units/{unit_id}/generations"]["post"],
    )["description"]
    for upstream in sorted(reserved):
        served_code = _CONFLICT_CODES[upstream]
        assert served_code in description, (
            f"{served_code} is reserved, so the contract must still name it"
        )
    assert "reserved rather than" in description, (
        "the contract must state that the reserved 409 is not served by this operation"
    )


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
        (429, "job_capacity_exhausted", "render_submission_rate_limited", True),
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
