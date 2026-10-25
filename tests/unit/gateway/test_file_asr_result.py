"""Complete original-command consumers produce private results, never publication/stop."""

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from jsonschema import Draft202012Validator

from speechnexus_gateway.application.file_asr import FileAsrTranscription
from speechnexus_gateway.application.file_asr_result import stage_file_asr_result
from speechnexus_gateway.domain.asr import AsrCallError, AsrFailure
from speechnexus_gateway.domain.asr_results import (
    MAX_ASR_RESULT_BYTES,
    AsrResultError,
    PreparedAsrResult,
    StagedAsrResult,
)
from tests.fake.execution_command import original_request
from tests.fake.vad import ScriptedVadModel
from tests.unit.gateway.test_file_asr_command import (
    CapturingRecognizer,
    consumer,
)
from tests.unit.gateway.test_file_asr_command import setup as setup


class Store:
    def __init__(self) -> None:
        self.results: list[PreparedAsrResult] = []
        self.change: str | None = None
        self.after_write: Callable[[], None] = lambda: None

    async def stage(self, result: PreparedAsrResult, *, cancelled: Any) -> StagedAsrResult:
        assert not cancelled()
        self.results.append(result)
        self.after_write()
        receipt = StagedAsrResult(result.request_digest, result.reference("sn-output-test"), "v1")
        if self.change == "request":
            receipt = replace(receipt, request_digest="sha256:" + "0" * 64)
        if self.change == "object":
            receipt = replace(
                receipt,
                reference=replace(receipt.reference, object_key="private/other/result.json"),
            )
        return receipt


@pytest.fixture
def transcription_schema() -> dict[str, Any]:
    schema = yaml.safe_load(Path("contracts/speech-openapi.yaml").read_text())
    return cast(dict[str, Any], schema["components"]["schemas"]["Transcription"])


async def test_complete_transcription_matches_existing_public_schema_and_private_reference(
    setup: Any, transcription_schema: dict[str, Any]
) -> None:
    request, objects, recognizer = setup
    store = Store()
    receipt = await consumer(objects, recognizer).stage_result(
        request, store=store, max_result_bytes=1024, cancelled=lambda: False
    )
    result = store.results[0]
    document = json.loads(result.body)
    Draft202012Validator(transcription_schema).validate(document)
    assert document == {"text": "Synthetic.\nSynthetic."}
    assert receipt.request_digest == request.request_digest
    assert receipt.reference == result.reference("sn-output-test")
    assert result.body == b'{"text":"Synthetic.\\nSynthetic."}'
    assert "Synthetic." not in repr(result) + repr(receipt)
    assert "private/" not in repr(receipt)
    assert len(recognizer.requests) == 2
    assert all(stream.closed for stream in objects.streams)
    # The receipt contains no terminal state, stop, release, or publication assertion.
    assert not hasattr(receipt, "execution_state")
    assert not hasattr(receipt, "stop_evidence_kind")


async def test_silence_has_valid_nonempty_json_without_a_vendor_call(setup: Any) -> None:
    request, objects, recognizer = setup
    value = consumer(objects, recognizer)
    value._vad_model = ScriptedVadModel(fallback=0.0)
    store = Store()
    receipt = await value.stage_result(
        request, store=store, max_result_bytes=11, cancelled=lambda: False
    )
    assert store.results[0].body == b'{"text":""}'
    assert receipt.reference.byte_length == 11
    assert not recognizer.requests
    assert all(stream.closed for stream in objects.streams)


@pytest.mark.parametrize("limit", [0, 10, -1, True, 1.5, MAX_ASR_RESULT_BYTES + 1])
async def test_invalid_limit_is_rejected_before_input_io(setup: Any, limit: Any) -> None:
    request, objects, recognizer = setup
    store = Store()
    with pytest.raises(AsrResultError, match="asr_result_limit_invalid"):
        await consumer(objects, recognizer).stage_result(
            request, store=store, max_result_bytes=limit, cancelled=lambda: False
        )
    assert not store.results and not objects.reads and not recognizer.requests


async def test_aggregate_limit_prevents_all_writes_and_closes_input(setup: Any) -> None:
    request, objects, recognizer = setup
    store = Store()
    with pytest.raises(AsrResultError, match="asr_result_limit_exceeded"):
        await consumer(objects, recognizer).stage_result(
            request, store=store, max_result_bytes=30, cancelled=lambda: False
        )
    assert not store.results and len(recognizer.requests) == 2
    assert all(stream.closed for stream in objects.streams)


@pytest.mark.parametrize("failure", ["cancel", "expired", "binding"])
async def test_late_storage_return_cannot_escape_as_a_usable_result(
    setup: Any, failure: str
) -> None:
    request, objects, recognizer = setup
    store = Store()
    state = {"cancelled": False, "now": request.identity.deadline_at - timedelta(seconds=10)}
    if failure == "cancel":
        store.after_write = lambda: state.update(cancelled=True)
        code = AsrFailure.CANCELLED
    elif failure == "expired":
        store.after_write = lambda: state.update(now=request.identity.deadline_at)
        code = AsrFailure.DEADLINE_EXCEEDED
    else:
        store.after_write = lambda: setattr(recognizer, "binding_revision_id", "binding-other")
        code = AsrFailure.RESPONSE_INVALID
    with pytest.raises(AsrCallError) as caught:
        await consumer(objects, recognizer, now=lambda: state["now"]).stage_result(
            request, store=store, max_result_bytes=1024, cancelled=lambda: state["cancelled"]
        )
    assert caught.value.code is code and caught.value.request_may_have_run
    assert len(store.results) == 1
    assert all(stream.closed for stream in objects.streams)


