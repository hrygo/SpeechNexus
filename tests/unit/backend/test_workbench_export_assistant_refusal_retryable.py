"""Every 4xx the export and assistant operations answer is `retryable: false` (#806).

`#803` made the three panels decide "may this settled refusal be replayed under the
original Idempotency-Key?" from `ErrorBody.error.retryable`. The render panel also owns a
per-code copy table, but the export and assistant panels have none, so they read the field
literally: `recovery/command-journal.ts` classifies a *settled* refusal (4xx, with 408 and
429 excluded) as `retryable` or `terminal`, and only `retryable` keeps the replay
affordance.

Both routers answer their refusals through `http_versions._error`, whose `retryable`
argument defaults to `False`. That default is the premise, and it is not one uniform fact.
Five of the statuses below settle the attempt: the operation refused before creating
anything, so the retained key can never produce a different answer and replay must stay
off. The sixth, `408 request_timeout`, is answered by the ingress layer before dispatch
("Request body deadline exceeded"), so it created nothing either; the field stays `false`
there for the same reason as the rest -- these routes never state a retry permission --
while the panel reaches the same conclusion on its own, because a 408 does not settle the
attempt and it keeps the key regardless of this field. The premise was true but untested.
Nothing made `_error(..., retryable=True)` on one of these branches visible: the envelope
schema allows both values, the panel tests build their own `ErrorResponse` fixtures, and
the suites that already drive these routes assert the field only for 500/503
(`test_workbench_http_error_fallback`).

This module drives every 4xx branch of the six operations through the real app and asserts
the field on the answer, so adding a retry permission to any of them fails here: on a
settled refusal it would silently offer a replay that can only re-refuse, and on 408 it
would state a step these operations cannot take.

What this does not cover: the same operations' 5xx answers are deliberately
`retryable: false` *and* replayable -- a 5xx is not a settled refusal, so the panels keep
the key and replay it as recovery of an unknown outcome. That opposite direction is stated
in `contracts/shared/errors.schema.json` and pinned in
`tests/contract/test_error_retryable_semantics.py`; it is not retryability, so it is not
asserted here.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass
from typing import Any
from unittest import mock

import httpx
import pytest
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.llm import LlmError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.assistant import AssistantStateConflictError
from speechnexus_backend.workbench.domain.exports import ExportStateConflictError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.interfaces import http_assistant, http_exports
from speechnexus_backend.workbench.interfaces.http_ingress import IngressError
from speechnexus_backend.workbench.interfaces.http_versions import ERRORS
from tests.unit.backend.test_workbench_http_degradations import _load_envelopes

PRIVATE = "synthetic-806"
IDEMPOTENT = {"Idempotency-Key": "key-806-0001", "X-Request-ID": "rid-806-probe"}
# The four 4xx statuses the shared ingress answers on any route, and the 415 only a
# route with a request body can reach. Each is a refusal that created nothing.
INGRESS_REFUSALS = (
    IngressError("malformed_request"),
    IngressError("invalid_request"),
    IngressError("request_timeout"),
    IngressError("request_too_large"),
)
BODY_REFUSAL = IngressError("unsupported_media_type")

# (operation, method, path) of the six routes whose consumers read `error.retryable`
# without a per-code vocabulary of their own.
OPERATIONS = (
    ("createAssistantTurn", "POST", "/v1/workbench/projects/{project_id}/assistant-turns"),
    ("getAssistantTurn", "GET", "/v1/workbench/assistant-turns/{turn_id}"),
    ("cancelAssistantTurn", "POST", "/v1/workbench/assistant-turns/{turn_id}/cancellations"),
    ("createBookExport", "POST", "/v1/workbench/books/{book_id}/exports"),
    ("getBookExport", "GET", "/v1/workbench/exports/{export_id}"),
    ("cancelBookExport", "POST", "/v1/workbench/exports/{export_id}/cancellations"),
)
BODY_OPERATIONS = frozenset({"createAssistantTurn", "createBookExport"})
ASSISTANT_OPERATIONS = frozenset({"createAssistantTurn", "getAssistantTurn", "cancelAssistantTurn"})

# One refusal per branch the two routers catch. The branch set is per router, not per
# operation: `_error` is reached by the shared chain, so a read route can answer a 409 it
# never raises itself -- which is exactly why the field must stay false there too.
ASSISTANT_BRANCHES = (
    ResourceNotFoundError(),
    IdempotencyConflictError(),
    RevisionConflictError(1, 2),
    AssistantStateConflictError(),
    InvalidVersionChangeError(PRIVATE),
    LlmError("invalid_input"),
    LlmError("profile_mismatch"),
    LlmError("source_not_sealed"),
    LlmError("source_changed"),
)
EXPORT_BRANCHES = (
    ResourceNotFoundError(),
    IdempotencyConflictError(),
    RevisionConflictError(1, 2),
    ExportStateConflictError(),
    InvalidVersionChangeError(PRIVATE),
)


@dataclass(frozen=True)
class _Case:
    operation: str
    method: str
    path: str
    failure: BaseException
    status: int
    code: str


def _cases() -> list[_Case]:
    """Every 4xx answer these six operations can give, one case per answer."""
    cases: list[_Case] = []
    for operation, method, path in OPERATIONS:
        ingress = INGRESS_REFUSALS + ((BODY_REFUSAL,) if operation in BODY_OPERATIONS else ())
        for refusal in ingress:
            status, _, _ = ERRORS[refusal.code]
            cases.append(_Case(operation, method, path, refusal, status, refusal.code))
        branches = ASSISTANT_BRANCHES if operation in ASSISTANT_OPERATIONS else EXPORT_BRANCHES
        for branch in branches:
            code = _code_of(operation, branch)
            cases.append(_Case(operation, method, path, branch, ERRORS[code][0], code))
    return cases


def _code_of(operation: str, failure: BaseException) -> str:
    """The table code the router's own ``except`` branch answers with.

    Derived from the modules rather than restated: a case that guessed the code would
    assert the wrong answer once a branch changes its disposition, which is precisely the
    drift this suite exists to catch.
    """
    if isinstance(failure, LlmError):
        from speechnexus_backend.workbench.interfaces.http_versions import LLM_CODES

        return LLM_CODES[failure.code]
    # The remaining branches answer one literal code each, except the two domain state
    # refusals that share `invalid_change`; they are listed per operation because the
    # export and assistant routers catch different types.
    return {
        "ResourceNotFoundError": "resource_not_found",
        "IdempotencyConflictError": "idempotency_conflict",
        "RevisionConflictError": "revision_conflict",
        "AssistantStateConflictError": "invalid_change",
        "ExportStateConflictError": "invalid_change",
        "InvalidVersionChangeError": "invalid_change",
    }[type(failure).__name__]


CASES = _cases()
ENVELOPES = _load_envelopes()


class _RaisingService:
    """A service whose every call raises the injected refusal; none of them is expected."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    async def get_turn(self, **_: Any) -> Any:
        raise self.failure

    async def create_turn(self, **_: Any) -> Any:
        raise self.failure

    async def cancel_turn(self, **_: Any) -> Any:
        raise self.failure

    async def get_export(self, **_: Any) -> Any:
        raise self.failure

    async def create_export(self, **_: Any) -> Any:
        raise self.failure

    async def cancel_export(self, **_: Any) -> Any:
        raise self.failure


