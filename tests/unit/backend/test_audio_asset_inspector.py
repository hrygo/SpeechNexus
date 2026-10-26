"""Untrusted worker output cannot manufacture an Asset inspection result."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, BinaryIO, Literal

import pytest
from speechnexus_contracts.audio_inspection import parse as parse_receipt
from speechnexus_contracts.audio_inspection_models import AudioInspectionSuccess

from speechnexus_backend.speech.domain.assets import AssetPolicy, MediaFacts
from speechnexus_backend.speech.infrastructure import audio_asset_inspector as inspector_module
from speechnexus_backend.speech.infrastructure.audio_asset_inspector import (
    ProcessAudioAssetInspector,
)
from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    InspectionProcessResult,
    InspectionProcessStatus,
)
from speechnexus_media.domain import AudioDecodeLimits, AudioSourceFormat
from speechnexus_media.inspection import inspect_audio
from tests.unit.media.test_audio_inspection import wav

POLICY = AssetPolicy(604800)


def receipt() -> dict[str, str | int]:
    return inspect_audio(
        io.BytesIO(wav()),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )


class Worker:
    def __init__(self, outcome: InspectionProcessResult) -> None:
        self.outcome = outcome
        self.calls = 0
        self.after_run: Callable[[], None] = lambda: None

    def run(self, source: BinaryIO, *, cancelled: Callable[[], bool]) -> InspectionProcessResult:
        assert source.tell() == 0 and source.read(4) == b"RIFF"
        assert not cancelled()
        self.calls += 1
        self.after_run()
        return self.outcome


def inspect_output(output: bytes) -> tuple[bool, MediaFacts | None]:
    worker = Worker(InspectionProcessResult("complete", output))
    inspector = ProcessAudioAssetInspector(worker, max_duration_ms=1000)
    body = io.BytesIO(wav())
    body.seek(5)
    result = inspector.inspect("asr_input", body, POLICY)
    assert body.tell() == 0 and not body.closed
    assert result.error_code is None and worker.calls == 1
    return result.available, result.facts


def test_actual_worker_receipt_projects_only_validated_media_facts() -> None:
    assert inspect_output(json.dumps(receipt()).encode()) == (
        True,
        MediaFacts("audio/wav", 100, 48000, 2),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "sha256:" + "0" * 64),
        ("encoded_bytes", 1),
        ("encoded_bytes", 19244.0),
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", 2),
        ("recipe_version", "unapproved-recipe"),
        ("status", "failed"),
        ("source_samples", 4801),
        ("source_samples", 4800.0),
        ("source_samples", False),
        ("duration_ms", 101),
        ("duration_ms", 0),
        ("sample_rate_hz", 0),
        ("sample_rate_hz", 48000.0),
        ("channels", 2.0),
        ("channels", True),
        ("channels", 9),
        ("media_type", "audio/pcm"),
        ("media_type", "text/plain"),
        ("private_path", "synthetic-private-input"),
    ],
)
def test_bad_identity_clock_schema_or_extra_fields_stays_unavailable(
    field: str, value: object
) -> None:
    document: dict[str, object] = dict(receipt())
    document[field] = value
    assert inspect_output(json.dumps(document).encode()) == (False, None)


@pytest.mark.parametrize("kind", ["truncated", "duplicate", "constant", "utf8", "root", "oversize"])
def test_malformed_or_ambiguous_receipt_never_yields_facts(kind: str) -> None:
    output = json.dumps(receipt()).encode()
    if kind == "truncated":
        output = output[:-1]
    elif kind == "duplicate":
        output = b'{"status":"failed",' + output[1:]
    elif kind == "constant":
        output = output.replace(b'"source_samples": 4800', b'"source_samples": NaN')
    elif kind == "utf8":
        output = b"\xff"
    elif kind == "root":
        output = b"[]"
    elif kind == "oversize":
        output += b" " * 4096
    assert inspect_output(output) == (False, None)


@pytest.mark.parametrize(
    "status", ["unavailable", "busy", "failed", "timeout", "cancelled", "stop_unknown"]
)
def test_process_failure_or_unknown_cannot_become_content_rejection(
    status: InspectionProcessStatus,
) -> None:
    # Construct through the production result type; its invariant disallows
    # output for every non-complete process outcome.
    result = InspectionProcessResult(status)
    worker = Worker(result)
    inspection = ProcessAudioAssetInspector(worker, max_duration_ms=1000).inspect(
        "asr_input", io.BytesIO(wav()), POLICY
    )
    assert not inspection.available and inspection.facts is None and inspection.error_code is None


def test_cancel_after_worker_completion_discards_late_result() -> None:
    cancelled = False

    def cancel() -> None:
        nonlocal cancelled
        cancelled = True

    worker = Worker(InspectionProcessResult("complete", json.dumps(receipt()).encode()))
    worker.after_run = cancel
    result = ProcessAudioAssetInspector(
        worker,
        max_duration_ms=1000,
        cancelled=lambda: cancelled,
    ).inspect("asr_input", io.BytesIO(wav()), POLICY)
    assert not result.available and result.facts is None


def test_cancel_during_receipt_validation_is_checked_before_exposing_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = False
    original = parse_receipt

    def parse_then_cancel(
        name: Literal["AudioInspectionSuccess"], value: Any
    ) -> AudioInspectionSuccess:
        nonlocal cancelled
        parsed = original(name, value)
        cancelled = True
        return parsed

    monkeypatch.setattr(inspector_module, "parse", parse_then_cancel)
    worker = Worker(InspectionProcessResult("complete", json.dumps(receipt()).encode()))
    result = ProcessAudioAssetInspector(
        worker, max_duration_ms=1000, cancelled=lambda: cancelled
    ).inspect("asr_input", io.BytesIO(wav()), POLICY)
    assert not result.available and result.facts is None


def test_cancelled_empty_or_oversized_input_never_starts_a_worker() -> None:
    worker = Worker(InspectionProcessResult("failed"))
    inspector = ProcessAudioAssetInspector(worker, max_duration_ms=1000)
    assert not inspector.inspect("asr_input", io.BytesIO(), POLICY).available
    assert not inspector.inspect(
        "asr_input", io.BytesIO(wav()), replace(POLICY, max_bytes=10)
    ).available
    cancelled = ProcessAudioAssetInspector(worker, max_duration_ms=1000, cancelled=lambda: True)
    assert not cancelled.inspect("asr_input", io.BytesIO(wav()), POLICY).available
    assert worker.calls == 0


def test_text_behavior_and_unqualified_voice_reference_are_preserved() -> None:
    worker = Worker(InspectionProcessResult("failed"))
    inspector = ProcessAudioAssetInspector(worker, max_duration_ms=1000)
    assert inspector.inspect("text_input", io.BytesIO(b"plain text"), POLICY).facts == MediaFacts(
        "text/plain; charset=utf-8"
    )
    assert (
        inspector.inspect("text_input", io.BytesIO(b"\xff"), POLICY).error_code
        == "invalid_asset_text"
    )
    assert not inspector.inspect("voice_reference", io.BytesIO(wav()), POLICY).available
    assert worker.calls == 0


def test_configured_duration_limit_is_enforced_independently_of_worker() -> None:
    worker = Worker(InspectionProcessResult("complete", json.dumps(receipt()).encode()))
    result = ProcessAudioAssetInspector(worker, max_duration_ms=99).inspect(
        "asr_input", io.BytesIO(wav()), POLICY
    )
    assert not result.available and result.facts is None
