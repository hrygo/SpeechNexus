"""Public envelope for framework 405 replies without a resource-specific mapper."""

from speechnexus_contracts import parse
from starlette.responses import JSONResponse


def method_not_allowed_error(request_id: str, headers: dict[str, str]) -> JSONResponse:
    return JSONResponse(
        status_code=405,
        headers={"Cache-Control": "no-store", **headers},
        content=parse(
            "ErrorResponse",
            {
                "error": {
                    "code": "method_not_allowed",
                    "type": "invalid_request_error",
                    "message": "The path does not accept this HTTP method.",
                    "param": None,
                    "request_id": request_id,
                    "retryable": False,
                }
            },
        ),
    )
