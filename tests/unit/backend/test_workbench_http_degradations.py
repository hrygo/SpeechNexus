"""Every (operation, status) pair answered by the declaration-independent envelope (#777).

The fallback in ``http_versions._error`` keeps the envelope valid when the declaration
gate refuses a response the operation never declared. That makes the failure mode
invisible: nothing compares an operation's declarations with the statuses its handler can
emit, and the contract tooling cannot gate a status nobody declared (see
``docs/validation/http-contract-change-checks.md``). This module is that comparison.

It sweeps behaviour rather than restating declarations. Every routed workbench operation
is read from the live app (so a new route is swept without editing this file) and driven
through its real router with the two boundaries stubbed:

* ``read_envelope`` either raises the ``IngressError`` code under test or returns the
  operation's own valid request example from
  ``tests/fixtures/contracts/workbench-http.json``, completed with the envelope sections
  every router indexes (see ``_load_envelopes``);
* the injected service raises the failure class under test.

A pair is *degraded* exactly when the router answers and the layer logs
``workbench error response outside the operation's declarations``; the sweep records that
pair and checks the answer it observed (status from ``ERRORS``, JSON content type, an
``ErrorResponse`` body, echoed request id). ``DEGRADATIONS`` below is the frozen answer:
the set on which the declarations and the handler chains currently disagree.

What this guard sees, and what it cannot:

* reversed declarations and a new routed operation are always visible -- a declaration
  that closes a gap leaves a stale pair, and a new operation without declarations adds
  pairs;
* a branch added for a *new exception type* is invisible to the sweep, because the matrix
  only raises the types it knows. ``test_every_answering_branch_is_driven_by_the_sweep``
  closes that path statically: every branch that answers a status must catch a type the
  matrix raises, so such a branch fails until the matrix grows with it (this is exactly
  the hole MF-3 found -- an added ``except OSError`` that answered 429 stayed green);
* a new *value* of an already-injected type is driven only if the matrix raises that
  value (a brand-new port code, for instance).
  ``test_literal_refusal_codes_are_declared_or_registered`` covers the codes a router
  names itself; a code only a port can return stays invisible until the matrix raises it;
* an operation the app does not route answers nothing today and is outside the sweep
  entirely; it enters the registry the moment it gains a route.

The sweep freezes what the *handler chains* can answer, not what today's services happen
to raise, and a large share of the pairs is therefore chain capability: a read-only route
is registered for a 409 its service never raises because the shared chain decides the
answer, not the caller. The count is the size of the declaration surface the fallback
covers, **not** the number of real gaps in production -- closing one real gap can delete
several pairs that only ever described the chain. The 408 is likewise injected at the
boundary instead of streaming a slow body, which keeps the sweep deterministic without
pretending the ingress path itself is covered here (that is
``test_workbench_http_error_fallback.py``).

The sweep also asserts that every routed operation was observed calling its service
(``NO_SERVICE`` lists the three routes that cannot, each for a reason), so a sweep that
silently stopped driving an operation fails instead of reporting "nothing degrades". For
those three routes the pair set stays blind to a branch only they can reach -- they answer
from composition state, so no injection drives their dispatch -- though the two static
tests still name such a branch.

Changing this set is a decision, in both directions:

* a new pair means a client answer is now structural-only -- register the pair with the
  issue that closes the gap and why answering without a declaration is allowed meanwhile;
* a pair that disappeared means the operation now declares that response -- delete it.
  The registry fails until it agrees with the declarations and with the answering branches,
  so a changed answer cannot pass unreviewed.
"""

from __future__ import annotations

