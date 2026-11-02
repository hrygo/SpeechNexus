"""Synthetic faults for the private synchronous TTS execution path."""

from dataclasses import asdict, replace
from typing import Any, cast

import pytest
from speechnexus_contracts.execution_models import SubmitExecutionRequest

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.speech_synthesis import SpeechUnavailableError
from speechnexus_backend.speech.application.transcription import AdmittedExecution
from speechnexus_backend.speech.domain.admission import (
    AdmissionFact,
    AdmissionResult,
    StageAllocation,
    StageReservation,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat, SpeechPlan
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayConnectionError,
    GatewayExecutionClient,
    GatewayRejectedError,
)
from speechnexus_backend.speech.infrastructure.gateway_synthesis import GatewaySynthesizer
from speechnexus_backend.speech.infrastructure.tts_admission import (
    PostgresTtsAdmission,
    _identities,
)
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit


def plan() -> SpeechPlan:
    return SpeechPlan(
        request_id="request-test",
        model_id="model-test",
        workspace_id="workspace-test",
        binding_revision_id="binding-test",
        workspace_policy_revision=1,
        text="Synthetic text.",
        voice_id="voice-test",
        voice_revision_id="voice-revision-test",
        voice_revision=2,
        gateway_policy_digest="sha256:" + "a" * 64,
        native_speaker_id="Vivian",
        response_format=SpeechFormat.PCM,
        sample_rate_hz=24000,
        channels=1,
        speed=1.0,
        language="en",
    )


def execution() -> AdmittedExecution:
    identity, _, _, _, submit = _identities("request-test")
    return AdmittedExecution(
        submit,
        "lease-test",
        identity.operation_id,
        identity.attempt_id,
        "stage-test",
        "workspace-test",
        "tts-realtime",
        "host-test",
        "gateway-test",
        "instance-test",
        1,
        "binding-test",
        "policy-test",
        1,
        2_000_000_000_000_000,
    )


def test_command_pins_voice_and_every_effective_parameter() -> None:
    request = build_tts_submit(plan(), execution())
    command = cast(dict[str, Any], request["command"])
    assert request["voice_revision_id"] == "voice-revision-test"
    assert command["resources"]["voice"] == {"id": "voice-test", "revision": "2"}
    assert command["text"] == "Synthetic text."
    assert command["parameters"]["audio_delivery"] == "complete"
    with pytest.raises(ValueError):
        build_tts_submit(replace(plan(), planner_max_chars=2), execution())


class Originals:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def read(self, identity: Any) -> dict[str, Any] | None:
        return self.value


class Transactions:
    def query_admission(self, identity: Any, *, request_digest: Any = None) -> AdmissionResult:
        stage = StageAllocation(
            "stage-test",
            "lease-test",
            "tts-realtime",
            "host-test",
            "gateway-test",
            "instance-test",
            1,
            1,
            1,
        )
        physical = PhysicalResources(0, 0, 0, 0, 0, 0)
        return AdmissionResult(
            "replayed", identity, AdmissionFact(identity, (StageReservation(stage, physical),), 1)
        )


def test_restart_recovers_stored_plan_and_rejects_changed_intent() -> None:
    original = {
        "kind": "tts_file",
        "plan": asdict(plan()),
        "gateway_request": build_tts_submit(plan(), execution()),
    }
    adapter = PostgresTtsAdmission(
        cast(Any, None),
        AdmissionService(cast(Any, Transactions())),
        Originals(original),
        cast(Any, None),
    )
    restored, request = adapter.recover("request-test")
    assert restored == plan()
    assert request == original["gateway_request"]
    with pytest.raises(SpeechUnavailableError):
        adapter.admit(replace(plan(), text="changed"))


class Admission:
    def admit(self, value: SpeechPlan) -> SubmitExecutionRequest:
        return build_tts_submit(value, execution())

    def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]:
        return plan(), self.admit(plan())


