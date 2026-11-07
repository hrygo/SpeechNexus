"""Seven source-validated voice creation, metadata, revision and preview operations."""

import json
from collections.abc import Callable
from functools import lru_cache
from importlib.resources import files
from typing import Any, cast

from fastapi import APIRouter, Request
from jsonschema import Draft202012Validator, FormatChecker
from speechnexus_contracts import ContractValidationError, parse
from speechnexus_contracts.voice_drafts_http import contract, validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    DisabledWorkspaceError,
    IdempotencyConflictError,
    JobAdmissionRejectedError,
    JobOutcomeUnknownError,
    JobRejectedError,
    ModelBindingError,
    ModelNotPublishedError,
    UnknownWorkspaceError,
    VoiceDraftNotCandidateError,
    VoiceDraftStaleRevisionError,
    VoiceInputInvalidError,
)
from speechnexus_backend.speech.application.voice_drafts import (
    VoiceDraftConflictError,
    VoiceDraftNotFoundError,
    VoiceDrafts,
    voice_input_document,
    voice_input_fits,
    voice_input_shape,
)
from speechnexus_backend.speech.domain.public_jobs import (
    PublicJobNotFoundError,
    PublicJobUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract
from speechnexus_backend.speech.interfaces.http.jobs import (
    _constant,
    _idempotency_key,
    _object,
    _receive,
    _TooLargeError,
    public_job,
)
from speechnexus_backend.speech.interfaces.http.origin_policy import rejected_authority

# (status, public type, message, wire code, retryable). ``retryable`` is a
# decision, not a function of the status: a definite transient failure may be
# retried, but a submission whose commit is unknown may only be replayed with
# its original Idempotency-Key.
_ERRORS: dict[str, tuple[int, str, str, str, bool]] = {
    "invalid_request": (
        400,
        "invalid_request_error",
        "Invalid voice draft request.",
        "invalid_request",
        False,
    ),
    "authority_rejected": (
        403,
        "permission_error",
        "The voice Host or Origin is not allowed.",
        "authority_rejected",
        False,
    ),
    "not_found": (404, "not_found_error", "The voice draft was not found.", "not_found", False),
    "conflict": (
        409,
        "invalid_request_error",
        "Voice metadata version or intent conflicts.",
        "conflict",
        False,
    ),
    # ``job_rejected`` with a conflict reason: the store refused to insert a
    # prepared Job because the facts it pinned changed, an extreme UUID
    # identity collision occurred, or the revision was exhausted. The transaction rolled
    # back, so nothing was committed, but the request conflicts with current
    # state and replaying it cannot succeed. This is the declared 409 conflict
    # class -- the same stable ``voice_draft_conflict`` code -- not a retryable
    # storage outage. A submission-window connection failure is outcome unknown,
    # never this class.
    "rejected": (
        409,
        "invalid_request_error",
        "Voice draft facts or identity conflict with the current state.",
        "conflict",
        False,
    ),
    # A request without a key that may already have committed cannot be retried
    # safely: a resubmission is a new intent and may create a second draft and
    # generation Job. 409 is the same reconcile-the-original class already used
    # for an unconfirmed cancellation; it is deliberately not a retryable 503.
    "outcome_unknown_unkeyed": (
        409,
        "invalid_request_error",
        "The original submission may have been accepted; resubmit only to create a new draft.",
        "outcome_unknown",
        False,
    ),
    "not_candidate": (
        422,
        "invalid_request_error",
        "The draft revision is not a candidate.",
        "not_candidate",
        False,
    ),
    "unsupported_parameter": (
        422,
        "invalid_request_error",
        "Only display metadata is mutable.",
        "unsupported_parameter",
        False,
    ),
    "input_unavailable": (
        422,
        "invalid_request_error",
        "Published model or ready input required.",
        "input_unavailable",
        False,
    ),
    # Admission capacity is transient: the same request can succeed once a
    # lease is released. ``Retry-After`` is required by the 429 contract.
    "capacity": (
        429,
        "rate_limit_error",
        "Job capacity is exhausted; retry after the indicated delay.",
        "capacity",
        True,
    ),
    "unavailable": (
        503,
        "service_unavailable_error",
        "Voice draft storage is unavailable.",
        "unavailable",
        True,
    ),
    # Only a request that carried its original Idempotency-Key reaches this:
    # the same-key replay recovers the committed draft instead of creating a
    # second one, so retrying is safe and bounded.
    "outcome_unknown": (
        503,
        "service_unavailable_error",
        "Reconcile the original voice request with its Idempotency-Key.",
        "outcome_unknown",
        True,
    ),
    # Our own reply violated the declared contract: an implementation defect,
    # not an outage, so it must not be dressed up as a retryable 503.
    "contract_violation": (
        500,
        "api_error",
        "The voice draft response violated the declared contract.",
        "contract_violation",
        False,
    ),
    # The store refused the write at the database due to a deterministic
    # constraint/data error. That is an implementation defect, not a client
    # conflict and not a retryable storage outage.
    "rejected_defect": (
        500,
        "api_error",
        "The voice draft write was rejected by a deterministic constraint.",
        "contract_violation",
        False,
    ),
}


def voice_drafts_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "VoiceDrafts_")


