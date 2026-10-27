"""Actual draft POST and pinned GET; uncertainty never becomes absence or a new key."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.binding_defaults_http import (
    validate_request as validate_defaults_request,
)
from speechnexus_contracts.binding_defaults_http import (
    validate_response as validate_defaults_response,
)
from speechnexus_contracts.binding_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from speechnexus_backend.speech.application.binding_default_drafts import BindingDefaultDrafts
from speechnexus_backend.speech.application.binding_documents import encode_json, parse_integer
from speechnexus_backend.speech.application.binding_drafts import BindingDrafts
from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationFacts,
)
from speechnexus_backend.speech.domain.binding_defaults import BindingDefaultsDraftResult
from speechnexus_backend.speech.domain.binding_drafts import BindingUnavailableError
from speechnexus_backend.speech.interfaces.binding_default_drafts import (
    defaults_to_wire,
    prepare_binding_defaults_draft,
)
from speechnexus_backend.speech.interfaces.binding_drafts import (
    draft_to_wire,
    prepare_binding_draft,
    revision_to_wire,
)
from speechnexus_backend.speech.interfaces.http.binding_errors import (
    CREATE,
    READ,
    READ_DEFAULTS,
    binding_error,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    CREATE_PATH,
    DEFAULTS_PATH,
    READ_PATH,
)

type BindingProvider = BindingDrafts | BindingDefaultDrafts


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _create(
    provider: BindingProvider,
    envelope: dict[str, Any],
    raw: bytes,
    request_id: str,
    *,
    producer: bool = False,
) -> Response:
    try:
        body = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_int=parse_integer,
        )
        envelope = {**envelope, "body": body}
        (validate_defaults_request if producer else validate_request)(CREATE, envelope)
    except ValueError, TypeError, RecursionError:
        return binding_error(
            "invalid_binding_request", request_id, operation=CREATE, producer=producer
        )
    try:
        prepare = prepare_binding_defaults_draft if producer else prepare_binding_draft
        command = prepare(body, envelope["headers"].get("idempotency-key"))
    except ContractValidationError:
        return binding_error(
            "invalid_binding_definition", request_id, operation=CREATE, producer=producer
        )
    try:
        created = provider.create(command)
        if isinstance(created, BindingDefaultsDraftResult) != producer:
            raise BindingUnavailableError
        if (
            isinstance(created, BindingDefaultsDraftResult)
            and created.draft is not None
            and (
                (created.snapshot is None) != (command.parameter_defaults is None)
                or (
                    created.snapshot is not None
                    and created.snapshot.parameters != command.parameter_defaults
                )
            )
        ):
            raise BindingUnavailableError
        result = created.binding if isinstance(created, BindingDefaultsDraftResult) else created
        if result.outcome == "conflict":
            if result.error_code != "idempotency_conflict":
                raise BindingUnavailableError
            return binding_error(
                "idempotency_conflict", request_id, operation=CREATE, producer=producer
            )
        if result.outcome not in ("created", "replayed") or result.draft is None:
            raise BindingUnavailableError
        if (
            result.draft.binding_id != command.binding_id
            or result.draft.definition != command.definition
        ):
            raise BindingUnavailableError
        wire = draft_to_wire(result.draft)
        (validate_defaults_response if producer else validate_response)(CREATE, 201, {"body": wire})
        return Response(
            encode_json(wire, max_bytes=1048576),
            status_code=201,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        # No raw driver/validation diagnostics or caller data leave this boundary.
        return binding_error(
            "binding_result_unknown", request_id, operation=CREATE, producer=producer
        )


def _read(
    provider: BindingProvider,
    facts: BindingPublicationFacts | None,
    envelope: dict[str, Any],
    request_id: str,
) -> Response:
    try:
        validate_request(READ, envelope)
    except ContractValidationError, ValueError, TypeError, RecursionError:
        return binding_error("invalid_binding_request", request_id, operation=READ)
    try:
        identity, revision = envelope["path"]["binding_id"], envelope["path"]["revision"]
        draft = provider.get(identity, revision)
        if draft is None:
            return binding_error("binding_not_found", request_id, operation=READ)
        if draft.binding_id != identity or draft.revision != revision:
            raise BindingUnavailableError
        if facts is None:
            # Without the read-only publication projection the endpoint cannot
            # distinguish a draft from a published revision. Fail closed rather
            # than report ``draft`` for a revision already in use.
            return binding_error("binding_unavailable", request_id, operation=READ)
        wire = revision_to_wire(draft, facts.project(draft))
        validate_response(READ, 200, {"body": wire})
        return Response(
            encode_json(wire, max_bytes=1048576),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return binding_error("binding_unavailable", request_id, operation=READ)


def _read_defaults(
    provider: BindingDefaultDrafts,
    envelope: dict[str, Any],
    request_id: str,
) -> Response:
    try:
        validate_defaults_request(READ_DEFAULTS, envelope)
    except ContractValidationError, ValueError, TypeError, RecursionError:
        return binding_error("invalid_binding_request", request_id, operation=READ_DEFAULTS)
    try:
        identity, revision = envelope["path"]["binding_id"], envelope["path"]["revision"]
        parent = provider.get(identity, revision)
        snapshot = provider.get_defaults(identity, revision)
        if parent is not None and (parent.binding_id != identity or parent.revision != revision):
            raise BindingUnavailableError
        if snapshot is None:
            return binding_error("binding_defaults_not_found", request_id, operation=READ_DEFAULTS)
        if parent is None or (
            snapshot.binding_id,
            snapshot.revision,
            snapshot.revision_id,
            snapshot.created_at_us,
            snapshot.document_version,
        ) != (
            parent.binding_id,
            parent.revision,
            parent.revision_id,
            parent.created_at_us,
            parent.document_version,
        ):
            raise BindingUnavailableError
        wire = defaults_to_wire(snapshot)
        validate_defaults_response(READ_DEFAULTS, 200, {"body": wire})
        return Response(
            encode_json(wire, max_bytes=1048576),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return binding_error("binding_unavailable", request_id, operation=READ_DEFAULTS)


def create_binding_router(
    get_provider: Callable[[], BindingProvider | None],
    *,
    get_admin_reader: Callable[[], BindingPublicationFacts | None],
    producer: bool = False,
) -> APIRouter:
    router = APIRouter(tags=["bindings"])

    async def handle(request: Request, *, operation: str) -> Response:
        request_id = str(request.state.request_id)
        provider = get_provider()
        if provider is None:
            return binding_error(
                "binding_result_unknown" if operation == CREATE else "binding_unavailable",
                request_id,
                operation=operation,
                producer=producer,
            )
        try:
            pairs = list(request.query_params.multi_items())
            if len(pairs) != len({key for key, _ in pairs}):
                raise ContractValidationError("Ambiguous query")
            headers: dict[str, str] = {}
            for name in ("x-request-id", "idempotency-key"):
                values = request.headers.getlist(name)
                if len(values) > 1:
                    raise ContractValidationError("Ambiguous header")
                if values:
                    headers[name] = values[0]
            envelope = {"path": request.path_params, "query": dict(pairs), "headers": headers}
            if operation == CREATE:
                raw = await request.body()  # BindingIngress has bounded the actual bytes and time.
                return await run_in_threadpool(
                    _create,
                    provider,
                    envelope,
                    raw,
                    request_id,
                    producer=producer,
                )
            lengths = request.headers.getlist("content-length")
            if (
                len(lengths) > 1
                or (lengths and lengths != ["0"])
                or request.headers.getlist("transfer-encoding")
            ):
                raise ContractValidationError("A fixed binding read has no body")
            async with asyncio.timeout(1):
                for _ in range(8):
                    message = await request.receive()
                    if message["type"] != "http.request" or message.get("body"):
                        raise ContractValidationError("A fixed binding read has no body")
                    if not message.get("more_body", False):
                        break
                else:
                    raise ContractValidationError("Read receive budget exceeded")
            if operation == READ_DEFAULTS:
                return await run_in_threadpool(
                    _read_defaults,
                    cast(BindingDefaultDrafts, provider),
                    envelope,
                    request_id,
                )
            return await run_in_threadpool(
                _read, provider, get_admin_reader(), envelope, request_id
            )
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return binding_error(
                "invalid_binding_request", request_id, operation=operation, producer=producer
            )

    async def create(request: Request) -> Response:
        return await handle(request, operation=CREATE)

    async def read(request: Request) -> Response:
        return await handle(request, operation=READ)

    async def read_defaults(request: Request) -> Response:
        return await handle(request, operation=READ_DEFAULTS)

    router.add_api_route(CREATE_PATH, create, methods=["POST"], operation_id=CREATE)
    router.add_api_route(READ_PATH, read, methods=["GET"], operation_id=READ)
    if producer:
        router.add_api_route(
            DEFAULTS_PATH, read_defaults, methods=["GET"], operation_id=READ_DEFAULTS
        )
    return router