class Gateway:
    def __init__(self) -> None:
        self.submissions: list[SubmitExecutionRequest] = []
        self.committed: dict[str, Any] | None = None

    async def query(self, identity: str) -> Any:
        if self.committed is None:
            raise GatewayRejectedError("not found", request_may_have_run=False, status_code=404)
        return self.committed

    async def query_tts_configuration_evidence(self) -> Any:
        requested = plan()
        admitted = execution()
        return {
            "gateway_policy_digest": requested.gateway_policy_digest,
            "native_speaker_id": requested.native_speaker_id,
            "voice_id": requested.voice_id,
            "voice_revision": str(requested.voice_revision),
            "voice_revision_id": requested.voice_revision_id,
            "policy_revision_id": admitted.policy_revision_id,
            "binding_revision_id": admitted.binding_revision_id,
            "node_id": admitted.host_node_id,
            "gateway_id": admitted.gateway_id,
            "instance_id": admitted.instance_id,
            "instance_generation": admitted.instance_generation,
        }

    async def submit(self, request: SubmitExecutionRequest) -> Any:
        self.submissions.append(request)
        self.committed = {
            key: request[key]
            for key in (
                "submit_intent_id",
                "lease_id",
                "operation_id",
                "attempt_id",
                "stage_id",
                "workspace_id",
                "pool_id",
                "host_node_id",
                "gateway_id",
                "instance_id",
                "instance_generation",
                "binding_revision_id",
                "policy_revision_id",
                "voice_revision_id",
                "fencing_token",
                "command_digest",
            )
        }
        self.committed["execution_state"] = "unknown_quarantined"
        raise GatewayConnectionError("lost reply", request_may_have_run=True)

    async def aclose(self) -> None:
        pass


def test_lost_submit_reply_recovers_original_without_second_execution() -> None:
    gateway = Gateway()
    adapter = GatewaySynthesizer(
        cast(GatewayExecutionClient, gateway), Admission(), cast(Any, object())
    )
    try:
        with pytest.raises(SpeechUnavailableError):
            adapter.synthesize(plan())
        with pytest.raises(SpeechUnavailableError):
            adapter.recover("request-test", timeout_seconds=1)
        assert len(gateway.submissions) == 1
        assert gateway.submissions[0] == build_tts_submit(plan(), execution())
    finally:
        adapter.close()


def test_commit_then_lost_admission_reply_reloads_full_frozen_request() -> None:
    import json

    from speechnexus_backend.speech.domain.admission import unknown
    from speechnexus_backend.speech.infrastructure.transcription_admission import (
        LiveTranscriptionFacts,
    )
    from speechnexus_backend.speech.infrastructure.tts_admission import TtsRecipe
    from tests.fake.admission import APPROVED
    from tests.fake.runtime_intents import NOW

    class Store:
        body: dict[str, Any] | None = None
        saved: Any = None
        calls = 0

        def read(self, identity: Any) -> dict[str, Any] | None:
            return self.body

        def admit_recipe(self, command: Any) -> AdmissionResult:
            self.calls += 1
            self.saved = command
            self.body = json.loads(command.business_command.document)
            return unknown(command.request.identity)

        def query_admission(self, identity: Any, *, request_digest: Any = None) -> AdmissionResult:
            stage = self.saved.request.stages[0]
            fact = AdmissionFact(
                identity, (StageReservation(stage, PhysicalResources(0, 0, 0, 0, 0, 0)),), 1
            )
            return AdmissionResult("replayed", identity, fact)

    class Facts:
        def read(self, value: Any, recipe: Any) -> LiveTranscriptionFacts:
            return LiveTranscriptionFacts(
                NOW,
                1,
                "policy-test",
                1,
                "realtime",
                "scheduler-test",
                7,
                1,
                2,
                "host-test",
                "gateway-test",
            )

    store = Store()
    recipe = TtsRecipe("model-test", "stage-main", "instance-new", 1, APPROVED)
    adapter = PostgresTtsAdmission(
        Facts(), AdmissionService(store), store, recipe, default_language="en"
    )
    requested = replace(plan(), language=None)
    request = adapter.admit(requested)
    assert store.body is not None
    assert store.body["gateway_request"] == request
    assert store.body["plan"]["language"] == "en"
    assert store.body["voice"] == {
        "id": "voice-test",
        "revision": 2,
        "revision_id": "voice-revision-test",
    }
    # A later deployment default cannot change a previously committed command.
    restored = PostgresTtsAdmission(
        Facts(), AdmissionService(store), store, recipe, default_language="zh"
    )
    assert restored.admit(requested) == request
    assert restored.recover("request-test")[1] == request
    assert store.calls == 1