def _error(code: str, request_id: str, operation: str) -> JSONResponse:
    status, kind, message, wire, retryable = _ERRORS[code]
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()}
    if code == "capacity":
        headers["Retry-After"] = "1"
    body = parse(
        "ErrorResponse",
        {
            "error": {
                "code": "voice_draft_" + wire,
                "type": kind,
                "message": message,
                "param": None,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )
    # The error reply is part of the same contract; validate it before sending so
    # a mismatched error shape cannot masquerade as a declared status.
    validate_response(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


# A creation request is stored verbatim and later echoed as the draft revision, so
# the stored shape is the revision schema in the same packaged surface. Checking it
# before the Job is submitted rejects input that could never satisfy that echo.
_STORED_INPUTS = {
    "createVoiceDesign": ("design", "External_voice_resource_schema_json_DesignInput"),
    "createVoiceClone": ("clone", "External_voice_resource_schema_json_CloneInput"),
}


@lru_cache
def _stored_input_validator(name: str) -> Draft202012Validator:
    document = cast(
        dict[str, Any],
        json.loads(
            files("speechnexus_contracts").joinpath("voice_drafts.openapi.json").read_text()
        ),
    )
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": document["components"]},
        format_checker=FormatChecker(),
    )


def _stored_input_rejected(operation: str, payload: dict[str, Any]) -> bool:
    kind, name = _STORED_INPUTS[operation]
    language = payload["input"].get("language")
    if language is not None and (
        not isinstance(language, str) or not language.strip() or len(language) > 35
    ):
        return True
    stored = voice_input_shape(payload["input"], kind)
    if not _stored_input_validator(name).is_valid(stored):
        return True
    # The ingress limit bounds the whole request body, not the revision-sized
    # input document the database stores. Check the exact bytes that will be
    # persisted so a valid UTF-8 request cannot roll back with a 65536 CHECK.
    return not voice_input_fits(voice_input_document(payload["input"], kind))


def _execute(
    provider: VoiceDrafts,
    operation: str,
    request: Request,
    payload: dict[str, Any],
    key: str | None,
) -> JSONResponse:
    request_id = str(request.state.request_id)
    try:
        if operation == "getVoiceDraft":
            body, status = (
                provider.get(
                    request.path_params["draft_id"],
                    request.query_params.get("draft_revision"),
                ),
                200,
            )
        elif operation == "updateVoiceDraftMetadata":
            body = provider.update_metadata(request.path_params["draft_id"], payload, key)
            status = 200
        elif operation == "generateVoiceDraftRevision":
            view, job = provider.generate(
                request.path_params["draft_id"],
                payload,
                request_id=request_id,
                idempotency_key=key,
            )
            body = {
                "object": "nexus.voice_generation",
                "draft": view["draft"],
                "revision": view["revision"],
                "job": public_job(job),
            }
            status = 202
        elif operation == "createVoicePreview":
            body = public_job(
                provider.preview(
                    request.path_params["draft_id"],
                    payload,
                    request_id=request_id,
                    idempotency_key=key,
                )
            )
            status = 202
        elif operation == "validateVoiceDraftRevision":
            body = public_job(
                provider.validate(
                    request.path_params["draft_id"],
                    payload,
                    request_id=request_id,
                    idempotency_key=key,
                )
            )
            status = 202
        else:
            if _stored_input_rejected(operation, payload):
                return _error("invalid_request", request_id, operation)
            draft, job = provider.create(
                "voice_design" if operation == "createVoiceDesign" else "voice_clone",
                payload,
                request_id=request_id,
                idempotency_key=key,
            )
            body = {"object": "nexus.voice_creation", "draft": draft, "job": public_job(job)}
            status = 202
        validate_response(operation, status, {"body": body})
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
    except VoiceInputInvalidError:
        return _error("invalid_request", request_id, operation)
    except VoiceDraftNotFoundError:
        if operation in (
            "getVoiceDraft",
            "updateVoiceDraftMetadata",
            "generateVoiceDraftRevision",
            "createVoicePreview",
            "validateVoiceDraftRevision",
        ):
            return _error("not_found", request_id, operation)
        return _error("outcome_unknown", request_id, operation)
    except PublicJobNotFoundError:
        if operation in (
            "generateVoiceDraftRevision",
            "createVoicePreview",
            "validateVoiceDraftRevision",
        ):
            return _error("not_found", request_id, operation)
        return _error("unavailable", request_id, operation)
    except VoiceDraftStaleRevisionError:
        if operation in (
            "generateVoiceDraftRevision",
            "createVoicePreview",
            "validateVoiceDraftRevision",
        ):
            return _error("conflict", request_id, operation)
        return _error("unavailable", request_id, operation)
    except VoiceDraftNotCandidateError:
        if operation in ("createVoicePreview", "validateVoiceDraftRevision"):
            return _error("not_candidate", request_id, operation)
        return _error("unavailable", request_id, operation)
    except VoiceDraftConflictError, IdempotencyConflictError:
        if operation != "getVoiceDraft":
            return _error("conflict", request_id, operation)
        return _error("unavailable", request_id, operation)
    except (
        AssetNotReadyError,
        ModelBindingError,
        ModelNotPublishedError,
        UnknownWorkspaceError,
        DisabledWorkspaceError,
    ):
        if operation in (
            "createVoiceDesign",
            "createVoiceClone",
            "generateVoiceDraftRevision",
            "createVoicePreview",
            "validateVoiceDraftRevision",
        ):
            return _error("input_unavailable", request_id, operation)
        return _error("unavailable", request_id, operation)
    except JobOutcomeUnknownError:
        if operation in (
            "createVoiceDesign",
            "createVoiceClone",
            "generateVoiceDraftRevision",
            "createVoicePreview",
            "validateVoiceDraftRevision",
        ):
            # With the original key, a same-key replay returns the committed
            # draft and no second Job exists. Without one, any resubmission is
            # a new intent, so the reply must not invite an ordinary retry.
            if key is None:
                return _error("outcome_unknown_unkeyed", request_id, operation)
            return _error("outcome_unknown", request_id, operation)
        return _error("unavailable", request_id, operation)
    except ContractValidationError:
        # The reply we built violates our own contract. Answering 503 would hide
        # an implementation defect behind a retryable outage.
        return _error("contract_violation", request_id, operation)
    except JobAdmissionRejectedError as error:
        # Admission refusals are deterministic facts about the candidate or
        # capacity, not a storage outage. Only Job-submitting operations
        # declare the 422/429 surface; other operations keep the 503 fallback.
        if operation in (
            "createVoiceDesign",
            "createVoiceClone",
            "generateVoiceDraftRevision",
            "createVoicePreview",
            "validateVoiceDraftRevision",
        ):
            if error.code == "job_capacity_exhausted":
                return _error("capacity", request_id, operation)
            if error.code == "voice_draft_not_candidate":
                return _error("not_candidate", request_id, operation)
            if error.code == "job_admission_contended":
                return _error("unavailable", request_id, operation)
            return _error("input_unavailable", request_id, operation)
        return _error("unavailable", request_id, operation)
    except JobRejectedError as error:
        # A refusal carries its reason: only a genuine pre-commit storage outage
        # is retryable; a deterministic refusal or a missing result payload is an
        # implementation defect, and a changed fact is a conflict with current
        # state. Never dress any of them up as a retryable 503.
        if error.code == "storage_rejected":
            return _error("rejected_defect", request_id, operation)
        if error.code == "contract":
            return _error("contract_violation", request_id, operation)
        if operation in (
            "createVoiceDesign",
            "createVoiceClone",
            "generateVoiceDraftRevision",
            "createVoicePreview",
            "validateVoiceDraftRevision",
        ):
            return _error("rejected", request_id, operation)
        # A read or metadata operation never submits a Job, so a conflict-class
        # refusal here cannot describe current state: it is an implementation
        # defect, not the retryable 503 it was previously mislabelled as.
        return _error("contract_violation", request_id, operation)
    except (
        PublicJobUnavailableError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
    ):
        return _error("unavailable", request_id, operation)


def create_voice_drafts_router(
    get_provider: Callable[[], VoiceDrafts | None],
    *,
    allowed_hosts: tuple[str, ...],
    allowed_origins: tuple[str, ...],
) -> APIRouter:
    router = APIRouter(tags=["voice-creation"])

    async def handle(request: Request, operation: str) -> JSONResponse:
        request_id = str(request.state.request_id)
        if rejected_authority(
            request.headers, frozenset(allowed_hosts), frozenset(allowed_origins)
        ):
            return _error("authority_rejected", request_id, operation)
        payload: Any = {}
        try:
            key = _idempotency_key(request)
            pairs = list(request.query_params.multi_items())
            if len(pairs) != len({name for name, _ in pairs}):
                raise ValueError("Duplicate query parameter")
            headers = {}
            for name in ("x-request-id", "idempotency-key"):
                values = request.headers.getlist(name)
                if len(values) > 1:
                    raise ValueError("Duplicate header")
                if values:
                    headers[name] = values[0]
            payload = {}
            if request.method != "GET":
                raw = await _receive(request, 65536, 3)
                payload = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
            envelope = {"path": request.path_params, "query": dict(pairs), "headers": headers}
            if request.method != "GET":
                envelope["body"] = payload
            validate_request(operation, envelope)
        except ContractValidationError, ValueError, TypeError, RecursionError, _TooLargeError:
            if (
                operation == "updateVoiceDraftMetadata"
                and isinstance(payload, dict)
                and set(payload) - {"expected_metadata_revision", "display_name"}
            ):
                return _error("unsupported_parameter", request_id, operation)
            return _error("invalid_request", request_id, operation)
        provider = get_provider()
        if provider is None:
            return _error("unavailable", request_id, operation)
        return await run_in_threadpool(_execute, provider, operation, request, payload, key)

    @router.post("/v1/nexus/voice-designs", operation_id="createVoiceDesign", status_code=202)
    async def create_design(request: Request) -> JSONResponse:
        return await handle(request, "createVoiceDesign")

    @router.post("/v1/nexus/voice-clones", operation_id="createVoiceClone", status_code=202)
    async def create_clone(request: Request) -> JSONResponse:
        return await handle(request, "createVoiceClone")

    @router.get("/v1/nexus/voice-drafts/{draft_id}", operation_id="getVoiceDraft")
    async def get_draft(request: Request) -> JSONResponse:
        return await handle(request, "getVoiceDraft")

    @router.patch("/v1/nexus/voice-drafts/{draft_id}", operation_id="updateVoiceDraftMetadata")
    async def update_metadata(request: Request) -> JSONResponse:
        return await handle(request, "updateVoiceDraftMetadata")

    @router.post(
        "/v1/nexus/voice-drafts/{draft_id}/generations",
        operation_id="generateVoiceDraftRevision",
        status_code=202,
    )
    async def generate_revision(request: Request) -> JSONResponse:
        return await handle(request, "generateVoiceDraftRevision")

    @router.post(
        "/v1/nexus/voice-drafts/{draft_id}/previews",
        operation_id="createVoicePreview",
        status_code=202,
    )
    async def create_preview(request: Request) -> JSONResponse:
        return await handle(request, "createVoicePreview")

    @router.post(
        "/v1/nexus/voice-drafts/{draft_id}/validations",
        operation_id="validateVoiceDraftRevision",
        status_code=202,
    )
    async def validate_revision(request: Request) -> JSONResponse:
        return await handle(request, "validateVoiceDraftRevision")

    return router
