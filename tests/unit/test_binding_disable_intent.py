"""Fixed identities for a future durable binding-disable coordinator."""

from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

import pytest
from speechnexus_contracts.execution import parse

from speechnexus_backend.speech.domain.binding_disable_intent import (
    DisableCancellationIntent,
    DisableExecutionTarget,
)
from speechnexus_backend.speech.domain.binding_lifecycle import BindingRetirementCommand


def test_disable_intent_retains_the_full_original_command_and_revision() -> None:
    command = BindingRetirementCommand(
        binding_id="binding-1",
        revision="2",
        request_id="request-1",
        actor_type="anonymous",
        credential_label=None,
        key_digest="sha256:" + "a" * 64,
        intent_digest="sha256:" + "b" * 64,
        mode="disable",
        reason="emergency",
    )
    intent = DisableCancellationIntent("event-1", "revision-id-2", command)
    assert intent.command == command
    assert intent.revision_id == "revision-id-2"
    with pytest.raises(FrozenInstanceError):
        intent.revision_id = "revision-id-3"  # type: ignore[misc]
    with pytest.raises(ValueError):
        DisableCancellationIntent("event-2", "revision-id-2", replace(command, mode="retire"))


def test_abort_target_preserves_full_identity_and_matches_gateway_contract() -> None:
    target = DisableExecutionTarget(
        submit_intent_id="submit-1",
        operation_id="operation-1",
        attempt_id="attempt-1",
        lease_id="lease-1",
        stage_id="stage-1",
        workspace_id="workspace-1",
        pool_id="asr-batch",
        gateway_id="gateway-1",
        host_node_id="node-1",
        instance_id="instance-1",
        instance_generation=7,
        binding_revision_id="revision-id-2",
        policy_revision_id="policy-1",
        voice_revision_id=None,
        fencing_token=9,
        command_digest="sha256:" + "c" * 64,
        wait_timeout_ms=1000,
    )
    payload = target.abort_payload()
    assert parse("AbortExecutionRequest", payload) == {
        "operation_id": "operation-1",
        "attempt_id": "attempt-1",
        "fencing_token": 9,
        "reason": "resource_force_disabled",
        "wait_timeout_ms": 1000,
    }
    assert target.submit_intent_id == "submit-1"
    assert target.lease_id == "lease-1"
    assert target.instance_generation == 7
    assert target.binding_revision_id == "revision-id-2"


@pytest.mark.parametrize(
    "field,value",
    [
        ("submit_intent_id", ""),
        ("gateway_id", ""),
        ("binding_revision_id", ""),
        ("fencing_token", 0),
        ("fencing_token", True),
        ("instance_generation", 0),
        ("wait_timeout_ms", 99),
        ("wait_timeout_ms", 60001),
    ],
)
def test_abort_target_rejects_missing_or_invalid_identity(field: str, value: object) -> None:
    target = DisableExecutionTarget(
        submit_intent_id="submit-1",
        operation_id="operation-1",
        attempt_id="attempt-1",
        lease_id="lease-1",
        stage_id="stage-1",
        workspace_id="workspace-1",
        pool_id="asr-batch",
        gateway_id="gateway-1",
        host_node_id="node-1",
        instance_id="instance-1",
        instance_generation=7,
        binding_revision_id="revision-id-2",
        policy_revision_id="policy-1",
        voice_revision_id=None,
        fencing_token=9,
        command_digest="sha256:" + "c" * 64,
        wait_timeout_ms=1000,
    )
    with pytest.raises(ValueError):
        replace(target, **{field: cast(Any, value)})