def test_new_admission_locks_before_retirement_and_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from types import SimpleNamespace

    from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
    from speechnexus_backend.speech.infrastructure import admission_postgres

    events: list[str] = []

    class Cursor:
        def execute(self, sql: str, parameters: Any = ()) -> None:
            events.append("lock" if "pg_advisory_xact_lock" in sql else "publication")

        def fetchall(self) -> list[dict[str, str]]:
            return [{"revision_id": "voice-revision-test"}]

    def original(*args: Any, **kwargs: Any) -> None:
        events.append("original")
        return

    def qualify(cursor: Any, **kwargs: Any) -> None:
        events.append("qualification")
        assert kwargs == {
            "voice_id": "voice-test",
            "revision": 2,
            "revision_id": "voice-revision-test",
            "binding_revision_id": "binding-test",
            "policy_revision_id": "policy-test",
            "instance_id": "instance-test",
            "instance_generation": 1,
            "host_node_id": "host-test",
            "gateway_id": "gateway-test",
            "command": build_tts_submit(plan(), execution())["command"],
            "sample_rate_hz": 24000,
            "gateway_policy_digest": plan().gateway_policy_digest,
            "native_speaker_id": plan().native_speaker_id,
        }

    monkeypatch.setattr(admission_postgres, "read_original", original)
    body: dict[str, Any] = {
        "kind": "tts_file",
        "plan": {
            "gateway_policy_digest": plan().gateway_policy_digest,
            "native_speaker_id": plan().native_speaker_id,
        },
        "voice": {"id": "voice-test", "revision": 2, "revision_id": "voice-revision-test"},
        "gateway_request": build_tts_submit(plan(), execution()),
    }
    prepared = SimpleNamespace(
        command=SimpleNamespace(
            business_command=SimpleNamespace(document=json.dumps(body)),
            request=SimpleNamespace(
                identity=execution(),
                binding_revision_id="binding-test",
                policy_revision_id="policy-test",
                workspace_id="workspace-test",
                deadline_at_us=execution().deadline_at_us,
                stages=(execution(),),
                authorities=(SimpleNamespace(fencing_token=1),),
            ),
        )
    )
    port = admission_postgres.PostgresAdmissionTransactions(
        cast(Any, None), max_command_bytes=65536, require_builtin_voice=qualify
    )
    port._lock_tts_voice(cast(Any, Cursor()), cast(Any, prepared))
    assert events == ["original", "lock", "original", "publication", "qualification"]
    events.clear()
    body["voice"]["revision"] = 3
    prepared.command.business_command.document = json.dumps(body)
    with pytest.raises(AdmissionRejectedError):
        port._lock_tts_voice(cast(Any, Cursor()), cast(Any, prepared))
    assert events == []
    body["voice"]["revision"] = 2
    prepared.command.business_command.document = json.dumps(body)
    port = admission_postgres.PostgresAdmissionTransactions(
        cast(Any, None), max_command_bytes=65536
    )
    with pytest.raises(AdmissionRejectedError):
        port._lock_tts_voice(cast(Any, Cursor()), cast(Any, prepared))
    assert events == ["original"]


@pytest.mark.parametrize("wrong_digest", [False, True])
def test_gateway_reads_complete_audio_for_admitted_identity(wrong_digest: bool) -> None:
    from speechnexus_gateway.interfaces.execution_payloads import (
        derived_stream_id_for,
        submit_request_from_payload,
    )

    payload = b"\0\0" * 2400

    class SuccessGateway(Gateway):
        async def submit(self, request: SubmitExecutionRequest) -> Any:
            self.submissions.append(request)
            keys = (
                "submit_intent_id",
                "lease_id",
                "operation_id",
                "attempt_id",
                "stage_id",
                "workspace_id",
                "pool_id",
                "host_node_id",
                "gateway_id",
                "instance_id",
                "instance_generation",
                "binding_revision_id",
                "policy_revision_id",
                "voice_revision_id",
                "fencing_token",
                "command_digest",
            )
            self.committed = {key: cast(dict[str, Any], request)[key] for key in keys}
            self.committed["execution_state"] = "succeeded"
            return self.committed

        async def audio_staging_result(self, identity: str) -> dict[str, Any]:
            request = self.submissions[0]
            digest = submit_request_from_payload(
                request, output_stream_id=derived_stream_id_for(request)
            ).request_digest
            if wrong_digest:
                digest = "sha256:" + "f" * 64
            return {
                "submit_intent_id": identity,
                "request_digest": digest,
                "staging_result": {
                    "request_digest": digest,
                    "reference": {"workspace_id": "workspace-test", "byte_length": len(payload)},
                    "storage_version": "version-test",
                    "media_type": "audio/pcm",
                    "audio": {"sample_rate_hz": 24000, "channels": 1, "duration_ms": 100},
                },
            }

    class Results:
        def read(self, reference: Any, storage_version: str) -> bytes:
            assert storage_version == "version-test"
            return payload

    gateway = SuccessGateway()
    adapter = GatewaySynthesizer(cast(GatewayExecutionClient, gateway), Admission(), Results())
    try:
        if wrong_digest:
            with pytest.raises(SpeechUnavailableError):
                adapter.synthesize(plan())
        else:
            audio = adapter.synthesize(plan())
            assert audio.payload == payload
            assert audio.duration_ms == 100
        assert len(gateway.submissions) == 1
    finally:
        adapter.close()


