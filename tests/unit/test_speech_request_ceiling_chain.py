"""Public HTTP and production TTS adapters, with synthetic storage/engine facts.

This is deterministic assembly evidence, not a PostgreSQL factory or real engine
qualification. The actual command consumer must see the HTTP request's ceiling.
"""

from dataclasses import asdict, replace
from typing import Any, cast

import pytest
from speechnexus_contracts.execution_models import SubmitExecutionRequest

from speechnexus_backend.speech.application.speech_synthesis import SpeechLimits, SpeechSynthesis
from speechnexus_backend.speech.domain.speech_synthesis import SpeechPlan
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.gateway_synthesis import GatewaySynthesizer
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit
from speechnexus_gateway.domain.tts import (
    BuiltinVoiceBinding,
    TtsAudio,
    TtsCallError,
    TtsExecutionTarget,
    TtsPolicy,
)
from speechnexus_gateway.interfaces.execution_payloads import (
    derived_stream_id_for,
    submit_request_from_payload,
)
from speechnexus_gateway.interfaces.file_tts_command import CommandFileTts
from tests.unit.backend.test_speech_http import BINDING, REQUEST, VOICE, Facts, client
from tests.unit.backend.test_tts_durable_intent import Gateway, execution
from tests.unit.gateway.test_file_tts_command import Encoder, Store, Synthesizer


@pytest.mark.parametrize("delivery", ["pcm", "wav", "mp3"])
@pytest.mark.parametrize("samples", [2400, 2401, 4800])
@pytest.mark.parametrize("request_ms", [None, 100])
async def test_public_request_reaches_production_submit_and_gateway(
    delivery: str, samples: int, request_ms: int | None
) -> None:
    effective_ms = 600_000 if request_ms is None else request_ms
    accepted = samples <= effective_ms * 24
    policy = TtsPolicy("policy-test", "Vivian")

    class Published(Facts):
        def voice_execution_authority(self, voice: Any) -> tuple[str, str]:
            return policy.digest, "Vivian"

    class Source(Synthesizer):
        async def synthesize(self, request: Any, policy: TtsPolicy, *, cancelled: Any) -> TtsAudio:
            self.calls.append((request, policy))
            return TtsAudio(
                request.request_id, "audio/wav", b"\x00\x00" * samples, 24000, 1, policy.digest
            )

    source = Source(
        target=TtsExecutionTarget(
            "host-test", "gateway-test", "tts-batch", "instance-test", 1, BINDING.revision_id
        )
    )
    source.binding_revision_id = BINDING.revision_id
    encoder, store = Encoder(), Store()
    runner = CommandFileTts(
        synthesizer=source,
        encoder=encoder,
        binding_policy=policy,
        builtin_voice_binding=BuiltinVoiceBinding(
            VOICE.object_id, str(VOICE.revision), VOICE.revision_id, "Vivian"
        ),
        max_payload_bytes=1024 * 1024,
    )

    class Admission:
        frozen: dict[str, Any]

        def admit(self, plan: SpeechPlan) -> SubmitExecutionRequest:
            admitted = replace(
                execution(),
                workspace_id=plan.workspace_id,
                binding_revision_id=plan.binding_revision_id,
                pool_id="tts-batch",
            )
            request = build_tts_submit(plan, admitted)
            # Stand-in for the atomic DB write; the command builder is production.
            self.frozen = {"plan": asdict(plan), "gateway_request": request}
            return request

        def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]:
            raise AssertionError("This request must not create a recovery/retry execution")

    admission = Admission()

    class Consumer(Gateway):
        receipt: dict[str, Any]

        async def query_tts_configuration_evidence(self) -> Any:
            plan = admission.frozen["plan"]
            return {
                **await super().query_tts_configuration_evidence(),
                "gateway_policy_digest": policy.digest,
                "voice_id": plan["voice_id"],
                "voice_revision": str(plan["voice_revision"]),
                "voice_revision_id": plan["voice_revision_id"],
                "binding_revision_id": plan["binding_revision_id"],
            }

        async def submit(self, request: SubmitExecutionRequest) -> Any:
            assert admission.frozen["gateway_request"] == request
            assert admission.frozen["plan"]["max_source_pcm_bytes"] == effective_ms * 48
            self.submissions.append(request)
            translated = submit_request_from_payload(
                request, output_stream_id=derived_stream_id_for(request)
            )
            self.committed = {key: value for key, value in request.items() if key != "command"}
            try:
                outcome = await runner.stage_result(
                    translated, store=store, cancelled=lambda: False
                )
                self.receipt = {
                    "submit_intent_id": request["submit_intent_id"],
                    "request_digest": translated.request_digest,
                    "staging_result": outcome.result.value(),
                }
                self.committed["execution_state"] = "succeeded"
            except TtsCallError:
                self.committed["execution_state"] = "failed"
            return self.committed

        async def audio_staging_result(self, identity: str) -> dict[str, Any]:
            return self.receipt

    class Objects:
        def read(self, reference: Any, storage_version: str) -> bytes:
            return store.results[0].body

    gateway = Consumer()
    adapter = GatewaySynthesizer(cast(GatewayExecutionClient, gateway), admission, Objects())
    service = SpeechSynthesis(Published(), adapter, SpeechLimits(max_output_audio_ms=600_000))
    try:
        nexus: dict[str, Any] = {"overrides": {"tts": {"language": "en"}}}
        if request_ms is not None:
            nexus["max_output_audio_ms"] = request_ms
        body = {**REQUEST, "nexus": nexus}
        if delivery != "mp3":
            body["response_format"] = delivery
        async with client(service) as http:
            response = await http.post(
                "/v1/audio/speech",
                json=body,
            )
        assert response.status_code == (200 if accepted else 503)
        assert len(gateway.submissions) == len(source.calls) == 1
        assert gateway.submissions[0]["max_output_audio_ms"] == effective_ms
        assert source.calls[0][1].max_audio_ms == effective_ms
        assert source.calls[0][1].max_pcm_bytes == min(policy.max_pcm_bytes, effective_ms * 48)
        assert len(store.results) == (1 if accepted else 0)
        if not accepted:
            assert encoder.calls == []
            assert response.headers["content-type"].startswith("application/json")
    finally:
        adapter.close()