import ast
import builtins
import json
import logging
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from fastapi.routing import APIRoute, iter_route_contexts
from speechnexus_contracts import workbench_http
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.books import OrdinalConflictError
from speechnexus_backend.workbench.application.downloads import DownloadError
from speechnexus_backend.workbench.application.llm import LlmError
from speechnexus_backend.workbench.application.render_units import (
    RenderDispatchUnavailableError,
    RenderRejection,
    RenderSubmissionRejectedError,
    RenderSubmissionUnknownError,
)
from speechnexus_backend.workbench.application.saved_assets import ObjectZoneUnavailableError
from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.application.voices import VoiceCatalogueUnavailableError
from speechnexus_backend.workbench.domain.assistant import AssistantStateConflictError
from speechnexus_backend.workbench.domain.deletion import (
    ConfirmationMismatchError,
    DeletionDependencyConflictError,
    DeletionOwnerMismatchError,
    DeletionPlanChangedError,
    DeletionPlanExpiredError,
)
from speechnexus_backend.workbench.domain.exports import ExportStateConflictError
from speechnexus_backend.workbench.domain.meetings import MeetingSourceUnsupportedError
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.domain.render_units import RenderAttemptConflictError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.interfaces import (
    http_assistant,
    http_books,
    http_downloads,
    http_exports,
    http_management,
    http_meetings,
    http_render_units,
    http_saved_assets,
    http_teleprompter,
    http_versions,
    http_voices,
)
from speechnexus_backend.workbench.interfaces.cursors import InvalidCursorError
from speechnexus_backend.workbench.interfaces.http_ingress import IngressError
from speechnexus_backend.workbench.interfaces.http_versions import ERRORS

ROOT = Path(__file__).resolve().parents[3]
PRIVATE = "synthetic-777"
DECLARATION_MISS = "workbench error response outside the operation's declarations"
LOGGER = "speechnexus_backend.workbench.interfaces.http_versions"
_LOG_LINE = re.compile(r"operation=(\w+) code=(\w+)")
_PATH_PARAM = re.compile(r"{([^}:]+)(?::[^}]+)?}")

# Registry of the degraded (operation, status) pairs, keyed by status. Each entry states
# the issue that closes the gap and why answering that status without a declaration is
# acceptable until it is closed. Entries are per status because the justification is:
# what produced the status decides whether a declaration-free answer is tolerable.
DEGRADATIONS: dict[int, dict[str, Any]] = {
    400: {
        "issue": "#777",
        "reason": (
            "The shared ingress refuses a malformed or contract-invalid request on every route, "
            "and these operations never declared 400."
        ),
        "operations": frozenset(
            {
                "cancelAssistantTurn",
                "cancelBookExport",
                "getAssistantTurn",
                "getBookExport",
            }
        ),
    },
    404: {
        "issue": "#777",
        "reason": (
            "The shared not-found branch answers 404 on this route, and the operation's "
            "declaration list omits it."
        ),
        "operations": frozenset(
            {
                "listProjects",
                "listVoices",
            }
        ),
    },
    408: {
        "issue": "#777",
        "reason": (
            "The shared ingress enforces the request-body deadline on every route, and these "
            "operations never declared 408."
        ),
        "operations": frozenset(
            {
                "cancelAssistantTurn",
                "cancelBookExport",
                "createAssistantTurn",
                "createBookExport",
                "generateRenderUnitV2",
                "getAssistantTurn",
                "getBookExport",
            }
        ),
    },
    409: {
        "issue": "#777",
        "reason": (
            "The shared conflict branch (revision, idempotency, render attempt) answers 409 on "
            "this route, and the declaration list omits it."
        ),
        "operations": frozenset(
            {
                "downloadSavedAsset",
                "getAssistantTurn",
                "getBook",
                "getBookExport",
                "getChapter",
                "getMeeting",
                "getMeetingMinutes",
                "getProject",
                "getRenderUnit",
                "getRenderUnitV2",
                "getSavedAsset",
                "getTeleprompterState",
                "getTranscript",
                "getTranscriptRevision",
                "listBooks",
                "listChapters",
                "listProjects",
                "listRenderUnitGenerationsV2",
                "listRenderUnits",
                "listRenderUnitsV2",
                "listTranscripts",
            }
        ),
    },
    410: {
        "issue": "#777",
        "reason": (
            "The shared source/download expiry branch answers 410, and the declaration list "
            "omits it."
        ),
        "operations": frozenset(
            {
                "createProject",
                "createTranscriptRevision",
                "downloadSavedAsset",
                "getProject",
                "getSavedAsset",
                "getTranscript",
                "getTranscriptRevision",
                "updateProject",
            }
        ),
    },
    413: {
        "issue": "#777",
        "reason": (
            "The shared ingress enforces the deployment body limit on every route, and these "
            "operations never declared 413."
        ),
        "operations": frozenset(
            {
                "cancelAssistantTurn",
                "cancelBookExport",
                "createAssistantTurn",
                "createBookExport",
                "generateRenderUnitV2",
                "getAssistantTurn",
                "getBookExport",
                "getVoiceRevision",
                "listVoices",
            }
        ),
    },
    415: {
        "issue": "#777",
        "reason": (
            "The shared ingress refuses a non-JSON media type for a body on these operations, "
            "which never declared 415."
        ),
        "operations": frozenset(
            {
                "createAssistantTurn",
                "createBookExport",
            }
        ),
    },
    422: {
        "issue": "#777",
        "reason": (
            "A shared refusal branch reports a semantic rejection as 422, and the declaration "
            "list omits it."
        ),
        "operations": frozenset(
            {
                "cancelAssistantTurn",
                "cancelBookExport",
                "getAssistantTurn",
                "getBook",
                "getBookExport",
                "getChapter",
                "getMeeting",
                "getMeetingMinutes",
                "getProject",
                "getRenderUnit",
                "getRenderUnitV2",
                "getSavedAsset",
                "getTeleprompterState",
                "getTranscript",
                "getTranscriptRevision",
                "listBooks",
                "listChapters",
                "listProjects",
                "listRenderUnits",
                "listRenderUnitsV2",
                "listTranscripts",
            }
        ),
    },
    429: {
        "issue": "#777",
        "reason": (
            "The render dispatch refusal vocabulary reports 429 through this router, and the "
            "declaration list omits it."
        ),
        "operations": frozenset(
            {
                "createRenderUnitV2",
                "getRenderUnit",
                "getRenderUnitV2",
                "listRenderUnitGenerationsV2",
                "listRenderUnits",
                "listRenderUnitsV2",
                "updateRenderUnit",
                "updateRenderUnitV2",
            }
        ),
    },
    500: {
        "issue": "#777",
        "reason": (
            "No operation-wide 500 declaration: an unclassified failure answers internal_error "
            "structurally, but the declaration list is still incomplete."
        ),
        "operations": frozenset(
            {
                "cancelAssistantTurn",
                "cancelBookExport",
                "createAssistantTurn",
                "createBookExport",
                "generateRenderUnitV2",
                "getAssistantTurn",
                "getBookExport",
            }
        ),
    },
    503: {
        "issue": "#777",
        "reason": (
            "The shared store/unavailable branch answers 503, and the declaration list omits it."
        ),
        "operations": frozenset(
            {
                "cancelAssistantTurn",
                "cancelBookExport",
                "createBookExport",
                "getAssistantTurn",
                "getBookExport",
            }
        ),
    },
}


