"""Four generated HTTP operations over the private book and chapter snapshot."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.books import (
    OrdinalConflictError,
    WorkbenchBooks,
)
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError
from speechnexus_backend.workbench.interfaces.book_views import book_payload, chapter_payload
from speechnexus_backend.workbench.interfaces.cursors import CursorCodec, InvalidCursorError
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    default_limit,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error

OPERATIONS = frozenset(
    {"listBooks", "listChapters", "createBook", "getBook", "createChapter", "getChapter"}
)


def create_book_router(
    service: WorkbenchBooks | None,
    *,
    default_workspace_id: str,
    cursor: CursorCodec,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    parse("BookCreateRequest", {"title": "synthetic"})
    parse("ChapterCreateRequest", {"title": "synthetic", "ordinal": 0, "text": "synthetic"})
    document = contract()
    limits = {
        op["operationId"]: default_limit(document, op)
        for item in document["paths"].values()
        for op in item.values()
        if op["operationId"] in {"listBooks", "listChapters"}
    }
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        if service is None:
            raise IngressError("workbench_unavailable")
        path = value["path"]
        if operation == "listBooks":
            query = value["query"]
            parent = path["project_id"]
            after = (
                None
                if "cursor" not in query
                else cursor.decode(query["cursor"], kind="books", parent_id=parent)
            )
            limit = int(query["limit"]) if "limit" in query else limits[operation]
            books_page = await service.list_books(
                project_id=parent,
                workspace_id=default_workspace_id,
                limit=limit,
                after=after,
            )
            next_position = books_page.next_position
            return 200, {
                "object": "nexus.list",
                "data": [book_payload(item) for item in books_page.items],
                "has_more": next_position is not None,
                "next_cursor": None if next_position is None else cursor.encode(next_position),
            }
        if operation == "listChapters":
            query = value["query"]
            parent = path["book_id"]
            after = (
                None
                if "cursor" not in query
                else cursor.decode(query["cursor"], kind="chapters", parent_id=parent)
            )
            limit = int(query["limit"]) if "limit" in query else limits[operation]
            chapters_page = await service.list_chapters(
                book_id=parent, workspace_id=default_workspace_id, limit=limit, after=after
            )
            next_position = chapters_page.next_position
            return 200, {
                "object": "nexus.list",
                "data": [chapter_payload(item) for item in chapters_page.items],
                "has_more": next_position is not None,
                "next_cursor": None if next_position is None else cursor.encode(next_position),
            }
        if operation == "getBook":
            return 200, book_payload(
                await service.get_book(book_id=path["book_id"], workspace_id=default_workspace_id)
            )
        if operation == "getChapter":
            return 200, chapter_payload(
                await service.get_chapter(
                    chapter_id=path["chapter_id"], workspace_id=default_workspace_id
                )
            )
        body = value["body"]
        if operation == "createBook":
            return 201, book_payload(
                await service.create_book(
                    project_id=path["project_id"],
                    workspace_id=default_workspace_id,
                    title=body["title"],
                    source_text=body.get("source_text"),
                    key=value["headers"].get("idempotency-key"),
                )
            )
        return 201, chapter_payload(
            await service.create_chapter(
                book_id=path["book_id"],
                workspace_id=default_workspace_id,
                title=body["title"],
                ordinal=body["ordinal"],
                text=body["text"],
                key=value["headers"].get("idempotency-key"),
            )
        )

    def endpoint(operation: str, has_body: bool) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            rid = request_id(request)
            try:
                value = await read_envelope(
                    request,
                    operation,
                    has_body=has_body,
                    max_json_bytes=max_json_bytes,
                    body_timeout_seconds=body_timeout_seconds,
                )
                status, body = await dispatch(operation, value)
                headers = {"X-Request-ID": rid, "Cache-Control": "no-store"}
                validate_response(operation, status, {"body": body, "headers": headers})
                return JSONResponse(body, status_code=status, headers=headers)
            except IngressError as error:
                return _error(error.code, rid, operation)
            except InvalidCursorError:
                return _error("invalid_cursor", rid, operation)
            except ResourceNotFoundError:
                return _error("resource_not_found", rid, operation)
            except IdempotencyConflictError:
                return _error("idempotency_conflict", rid, operation)
            except OrdinalConflictError:
                return _error("ordinal_conflict", rid, operation)
            except InvalidVersionChangeError:
                return _error("invalid_change", rid, operation)
            except VersionStoreError as error:
                return _error(
                    "outcome_unknown" if error.commit_unknown else "store_unavailable",
                    rid,
                    operation,
                )
            except Exception:
                # CancelledError is a BaseException and must reach the ASGI host.
                return _error("internal_error", rid, operation)

        return handle

    for path, item in document["paths"].items():
        for method, operation in item.items():
            name = operation["operationId"]
            if name in OPERATIONS:
                router.add_api_route(
                    path,
                    endpoint(name, "requestBody" in operation),
                    methods=[method.upper()],
                    operation_id=name,
                    response_model=None,
                )
    return router