@pytest.mark.parametrize("slow_phase", ["admission", "query", "recover_expired", "recover_slow"])
def test_deadline_covers_admission_query_and_recovery(slow_phase: str) -> None:
    import asyncio
    import threading
    import time

    from speechnexus_backend.speech.application.speech_synthesis import SpeechDeadlineExceededError

    short_plan = replace(plan(), timeout_seconds=0.01)
    release = threading.Event()

    class SlowAdmission(Admission):
        def admit(self, value: SpeechPlan) -> SubmitExecutionRequest:
            if slow_phase == "admission":
                time.sleep(0.03)
            return super().admit(value)

        def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]:
            if slow_phase == "recover_slow":
                release.wait(1)
            request = self.admit(short_plan)
            request["deadline_at"] = "2020-01-01T00:00:00Z"
            return short_plan, request

    class SlowGateway(Gateway):
        calls = 0

        async def query(self, identity: str) -> Any:
            self.calls += 1
            if slow_phase == "query":
                await asyncio.sleep(0.03)
            request = build_tts_submit(short_plan, execution())
            return {**request, "execution_state": "succeeded"}

    class Cancellation:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request_cancellation(self, operation_id: str, reason: str) -> None:
            self.calls.append((operation_id, reason))

    gateway = SlowGateway()
    cancellation = Cancellation()
    adapter = GatewaySynthesizer(
        cast(GatewayExecutionClient, gateway),
        SlowAdmission(),
        cast(Any, object()),
        cancellation=cancellation,
    )
    started = time.monotonic()
    try:
        with pytest.raises(SpeechDeadlineExceededError):
            if slow_phase in ("recover_expired", "recover_slow"):
                adapter.recover("request-test", timeout_seconds=0.01)
            else:
                adapter.synthesize(short_plan)
        if slow_phase in ("admission", "recover_expired", "recover_slow"):
            assert gateway.calls == 0
        if slow_phase == "recover_slow":
            assert time.monotonic() - started < 0.5
        if slow_phase in ("admission", "query"):
            assert len(cancellation.calls) == 1
            assert cancellation.calls[0][1] == "deadline_exceeded"
        else:
            assert cancellation.calls == []
    finally:
        release.set()
        adapter.close()


@pytest.mark.parametrize("missing", [False, True])
def test_original_404_never_submits_without_matching_live_authority(missing: bool) -> None:
    class ChangedGateway(Gateway):
        async def query_tts_configuration_evidence(self) -> Any:
            evidence = await super().query_tts_configuration_evidence()
            evidence["gateway_policy_digest"] = "sha256:" + "f" * 64
            return evidence

    gateway = ChangedGateway()
    adapter = GatewaySynthesizer(
        cast(GatewayExecutionClient, gateway), Admission(), cast(Any, object())
    )
    try:
        original = plan()
        if missing:
            original = replace(original, gateway_policy_digest=None, native_speaker_id=None)
        with pytest.raises(SpeechUnavailableError):
            adapter.synthesize(original)
        assert gateway.submissions == []
    finally:
        adapter.close()


def test_old_intent_with_existing_execution_does_not_refresh_authority() -> None:
    old = replace(plan(), gateway_policy_digest=None, native_speaker_id=None)

    class OriginalAdmission(Admission):
        def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]:
            return old, build_tts_submit(old, execution())

    class ExistingGateway(Gateway):
        async def query_tts_configuration_evidence(self) -> Any:
            pytest.fail("existing original execution must not resolve current authority")

    gateway = ExistingGateway()
    gateway.committed = {
        **build_tts_submit(old, execution()),
        "execution_state": "unknown_quarantined",
    }
    adapter = GatewaySynthesizer(
        cast(GatewayExecutionClient, gateway), OriginalAdmission(), cast(Any, object())
    )
    try:
        with pytest.raises(SpeechUnavailableError):
            adapter.recover("request-test", timeout_seconds=1)
        assert gateway.submissions == []
    finally:
        adapter.close()


def test_old_persisted_plan_without_authority_still_recovers_original_identity() -> None:
    from speechnexus_backend.speech.infrastructure.tts_admission import OriginalTtsRecovery

    old = replace(plan(), gateway_policy_digest=None, native_speaker_id=None)
    stored = asdict(old)
    del stored["gateway_policy_digest"]
    del stored["native_speaker_id"]
    original = {
        "kind": "tts_file",
        "plan": stored,
        "gateway_request": build_tts_submit(old, execution()),
    }
    reader = OriginalTtsRecovery(Originals(original), AdmissionService(cast(Any, Transactions())))
    restored, request = reader.recover("request-test")
    assert restored == old
    assert request == original["gateway_request"]
