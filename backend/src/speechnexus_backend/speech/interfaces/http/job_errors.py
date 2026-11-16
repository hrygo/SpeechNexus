"""Stable public Job errors; storage and intent details never cross this boundary."""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

_ERRORS: dict[str, tuple[int, str, str]] = {
    "invalid_job_request": (400, "invalid_request_error", "Invalid Job request."),
    "job_request_too_large": (
        413,
        "invalid_request_error",
        "The Job request body exceeds the deployment receive limit.",
    ),
    "job_not_found": (404, "not_found_error", "The Job was not found."),
    "job_method_not_allowed": (405, "invalid_request_error", "The Job method is not allowed."),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another Job intent.",
    ),
    # The same request succeeds once the original execution confirms stop, which
    # needs no operator or configuration step: under the pinned `retryable` rule
    # that is `true` (#828 review M5).
    "execution_cleanup_pending": (
        409,
        "invalid_request_error",
        "The original execution is not confirmed stopped.",
    ),
    "job_retry_not_allowed": (
        409,
        "invalid_request_error",
        "The Job state does not allow a retry.",
    ),
    "job_retry_unavailable": (
        409,
        "invalid_request_error",
        "The original Job intent is no longer available.",
    ),
    "voice_draft_stale_revision": (
        409,
        "invalid_request_error",
        "The draft revision is no longer current.",
    ),
    "voice_draft_not_generatable": (
        409,
        "invalid_request_error",
        "The draft lifecycle no longer accepts this operation.",
    ),
    "unsupported_parameter": (422, "invalid_request_error", "The Job request is not supported."),
    "job_kind_not_supported": (
        422,
        "invalid_request_error",
        "This execution unit does not accept this Job kind.",
    ),
    "workspace_not_found": (422, "invalid_request_error", "The workspace was not found."),
    "workspace_disabled": (422, "invalid_request_error", "The workspace is disabled."),
    # The workspace row exists but has no ``workspace_quotas`` row: admission has
    # no reservation to charge, and no retry can produce one. This is a
    # deterministic configuration refusal, deliberately distinct from the 503
    # ``job_unavailable`` storage-outage code.
    "workspace_not_configured": (
        422,
        "invalid_request_error",
        "The workspace has no provisioned Job quota.",
    ),
    "model_not_published": (422, "invalid_request_error", "The model is not published."),
    "model_binding_not_published": (
        422,
        "invalid_request_error",
        "The selected binding revision is not published.",
    ),
    "model_binding_retired": (
        422,
        "invalid_request_error",
        "The selected binding revision is retired.",
    ),
    "model_binding_not_current": (
        422,
        "invalid_request_error",
        "The selected binding revision is no longer current.",
    ),
    "model_binding_mismatch": (
        422,
        "invalid_request_error",
        "The model does not match the selected binding.",
    ),
    "model_binding_task_not_supported": (
        422,
        "invalid_request_error",
        "The selected binding does not support file TTS.",
    ),
    "voice_not_published": (422, "invalid_request_error", "The voice revision is not published."),
    "profile_not_published": (
        422,
        "invalid_request_error",
        "The profile revision is not published.",
    ),
    "pronunciation_not_found": (
        422,
        "invalid_request_error",
        "The pronunciation revision was not found.",
    ),
    "asset_not_ready": (422, "invalid_request_error", "The input asset is not ready."),
    "voice_draft_not_candidate": (
        422,
        "invalid_request_error",
        "The draft revision is not a validation candidate.",
    ),
    "job_outcome_unknown": (
        503,
        "service_unavailable_error",
        "The original Job outcome is unknown.",
    ),
    "job_capacity_exhausted": (
        429,
        "rate_limit_error",
        "Job capacity is exhausted; retry after the indicated delay.",
    ),
    "job_binding_incomplete": (
        422,
        "invalid_request_error",
        "The model binding does not declare complete execution parameters.",
    ),
    "job_text_asset_unsupported": (
        422,
        "invalid_request_error",
        "Text asset input is not supported for TTS Jobs.",
    ),
    # The one code that reports the Job store itself: the transaction could not
    # read or write durable state. Nothing an operator can do to an engine repairs
    # it, and no admission refusal may borrow it (#828 review B3).
    "job_unavailable": (503, "service_unavailable_error", "Job storage is unavailable."),
    # The pinned instance is ready, but its readiness receipt elapsed. The store is
    # healthy and no Job row was written; an operator re-signs the receipt and the
    # same request is then admitted. Recovery needs that out-of-band step, so the
    # refusal does not invite a retry of its own (#828, review B1).
    "engine_not_ready": (
        503,
        "service_unavailable_error",
        "The engine readiness receipt for the pinned instance has expired; "
        "re-register the engine (speechnexus-register-engine --renew-readiness), "
        "then send a new request.",
    ),
    # The pinned runtime state disagrees with the request (generation, topology,
    # observation or pool-owner identity): registering the engine again reconciles
    # it. The refusal is about the runtime, never the Job store.
    "engine_state_mismatch": (
        503,
        "service_unavailable_error",
        "The pinned engine instance no longer matches this request's runtime state; "
        "re-register the engine, then send a new request.",
    ),
    # The instance, its pool or its recipe approval is not active (stopped,
    # draining, unapproved or outside its approval window).
    "engine_resource_disabled": (
        503,
        "service_unavailable_error",
        "The pinned engine instance, its pool, its template or its recipe approval "
        "is not active; restore the instance, then send a new request.",
    ),
    # The request was built from another recipe than the one approved for the
    # pinned instance.
    "recipe_not_admitted": (
        503,
        "service_unavailable_error",
        "The request does not match the transcription recipe approved for the "
        "pinned instance; register the engine against the approved recipe, then "
        "send a new request.",
    ),
    # Version compare-and-set and pool-owner fencing races: the transaction rolled
    # back and the same request succeeds once the concurrent writer finishes.
    "job_admission_contended": (
        503,
        "service_unavailable_error",
        "Admission state has not settled (a concurrent write or a draining "
        "generation); retry the request.",
    ),
    # This request's own admission deadline elapsed before it could be admitted:
    # the deadline is part of the pinned intent, so only a new request can succeed.
    "job_request_expired": (
        422,
        "invalid_request_error",
        "The Job request's admission deadline elapsed before it was admitted; "
        "submit a new request.",
    ),
    # A clock or version counter left its declared range: an internal invariant
    # failed, so the answer is a server error and not a retryable refusal.
    "admission_state_invalid": (
        500,
        "api_error",
        "Admission state failed an internal invariant; this is a deployment defect "
        "and the request must not be replayed as-is.",
    ),
}