_STATE: dict[str, Any] = {"envelope": None, "ingress": None}


async def _stub_envelope(request: Any, operation: str, **_: Any) -> Any:
    """Hand the router a valid envelope, or the ingress refusal under test.

    The ingress is not what this suite tests -- it is already covered by
    `test_workbench_http_error_fallback` -- so it is stubbed to reach the router's own
    `except` branches, where the retry permission is decided.
    """
    if _STATE["ingress"] is not None:
        raise IngressError(_STATE["ingress"])
    return json.loads(json.dumps(_STATE["envelope"]))


def _app(failure: BaseException) -> Any:
    service = _RaisingService(failure)
    return create_app(
        None,
        assistant=service,  # type: ignore[arg-type]
        exports=service,  # type: ignore[arg-type]
        default_workspace_id=PRIVATE,
        deployment_id=PRIVATE,
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.05,
    )


async def _call(case: _Case) -> httpx.Response:
    _STATE["envelope"] = ENVELOPES[case.operation]
    _STATE["ingress"] = case.failure.code if isinstance(case.failure, IngressError) else None
    app = _app(case.failure)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with (
        mock.patch.object(http_assistant, "read_envelope", _stub_envelope),
        mock.patch.object(http_exports, "read_envelope", _stub_envelope),
    ):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://workbench.test"
        ) as client:
            return await client.request(
                case.method,
                case.path.replace("{project_id}", PRIVATE)
                .replace("{turn_id}", PRIVATE)
                .replace("{book_id}", PRIVATE)
                .replace("{export_id}", PRIVATE),
                headers=IDEMPOTENT,
            )


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[f"{case.operation}-{case.status}-{case.code}" for case in CASES],
)
async def test_every_4xx_refusal_is_terminal(case: _Case) -> None:
    """No 4xx from these operations may grant a retry permission.

    For a settled refusal -- 4xx other than 408/429 -- `retryable: true` tells
    `command-journal.ts` that the retained key is still worth re-sending, which can only
    reproduce the same refusal while looking like recovery. The two exceptions settle
    nothing: a 408 may have crossed the network before the deadline, and a 429 was
    answered before anything existed, so the panel keeps the key either way. The field
    still has to be `false` there, because no branch of these two routers knows a step
    the caller could take; this assertion is about that absence, not about the panel's
    408 rule.
    """
    response = await _call(case)
    assert response.status_code == case.status, response.text
    assert 400 <= case.status < 500, (
        "this suite pins the 4xx half of the surface; a 5xx is not a settled refusal"
    )
    error = parse("ErrorResponse", response.json())["error"]
    assert error["code"] == case.code, response.text
    assert error["retryable"] is False, (
        f"{case.operation} answered {case.status} {case.code} with "
        f"retryable={error['retryable']}; these operations have no per-code vocabulary, "
        "so the export and assistant panels read this field to decide whether the "
        "original Idempotency-Key may be replayed. A settled refusal would become "
        "replayable, and 408/429 would gain a permission they do not need because the "
        "panel keeps the key by itself; no 4xx from these operations may grant one"
    )
    assert response.headers["x-should-retry"] == "false", dict(response.headers)
    assert response.headers["cache-control"] == "no-store", dict(response.headers)


