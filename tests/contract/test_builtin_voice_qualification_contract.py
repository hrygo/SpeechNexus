"""The private probe authorization is not a public voice qualification."""

from copy import deepcopy
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.builtin_voice_qualification import parse


def authorization() -> dict[str, object]:
    digest = "sha256:" + "1" * 64
    return {
        "purpose": "builtin_voice_qualification",
        "authorization_id": "auth-test",
        "source_approval_id": "source-test",
        "voice_id": "voice-test",
        "voice_revision": "1",
        "voice_revision_id": "voice-rev",
        "native_speaker_id": "Vivian",
        "binding_id": "binding-test",
        "binding_revision_id": "binding-rev",
        "manifest_id": "manifest-test",
        "policy_revision_id": "policy-test",
        "host_node_id": "host-test",
        "gateway_id": "gateway-test",
        "instance_id": "instance-test",
        "instance_generation": 1,
        "operation_id": "operation-test",
        "attempt_id": "attempt-test",
        "command_digest": digest,
        "gateway_request_digest": digest,
        "admission_request_digest": digest,
        "business_request_digest": digest,
        "expires_at_us": 100,
        "manifest_digest": digest,
        "report_id": "report-test",
        "report_digest": digest,
        "model_artifact_digest": digest,
        "image_digest": digest,
        "template_id": "template-test",
        "template_config_digest": digest,
        "template_digest": digest,
        "controller_artifact_digest": digest,
        "controller_revision": "controller-rev",
        "native_authority": True,
        "database": "database-test",
        "schema": "schema-test",
        "installation_id": "installation-test",
    }


def test_private_contract_rejects_unbounded_or_forged_shape() -> None:
    value = authorization()
    parse("BuiltinVoiceQualificationAuthorization", value)
    for patch in (
        {"purpose": "tts_file"},
        {"native_speaker_id": "other"},
        {"native_authority": False},
        {"instance_generation": 0},
        {"command_digest": "bad"},
        {"passed": True},
    ):
        candidate: dict[str, Any] = deepcopy(value)
        candidate.update(patch)
        with pytest.raises(ContractValidationError):
            parse("BuiltinVoiceQualificationAuthorization", candidate)