# The declared answers that describe deployment state rather than the shape of one
# request: every create in this deployment gets one of these until the named
# resource changes, so they are the closed set the admission-readiness report has
# to cover. The report's cause enum is pinned to exactly this set in both
# directions (``tests/contract/test_job_admission_readiness.py``): a code added
# here without a cause would let the report answer `admitting` while every create
# is refused, and a cause naming a code outside this set would point an operator
# at an answer the create operation does not produce.
DEPLOYMENT_STATE_CODES: frozenset[str] = frozenset(
    {
        "engine_not_ready",
        "engine_state_mismatch",
        "engine_resource_disabled",
        "job_capacity_exhausted",
        "workspace_not_found",
        "workspace_disabled",
        "workspace_not_configured",
        "job_unavailable",
    }
)


def job_error(
    code: str,
    request_id: str,
    *,
    headers: dict[str, str] | None = None,
    detail: str | None = None,
) -> JSONResponse:
    """Render one declared Job error, and refuse to invent a storage answer.

    ``detail`` replaces the code's default message when this build knows the
    concrete missing item (a blank Job DSN, an unpublished binding, no registered
    engine instance). It never changes the declared code, status or retryability,
    and it must never carry a DSN value, credential or other internal fact -- the
    key name is what an operator needs (#877).

    An undeclared code is a deployment defect. It used to fall back to
    ``job_unavailable`` -- "Job storage is unavailable." -- so any unmapped cause
    in the layers above silently restored the #828 symptom. It now answers the
    declared 500 ``admission_state_invalid`` envelope and names the bad code in the
    message (#828 review M1).
    """
    undeclared = code not in _ERRORS
    if undeclared:
        message = (
            f"The Job API emitted an undeclared error code ({code}); this is a "
            "deployment defect and the request must not be replayed as-is."
        )
        code = "admission_state_invalid"
        status, kind, _ = _ERRORS[code]
    else:
        status, kind, message = _ERRORS[code]
    if detail is not None:
        message = detail
    # ``retryable`` answers one question: can this same request succeed without an
    # out-of-band change? Only the unknown execution outcome (the original identity
    # is replayed), a capacity delay a released lease clears, and the admission
    # contention/fencing races qualify. A refusal repaired by an operator step --
    # re-signing a readiness receipt, restoring a disabled instance, re-registering
    # against an approved recipe -- is ``false`` even though the recovery is known,
    # and that recovery lives in the message instead (#828 review B1). The
    # ``job_unavailable`` storage outage never qualifies.
    # ``execution_cleanup_pending`` is not retryable: the Job body reports
    # ``retryable: false`` for the same state, and nothing finishes the cleanup on
    # its own -- the capacity stays held until the gateway records a stop it can
    # prove or the accepted cancellation is judged against facts that authorize
    # isolation (#852 review P2).
    retryable = code in (
        "job_outcome_unknown",
        "job_capacity_exhausted",
        "job_admission_contended",
    )
    response_headers = {"Cache-Control": "no-store", **(headers or {})}
    if code == "job_capacity_exhausted":
        # One second is the minimum claim-lease granularity (see
        # PostgresDispatchQueue: 1_000 <= lease_ms). A capacity refusal is
        # therefore retried no sooner than the next possible lease release.
        response_headers.setdefault("Retry-After", "1")
    return JSONResponse(
        status_code=status,
        headers=response_headers,
        content=parse(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": message,
                    "param": None,
                    "request_id": request_id,
                    "retryable": retryable,
                }
            },
        ),
    )