def test_the_matrix_reaches_every_4xx_status_these_routes_answer() -> None:
    """A case list that missed a status family would pin a smaller surface silently."""
    assert {case.status for case in CASES} == {400, 404, 408, 409, 413, 415, 422}
    assert {case.operation for case in CASES} == {operation for operation, _, _ in OPERATIONS}
    for operation in ("createAssistantTurn", "getAssistantTurn", "cancelAssistantTurn"):
        assert any(case.operation == operation for case in CASES), operation


def test_the_two_routers_never_pass_an_explicit_retry_permission() -> None:
    """The behavioural sweep can only drive the branches the services raise today.

    A branch added without a matching case would stay invisible there, so the source is
    pinned too: every refusal in these two modules goes through `_error`'s default. Adding
    a retry permission here is a product decision about replaying a settled refusal, and it
    has to change this test in the same edit as the branch.
    """
    offenders: list[str] = []
    for module in (http_assistant, http_exports):
        tree = ast.parse(inspect.getsource(module))
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_error"
            ):
                continue
            named = {keyword.arg for keyword in call.keywords}
            if "retryable" in named:
                offenders.append(f"{module.__name__}:{call.lineno}")
    assert offenders == [], (
        f"{offenders} pass an explicit retry permission; these two routes answer every "
        "refusal terminally because the panels replay a settled refusal only when the "
        "contract says the same dispatch is retryable"
    )
