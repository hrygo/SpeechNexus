"""Public catalogue consumes the canonical speech HTTP surface."""

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.profile_public_http import validate_response

SUMMARY = {
    "id": "synthetic-profile",
    "revision": "1",
    "display_name": "Synthetic",
    "kind": "asr",
    "release_tier": "experimental",
    "execution_classes": ["batch"],
}
LIST = {"object": "nexus.list", "data": [SUMMARY], "has_more": False, "next_cursor": None}
ERROR = {
    "error": {
        "code": "profile_unavailable",
        "type": "service_unavailable_error",
        "message": "Fixed profile metadata is unavailable.",
        "param": None,
        "request_id": "synthetic-request",
        "retryable": True,
    }
}


def test_v1_list_keeps_the_original_shape_without_the_family_pointer() -> None:
    assert validate_response("listProfiles", 200, {"body": LIST}) is not None
    with pytest.raises(ContractValidationError):
        validate_response(
            "listProfiles", 200, {"body": {**LIST, "data": [{**SUMMARY, "current_revision": None}]}}
        )


@pytest.mark.parametrize("pointer", [None, "1", "9999999999999999999"])
def test_v2_list_accepts_only_the_nullable_family_pointer(pointer: str | None) -> None:
    body = {**LIST, "data": [{**SUMMARY, "current_revision": pointer}]}
    assert validate_response("listPublishedProfiles", 200, {"body": body}) is not None
    for invalid in ("latest", 1):
        changed = {**LIST, "data": [{**SUMMARY, "current_revision": invalid}]}
        with pytest.raises(ContractValidationError):
            validate_response("listPublishedProfiles", 200, {"body": changed})
    missing = {**LIST, "data": [SUMMARY]}
    with pytest.raises(ContractValidationError):
        validate_response("listPublishedProfiles", 200, {"body": missing})


@pytest.mark.parametrize("status", [400, 401, 403, 503])
def test_v2_declared_error_envelopes_are_closed(status: int) -> None:
    validate_response("listPublishedProfiles", status, {"body": ERROR})
    with pytest.raises(ContractValidationError):
        validate_response(
            "listPublishedProfiles", status, {"body": {**ERROR, "private": "not-public"}}
        )
