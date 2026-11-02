"""The backend and gateway produce the identical effective request digest."""

from copy import deepcopy

import pytest
from speechnexus_contracts.execution_canonical import effective_execution_request

from speechnexus_backend.speech.infrastructure.transcription_submit import command_digest
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit
from speechnexus_gateway.interfaces.execution_payloads import (
    derived_stream_id_for,
    submit_request_from_payload,
)
from tests.unit.backend.test_tts_durable_intent import execution, plan


@pytest.mark.parametrize("long_id", [False, True])
@pytest.mark.parametrize("deadline", ["2026-10-23T12:01:00+00:00", "2026-10-23T20:01:00+08:00"])
def test_effective_request_agrees_with_gateway(long_id: bool, deadline: str) -> None:
    request = deepcopy(build_tts_submit(plan(), execution()))
    request["deadline_at"] = deadline
    if long_id:
        request["operation_id"] = "operation-" + "a" * 100
        request["attempt_id"] = "attempt-" + "b" * 100
    gateway = submit_request_from_payload(request, output_stream_id=derived_stream_id_for(request))
    assert effective_execution_request(request) == gateway.effective_request
    assert command_digest(effective_execution_request(request)) == gateway.request_digest