def _load_envelopes() -> dict[str, dict[str, Any]]:
    """One complete valid request envelope per contract operation, as the ingress builds it.

    The fixture's valid examples are per-field probes, so a probe may omit a section the
    router always reads (a body-less example carries no ``path``/``query``/``headers``).
    The richest probe is taken and the four sections the routers index are always present;
    header names are lower-cased because the ingress lower-cases them before a router
    reads one.
    """
    examples = json.loads((ROOT / "tests/fixtures/contracts/workbench-http.json").read_text())[
        "examples"
    ]
    probes: dict[str, list[dict[str, Any]]] = {}
    for example in examples:
        if example.get("kind") == "request" and example.get("valid"):
            probes.setdefault(example["operation"], []).append(example["value"])
    envelopes: dict[str, dict[str, Any]] = {}
    for operation, values in probes.items():
        richest = max(values, key=len)
        envelope: dict[str, Any] = {"path": {}, "query": {}, "headers": {}, "body": {}}
        for section in envelope:
            if section in richest:
                envelope[section] = dict(richest[section])
        envelope["headers"] = {name.lower(): value for name, value in envelope["headers"].items()}
        envelopes[operation] = envelope
    return envelopes


def _body_operations() -> frozenset[str]:
    """Operations whose contract declares a request body: the only ones the ingress 415s."""
    return frozenset(
        operation["operationId"]
        for item in workbench_http.contract()["paths"].values()
        for operation in item.values()
        if "requestBody" in operation
    )


ENVELOPES = _load_envelopes()
BODY_OPERATIONS = _body_operations()

# Routed operations the sweep cannot push through a service call, each by design: the
# wire-only v1 render shape refuses with `workbench_unavailable` before any dispatch, and
# the two status routes answer from the deployment's own composition state, not from a
# store. Every other routed operation must be observed calling its service, or the sweep
# would report "nothing degrades" for an operation it never actually exercised.
NO_SERVICE = frozenset({"createRenderUnit", "getWorkbenchStatus", "getWorkbenchStatusV2"})


