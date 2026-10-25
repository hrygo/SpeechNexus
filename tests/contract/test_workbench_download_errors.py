"""Public downloads declare stable JSON failures before a business route ships."""

import pytest
from speechnexus_contracts.workbench_http import validate_response


@pytest.mark.parametrize("operation", ["downloadSavedAsset", "downloadBookExport"])
@pytest.mark.parametrize("status", [400, 408, 413, 416, 500, 503])
def test_download_declared_failures(operation: str, status: int) -> None:
    validate_response(
        operation,
        status,
        {
            "body": {
                "error": {
                    "code": "download_unavailable",
                    "type": "api_error",
                    "message": "Content is unavailable.",
                    "param": None,
                    "request_id": "synthetic-download",
                    "retryable": False,
                }
            },
            "headers": {"X-Request-ID": "synthetic-download", "Content-Range": "bytes */8"},
        },
    )
