"""C-owned executable failures must be accepted by the generated HTTP boundary."""

from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench_http import validate_request, validate_response

OPERATIONS = (
    "createProject",
    "getProject",
    "updateProject",
    "createTranscript",
    "getTranscript",
    "createTranscriptRevision",
    "getTranscriptRevision",
    "getWorkbenchStatus",
    "listProjects",
    "listTranscripts",
    "workbenchCreateDeletionPlan",
    "workbenchCreateDeletion",
    "workbenchGetDeletion",
)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("status", [400, 408, 413, 500, 503])
def test_real_boundary_failures_are_declared_and_validate_body(operation: str, status: int) -> None:
    value = {
        "headers": {"X-Request-ID": "synthetic-request"},
        "body": {
            "error": {
                "code": "invalid_request",
                "type": "invalid_request_error",
                "message": "Request rejected.",
                "param": None,
                "request_id": "synthetic-request",
                "retryable": False,
            }
        },
    }
    assert validate_response(operation, status, value) is value
    with pytest.raises(ContractValidationError):
        validate_response(operation, status, {"body": {"detail": "unsafe"}})


@pytest.mark.parametrize(
    "operation,status",
    [
        ("createProject", 415),
        ("updateProject", 415),
        ("createTranscript", 415),
        ("createTranscriptRevision", 415),
        ("createTranscript", 410),
        ("createProject", 404),
        ("workbenchCreateDeletionPlan", 415),
        ("workbenchCreateDeletion", 415),
    ],
)
def test_media_and_expired_source_failures_remain_explicit(operation: str, status: int) -> None:
    validate_response(
        operation,
        status,
        {
            "body": {
                "error": {
                    "code": "source_expired",
                    "type": "invalid_request_error",
                    "message": "Request rejected.",
                    "param": None,
                    "request_id": "synthetic-request",
                    "retryable": False,
                }
            }
        },
    )


@pytest.mark.parametrize(
    "operation,value",
    [
        (
            "workbenchCreateDeletionPlan",
            {
                "body": {
                    "target": {
                        "owner": "workbench",
                        "kind": "project",
                        "resource": {"id": "project-one", "revision": "1"},
                    },
                    "mode": "ordinary",
                },
                "headers": {"idempotency-key": "plan-one"},
            },
        ),
        (
            "workbenchCreateDeletion",
            {
                "body": {
                    "plan_id": "plan-one",
                    "expected_plan_revision": "1",
                    "plan_digest": "sha256:" + "a" * 64,
                    "confirm_targets": [
                        {
                            "owner": "workbench",
                            "kind": "project",
                            "resource": {"id": "project-one", "revision": "1"},
                        }
                    ],
                },
                "headers": {"idempotency-key": "create-one"},
            },
        ),
        ("workbenchGetDeletion", {"path": {"deletion_id": "deletion-one"}}),
    ],
)
def test_deletion_scope_is_explicit_optional_query(operation: str, value: dict[str, Any]) -> None:
    value["query"] = {"workspace_id": "workspace-one"}
    assert validate_request(operation, value) is value


@pytest.mark.parametrize(
    "operation",
    ["createMeeting", "getMeeting", "sealMeeting", "createMeetingMinutes", "getMeetingMinutes"],
)
@pytest.mark.parametrize("status", [400, 408, 413, 500, 503])
def test_meeting_http_declares_bounded_ingress_and_storage_errors(
    operation: str, status: int
) -> None:
    value = {
        "body": {
            "error": {
                "code": "synthetic_failure",
                "type": "api_error",
                "message": "Synthetic failure.",
                "param": None,
                "request_id": "request-demo",
                "retryable": False,
            }
        }
    }
    assert validate_response(operation, status, value) is value
    with pytest.raises(ContractValidationError):
        validate_response(operation, status, {"body": {"detail": "unsafe"}})


@pytest.mark.parametrize(
    "enabled,profile,valid",
    [
        (True, {"template_revision": "7", "llm_model": "approved-model"}, True),
        (False, None, True),
        (False, {"template_revision": "7", "llm_model": "approved-model"}, False),
        (True, {"template_revision": 7, "llm_model": "approved-model"}, False),
        (
            True,
            {"template_revision": "7", "llm_model": "approved-model", "endpoint": "private"},
            False,
        ),
    ],
)
def test_minutes_profile_discovery_is_public_pinned_and_absent_when_disabled(
    enabled: bool,
    profile: Any,
    valid: bool,
) -> None:
    value = {
        "body": {
            "object": "nexus.workbench_status",
            "workbench_enabled": True,
            "llm_enabled": enabled,
            "shared_deployment_notice": "shared_deployment",
            "capabilities": [],
            "minutes_profile": profile,
        }
    }
    if valid:
        assert validate_response("getWorkbenchStatusV2", 200, value) is value
    else:
        with pytest.raises(ContractValidationError):
            validate_response("getWorkbenchStatusV2", 200, value)


def test_minutes_expected_source_revision_is_validated_on_wire() -> None:
    value = {
        "path": {"meeting_id": "meeting-demo"},
        "body": {
            "template_revision": "7",
            "expected_source_revision": "2",
            "llm_model": "approved-model",
        },
    }
    assert validate_request("createMeetingMinutes", value) is value
    value["body"]["expected_source_revision"] = "0"
    with pytest.raises(ContractValidationError):
        validate_request("createMeetingMinutes", value)
