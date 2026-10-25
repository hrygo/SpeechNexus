"""An additive workbench version preserves strict v1 consumers and explicit pins."""

from copy import deepcopy
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench_http import validate_request, validate_response

CREATE: dict[str, Any] = {
    "path": {"chapter_id": "chapter-demo"},
    "body": {
        "ordinal": 0,
        "text": "Synthetic narration.",
        "voice": {"id": "voice-demo", "revision": "1"},
        "model": "synthetic-model",
    },
}
BINDING = {"id": "binding-demo", "revision": "3"}
UNIT: dict[str, Any] = {
    "id": "unit-demo",
    "object": "nexus.render_unit",
    "chapter_id": "chapter-demo",
    "ordinal": 0,
    "revision": "1",
    "text": "Synthetic narration.",
    "voice": {"id": "voice-demo", "revision": "1"},
    "model": "synthetic-model",
    "status": "pending",
    "updated_at": "2026-10-25T00:00:00Z",
}
STATUS: dict[str, Any] = {
    "object": "nexus.workbench_status",
    "workbench_enabled": True,
    "llm_enabled": True,
    "shared_deployment_notice": "shared_deployment",
    "capabilities": [],
}


def test_v1_create_keeps_the_original_binding_free_request_and_rejects_new_fields() -> None:
    assert validate_request("createRenderUnit", CREATE) is CREATE
    bound = deepcopy(CREATE)
    bound["body"]["model_binding"] = deepcopy(BINDING)
    with pytest.raises(ContractValidationError):
        validate_request("createRenderUnit", bound)


def test_v2_create_requires_the_callers_exact_binding_without_a_default() -> None:
    with pytest.raises(ContractValidationError):
        validate_request("createRenderUnitV2", CREATE)
    bound = deepcopy(CREATE)
    bound["body"]["model_binding"] = deepcopy(BINDING)
    assert validate_request("createRenderUnitV2", bound) is bound
    bound["body"]["model_binding"]["revision"] = 3
    with pytest.raises(ContractValidationError):
        validate_request("createRenderUnitV2", bound)


@pytest.mark.parametrize(
    "operation,status",
    [("createRenderUnit", 201), ("getRenderUnit", 200), ("updateRenderUnit", 200)],
)
def test_existing_render_responses_stay_closed_and_v2_pins_are_required(
    operation: str, status: int
) -> None:
    legacy = {"body": UNIT}
    bound = {"body": {**UNIT, "model_binding": BINDING}}
    assert validate_response(operation, status, legacy) is legacy
    with pytest.raises(ContractValidationError):
        validate_response(operation, status, bound)
    assert validate_response(operation + "V2", status, bound) is bound
    with pytest.raises(ContractValidationError):
        validate_response(operation + "V2", status, legacy)


def test_v1_status_rejects_the_profile_and_v2_validates_the_frozen_configuration() -> None:
    legacy = {"body": STATUS}
    configured = {
        "body": {
            **STATUS,
            "minutes_profile": {"template_revision": "7", "llm_model": "approved-model"},
        }
    }
    assert validate_response("getWorkbenchStatus", 200, legacy) is legacy
    with pytest.raises(ContractValidationError):
        validate_response("getWorkbenchStatus", 200, configured)
    assert validate_response("getWorkbenchStatusV2", 200, configured) is configured
    disabled = deepcopy(configured)
    disabled["body"]["llm_enabled"] = False
    with pytest.raises(ContractValidationError):
        validate_response("getWorkbenchStatusV2", 200, disabled)


def test_generation_acceptance_keeps_its_nested_render_unit_version() -> None:
    legacy: dict[str, Any] = {
        "body": {
            "render_unit": UNIT,
            "speech_job_id": "job-demo",
            "accepted_at": "2026-10-25T00:00:00Z",
        }
    }
    bound = deepcopy(legacy)
    bound["body"]["render_unit"]["model_binding"] = BINDING
    assert validate_response("generateRenderUnit", 202, legacy) is legacy
    with pytest.raises(ContractValidationError):
        validate_response("generateRenderUnit", 202, bound)
    assert validate_response("generateRenderUnitV2", 202, bound) is bound
    with pytest.raises(ContractValidationError):
        validate_response("generateRenderUnitV2", 202, legacy)