class _FailingService:
    """Any attribute is an async callable that raises the failure under test."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.called = False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        async def fail(*_: Any, **__: Any) -> Any:
            self.called = True
            raise self.failure

        return fail


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_STATE: dict[str, Any] = {"envelope": None, "ingress": None}


async def _stub_envelope(request: Any, operation: str, **_: Any) -> Any:
    if _STATE["ingress"] is not None:
        raise IngressError(_STATE["ingress"])
    return json.loads(json.dumps(_STATE["envelope"]))


INTERFACE_MODULES = (
    http_assistant,
    http_books,
    http_downloads,
    http_exports,
    http_management,
    http_meetings,
    http_render_units,
    http_saved_assets,
    http_teleprompter,
    http_versions,
    http_voices,
)

INJECTIONS: tuple[BaseException, ...] = (
    # No branch named it: the last-resort 500.
    RuntimeError(f"{PRIVATE}-unexpected"),
    # The shared inbound boundary, one instance per code it can raise.
    IngressError("malformed_request"),
    IngressError("invalid_request"),
    IngressError("request_timeout"),
    IngressError("request_too_large"),
    IngressError("unsupported_media_type"),
    IngressError("workbench_unavailable"),
    # The store port, both outcomes.
    VersionStoreError(sqlstate=PRIVATE, commit_unknown=False),
    VersionStoreError(sqlstate=PRIVATE, commit_unknown=True),
    # The domain refusals the routers name.
    ResourceNotFoundError(),
    RevisionConflictError(1, 2),
    IdempotencyConflictError(),
    InvalidVersionChangeError(PRIVATE),
    AssistantStateConflictError(),
    ExportStateConflictError(),
    LlmError("disabled"),
    LlmError("invalid_input"),
    LlmError("unavailable"),
    TranscriptSourceError("source_invalid"),
    TranscriptSourceError("source_unsupported"),
    TranscriptSourceError("source_too_large"),
    TranscriptSourceError("source_unavailable"),
    TranscriptSourceError("source_expired"),
    InvalidCursorError(),
    DownloadError("download_expired"),
    DownloadError("download_not_ready"),
    DownloadError("download_unavailable"),
    RenderDispatchUnavailableError(),
    RenderSubmissionUnknownError(),
    RenderAttemptConflictError(),
    RenderSubmissionRejectedError(),
    RenderSubmissionRejectedError(
        RenderRejection.upstream(
            status_code=429, code="job_capacity_exhausted", param=None, retry_after_seconds=7
        )
    ),
    # The remaining refusal types each router names; the static test below fails if a
    # router grows a branch for a type this tuple does not raise.
    OrdinalConflictError(),
    VoiceCatalogueUnavailableError(),
    ObjectZoneUnavailableError(),
    MeetingSourceUnsupportedError(),
    MinutesTaskConflictError(),
    DeletionOwnerMismatchError(),
    ConfirmationMismatchError(),
    DeletionPlanExpiredError(),
    DeletionPlanChangedError(),
    DeletionDependencyConflictError(),
    # A query a router refuses locally, before any call leaves the workbench.
    ValueError(PRIVATE),
)


def _app(failure: BaseException) -> Any:
    service = _FailingService(failure)
    app = create_app(
        service,  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        render_units=service,  # type: ignore[arg-type]
        assistant=service,  # type: ignore[arg-type]
        exports=service,  # type: ignore[arg-type]
        books=service,  # type: ignore[arg-type]
        downloads=service,  # type: ignore[arg-type]
        meetings=service,  # type: ignore[arg-type]
        teleprompter=service,  # type: ignore[arg-type]
        saved_assets=service,  # type: ignore[arg-type]
        voices=service,  # type: ignore[arg-type]
        default_workspace_id=PRIVATE,
        deployment_id=PRIVATE,
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.05,
    )
    app.state.synthetic_service = service
    return app


def _routed(app: Any) -> list[tuple[str, str, str]]:
    """(operation, path template, method) for every routed contract operation."""
    routed: list[tuple[str, str, str]] = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and route.operation_id:
            routed.append((route.operation_id, route.path_format, sorted(route.methods or ())[0]))
    return routed


def _url(path_format: str) -> str:
    return _PATH_PARAM.sub(PRIVATE, path_format)


async def _sweep() -> tuple[dict[tuple[str, int], set[str]], frozenset[str]]:
    """Drive every routed operation through every injected failure and record degradations.

    Also reports which operations were ever observed calling their service, so a harness
    regression (an envelope the router rejects before dispatch) cannot silently turn the
    sweep into a "nothing degrades" tautology.
    """
    observed: dict[tuple[str, int], set[str]] = {}
    reached: set[str] = set()
    routed: set[str] = set()
    capture = _Capture()
    logger = logging.getLogger(LOGGER)
    previous_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.WARNING)
    serial = 0
    try:
        with ExitStack() as stack:
            for module in INTERFACE_MODULES:
                stack.enter_context(mock.patch.object(module, "read_envelope", _stub_envelope))
            for failure in INJECTIONS:
                app = _app(failure)
                service = app.state.synthetic_service
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://workbench.test"
                ) as client:
                    for operation, path_format, method in _routed(app):
                        if isinstance(failure, IngressError) and (
                            failure.code == "unsupported_media_type"
                            and operation not in BODY_OPERATIONS
                        ):
                            continue
                        assert operation in ENVELOPES, (
                            f"{operation} has no valid request example in "
                            "tests/fixtures/contracts/workbench-http.json; add one so the "
                            "sweep can drive the new operation"
                        )
                        capture.records.clear()
                        _STATE["envelope"] = ENVELOPES[operation]
                        _STATE["ingress"] = (
                            failure.code if isinstance(failure, IngressError) else None
                        )
                        routed.add(operation)
                        service.called = False
                        serial += 1
                        rid = f"{PRIVATE}-{serial}"
                        response = await client.request(
                            method, _url(path_format), headers={"X-Request-ID": rid}
                        )
                        if service.called:
                            reached.add(operation)
                        misses = [
                            record
                            for record in capture.records
                            if DECLARATION_MISS in record.getMessage()
                        ]
                        assert len(misses) <= 1, (operation, failure, len(misses))
                        if not misses:
                            continue
                        match = _LOG_LINE.search(misses[0].getMessage())
                        assert match, misses[0].getMessage()
                        name, code = match.group(1), match.group(2)
                        status = ERRORS[code][0]
                        # The logged pair and the answer on the wire are the same decision.
                        assert response.status_code == status, (name, code, response.status_code)
                        assert response.headers["content-type"] == "application/json"
                        body = parse("ErrorResponse", response.json())
                        assert body["error"]["code"] == code
                        assert body["error"]["request_id"] == rid
                        observed.setdefault((name, status), set()).add(f"{code} <- {failure!r}")
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
    assert routed - reached == NO_SERVICE, (
        "the sweep never saw these operations reach their service, so their declarations "
        f"were not exercised: {sorted(routed - reached - NO_SERVICE)}"
    )
    return observed, frozenset(reached)


def _frozen() -> set[tuple[str, int]]:
    return {
        (operation, status)
        for status, entry in DEGRADATIONS.items()
        for operation in entry["operations"]
    }


def _mismatch(added: list[tuple[str, int]], stale: list[tuple[str, int]]) -> str:
    lines = [
        "The set of degraded (operation, status) pairs changed.",
        "",
        "Unregistered pairs -- a client answer is now structural-only. Either declare the "
        "response in the operation's contract (then this list stays empty) or register the "
        "pair under its status with the issue that closes the gap and why answering "
        "without a declaration is allowed meanwhile:",
        *[f"  + {operation} {status}" for operation, status in added],
        "",
        "Stale registry pairs -- the operation now declares that response, so the "
        "declaration closes the gap and the entry must go:",
        *[f"  - {operation} {status}" for operation, status in stale],
    ]
    return "\n".join(lines)


async def test_degradations_match_the_frozen_registry() -> None:
    observed, _ = await _sweep()
    frozen = _frozen()
    added = sorted(set(observed) - frozen)
    stale = sorted(frozen - set(observed))
    assert not added and not stale, _mismatch(added, stale)


def test_registry_entries_state_an_issue_and_a_reason() -> None:
    assert DEGRADATIONS, "the registry may not be emptied: #777's gaps are still open"
    statuses = {entry[0] for entry in ERRORS.values()}
    for status, entry in DEGRADATIONS.items():
        assert status in statuses, status
        assert entry["issue"].startswith("#"), status
        assert entry["reason"].strip(), status
        assert entry["operations"], f"status {status} has no operations"


# ---- Static half of the guard -------------------------------------------------------
#
# The sweep is a fixed matrix, so a branch added for a *new* exception type would never be
# driven and its pairs would never appear: the registry could stay green while the layer
# silently grew a degradation no test can see. These two checks close that path from the
# source side. The first requires every branch that answers a status to catch a type the
# matrix actually raises; the second requires every refusal code a router names as a
# literal to be declared for the operations that router actually dispatches, or already
# registered.

RESPONSE_CALLS = frozenset({"_error", "_unexpected_error"})
DECLARED = {
    operation["operationId"]: {int(status) for status in operation["responses"]}
    for item in workbench_http.contract()["paths"].values()
    for operation in item.values()
}


def _module_operations() -> dict[str, frozenset[str]]:
    """Which operations each router module serves, as the live app registers them."""
    app = _app(RuntimeError(PRIVATE))
    served: dict[str, set[str]] = {}
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and route.operation_id:
            module = route.endpoint.__module__.rsplit(".", 1)[-1]
            served.setdefault(module, set()).add(route.operation_id)
    return {module: frozenset(names) for module, names in served.items()}


SERVED = _module_operations()


def _resolve(module: Any, expression: ast.expr) -> Any:
    if isinstance(expression, ast.Name):
        # A builtin (`OSError`, `ValueError`) is not an attribute of the module.
        return getattr(module, expression.id, None) or builtins.__dict__.get(expression.id)
    if isinstance(expression, ast.Attribute):
        base = _resolve(module, expression.value)
        return None if base is None else getattr(base, expression.attr, None)
    if isinstance(expression, ast.Tuple):
        return [_resolve(module, element) for element in expression.elts]
    return None


def _answering_branches(module: Any) -> set[type]:
    """Exception types whose handler in this router answers a status."""
    tree = ast.parse(Path(module.__file__).read_text())
    caught: set[type] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        answers = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in RESPONSE_CALLS
            for call in ast.walk(node)
        )
        if not answers:
            continue
        resolved = _resolve(module, node.type)
        for candidate in resolved if isinstance(resolved, list) else [resolved]:
            if isinstance(candidate, type):
                caught.add(candidate)
    return caught


def _literal_refusals(module: Any) -> set[str]:
    """Codes this router names literally, plus ``internal_error`` when it answers 500s."""
    tree = ast.parse(Path(module.__file__).read_text())
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "_unexpected_error":
            codes.add("internal_error")
        elif (
            node.func.id == "_error"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            codes.add(node.args[0].value)
    return codes


def test_every_answering_branch_is_driven_by_the_sweep() -> None:
    injected = {type(failure) for failure in INJECTIONS}
    uncovered: list[str] = []
    for module in INTERFACE_MODULES:
        name = module.__name__.rsplit(".", 1)[-1]
        for caught in sorted(_answering_branches(module), key=lambda item: item.__name__):
            # The unclassified failure the sweep raises is an Exception subclass, so the
            # catch-all branch is driven; every other branch needs its own instance.
            if caught is Exception or caught in injected:
                continue
            uncovered.append(f"{name}: except {caught.__name__}")
    assert not uncovered, (
        "these router branches answer a status but the sweep never raises them, so their "
        "declarations were never exercised and their pairs would appear silently. Add an "
        "instance to INJECTIONS, then register or remove the pairs it produces:\n  "
        + "\n  ".join(uncovered)
    )


def test_literal_refusal_codes_are_declared_or_registered() -> None:
    frozen = _frozen()
    unaccounted: list[str] = []
    for module in INTERFACE_MODULES:
        name = module.__name__.rsplit(".", 1)[-1]
        for code in sorted(_literal_refusals(module)):
            assert code in ERRORS, f"{name} names {code!r}, which the error table lacks"
            status = ERRORS[code][0]
            # Only operations whose dispatch can actually reach a service: the routes in
            # NO_SERVICE answer from composition state (or refuse before dispatch), so the
            # branch they name here is unreachable for them by construction.
            for operation in sorted(SERVED.get(name, frozenset()) - NO_SERVICE):
                if status in DECLARED[operation] or (operation, status) in frozen:
                    continue
                unaccounted.append(f"{name}: {operation} answers {status} ({code})")
    assert not unaccounted, (
        "these refusal codes are emitted for operations whose declaration list omits the "
        "status, and the pair is not registered either. Declare the response for the "
        "operation, or register the (operation, status) pair with its issue:\n  "
        + "\n  ".join(unaccounted)
    )
