"""Source-validated private intent preparation; no HTTP registration or authority."""

from __future__ import annotations

import uuid
from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.profile_lifecycle_http import validate_request

from speechnexus_backend.speech.application.profile_documents import encode_document
from speechnexus_backend.speech.application.profile_lifecycle import check_lifecycle_command
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.domain.profile_lifecycle import (
    ProfileLifecycleCommand,
    ProfileLifecycleOperation,
)


def _prepare(
    operation: ProfileLifecycleOperation,
    profile_id: str,
    revision: str,
    request: Any,
    idempotency_key: str | None,
) -> ProfileLifecycleCommand:
    try:
        headers = {} if idempotency_key is None else {"Idempotency-Key": idempotency_key}
        source_operation = (
            "publishProfileRevision"
            if operation == "publish"
            else "activatePublishedProfileRevision"
        )
        validate_request(
            source_operation,
            {
                "path": {"profile_id": profile_id, "revision": revision},
                "headers": headers,
                "body": request,
            },
        )
        command = ProfileLifecycleCommand(
            operation,
            profile_id,
            revision,
            document_digest((idempotency_key or uuid.uuid4().hex).encode()),
            encode_document(
                {
                    "operation": operation,
                    "profile_id": profile_id,
                    "revision": revision,
                    "body": request,
                }
            ),
        )
        check_lifecycle_command(command)
        return command
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        raise ContractValidationError("Invalid profile lifecycle intent") from None


def prepare_profile_publication(
    profile_id: str, revision: str, request: Any, idempotency_key: str | None = None
) -> ProfileLifecycleCommand:
    return _prepare("publish", profile_id, revision, request, idempotency_key)


def prepare_profile_activation(
    profile_id: str, revision: str, request: Any, idempotency_key: str | None = None
) -> ProfileLifecycleCommand:
    return _prepare("activate", profile_id, revision, request, idempotency_key)