@pytest.mark.parametrize("change", ["request", "object"])
async def test_wrong_storage_receipt_cannot_be_used(setup: Any, change: str) -> None:
    request, objects, recognizer = setup
    store = Store()
    store.change = change
    with pytest.raises(AsrResultError, match="asr_result_identity_mismatch"):
        await consumer(objects, recognizer).stage_result(
            request, store=store, max_result_bytes=1024, cancelled=lambda: False
        )
    assert all(stream.closed for stream in objects.streams)


async def test_inference_failure_never_writes_a_partial_result(setup: Any) -> None:
    request, objects, recognizer = setup
    recognizer.fail = True
    store = Store()
    with pytest.raises(AsrCallError, match=AsrFailure.TRANSPORT_UNKNOWN):
        await consumer(objects, recognizer).stage_result(
            request, store=store, max_result_bytes=1024, cancelled=lambda: False
        )
    assert not store.results and all(stream.closed for stream in objects.streams)


def test_storage_name_changes_with_request_identity_or_content() -> None:
    a = PreparedAsrResult("workspace-one", "sha256:" + "a" * 64, b'{"text":"one"}')
    assert a.reference("bucket") == replace(a).reference("bucket")
    assert a.reference("bucket") != replace(a, body=b'{"text":"two"}').reference("bucket")
    assert a.reference("bucket") != replace(a, request_digest="sha256:" + "b" * 64).reference(
        "bucket"
    )


@pytest.mark.parametrize("version", ["", "null", "\x00", "v\n1", "v" * 1025])
def test_unusable_storage_version_cannot_become_a_receipt(version: str) -> None:
    result = PreparedAsrResult("workspace-one", "sha256:" + "a" * 64, b'{"text":"one"}')
    with pytest.raises(AsrResultError):
        StagedAsrResult(result.request_digest, result.reference("bucket"), version)


@pytest.mark.parametrize("text", ['私有合成"文本\\\n', "\ud800", "\x01" * 30])
async def test_utf8_and_json_escape_budget(setup: Any, text: str) -> None:
    from speechnexus_gateway.domain.asr import AsrAudio, AsrPolicy, AsrText

    class TextRecognizer(CapturingRecognizer):
        async def transcribe(
            self, audio: AsrAudio, policy: AsrPolicy, *, cancelled: Any
        ) -> AsrText:
            value = await super().transcribe(audio, policy, cancelled=cancelled)
            return replace(value, text=text)

    request, objects, _ = setup
    recognizer = TextRecognizer()
    store = Store()
    if text.startswith("\ud800") or text.startswith("\x01"):
        with pytest.raises(AsrResultError):
            await consumer(objects, recognizer).stage_result(
                request, store=store, max_result_bytes=64, cancelled=lambda: False
            )
        assert not store.results
    else:
        await consumer(objects, recognizer).stage_result(
            request, store=store, max_result_bytes=128, cancelled=lambda: False
        )
        assert json.loads(store.results[0].body) == {"text": text + "\n" + text}


async def test_mutating_original_command_during_storage_does_not_change_receipt(setup: Any) -> None:
    request, objects, recognizer = setup
    digest = request.request_digest
    store = Store()
    store.after_write = lambda: request.command.update(
        parameters={"response_format": "verbose_json"}
    )
    receipt = await consumer(objects, recognizer).stage_result(
        request, store=store, max_result_bytes=1024, cancelled=lambda: False
    )
    assert receipt.request_digest == digest
    assert original_request(request.command).request_digest != digest


@pytest.mark.parametrize(
    "failure",
    ["summary", "source-end", "model-end", "overlap", "sequence", "config", "policy", "clock"],
)
async def test_incomplete_or_inconsistent_native_run_never_reaches_storage(
    setup: Any, failure: str
) -> None:
    request, objects, recognizer = setup
    async with consumer(objects, recognizer).open(request, cancelled=lambda: False) as run:
        segments = [segment async for segment in run]
        summary = run.summary
    assert summary is not None
    if failure == "summary":
        summary = None
    elif failure == "source-end":
        summary = replace(summary, source_samples=summary.source_samples + 1)
    elif failure == "model-end":
        summary = replace(summary, output_samples=summary.output_samples + 1)
    elif failure == "overlap":
        segments[-1] = replace(segments[-1], model_sample_span=(0, 16005))
    elif failure == "sequence":
        segments[-1] = replace(segments[-1], sequence=3)
    elif failure == "config":
        segments[-1] = replace(segments[-1], config_revision="other-policy")
    elif failure == "policy":
        segments[-1] = replace(segments[-1], policy_digest="sha256:" + "0" * 64)
    else:
        segments[-1] = replace(
            segments[-1], source_clock=replace(segments[-1].source_clock, channels=2)
        )

    class Run:
        def __init__(self) -> None:
            self.summary = summary

        async def __aiter__(self) -> AsyncIterator[Any]:
            for segment in segments:
                yield segment

    store = Store()
    with pytest.raises(AsrResultError):
        await stage_file_asr_result(
            request,
            cast(FileAsrTranscription, Run()),
            store=store,
            max_result_bytes=1024,
            check=lambda: None,
        )
    assert not store.results
