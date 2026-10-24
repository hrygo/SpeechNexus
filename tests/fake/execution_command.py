"""Frozen synthetic machine-contract commands shared by B input tests."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import SubmissionIdentity, SubmitRequest


def command_example(index: int = 1) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "contracts/internal/execution-command.schema.json").read_text())
    return cast(dict[str, Any], copy.deepcopy(schema["examples"][index]))


def original_request(command: dict[str, Any] | None = None) -> SubmitRequest:
    value = command_example() if command is None else command
    return SubmitRequest(
        identity=SubmissionIdentity(
            submit_intent_id="intent-input",
            lease_id="lease-input",
            operation_id="operation-input",
            attempt_id="attempt-input",
            stage_id="stage-input",
            workspace_id="ws-demo",
            pool_id="asr-batch",
            host_node_id="node-input",
            gateway_id="gateway-input",
            instance_id="instance-input",
            instance_generation=1,
            binding_revision_id="binding-input",
            policy_revision_id="policy-input",
            voice_revision_id=None,
            fencing_token=1,
            command_digest=command_digest(cast(JsonValue, value)),
            deadline_at=datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1),
            max_output_audio_ms=None,
            sample_rate_hz=None,
        ),
        command=cast(JsonValue, value),
    )
