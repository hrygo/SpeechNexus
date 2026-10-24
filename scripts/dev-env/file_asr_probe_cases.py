"""Run the production native file-ASR composition inside the controller probe lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from audio_probe_inputs import SYNTHETIC_REFERENCE, corpus_selection, synthetic

from speechnexus_gateway.application.file_asr import FileAsrTranscription
from speechnexus_gateway.application.file_asr_audio import FileAsrAudio
from speechnexus_gateway.domain.asr import AsrAudio, AsrCallError, AsrFailure, AsrPolicy
from speechnexus_gateway.domain.media import AudioDecodeLimits, AudioSourceFormat
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.infrastructure.silero_onnx import SileroOnnxCpuModel
from speechnexus_gateway.infrastructure.vllm_asr import VllmAsrEndpoint, VllmAsrRecognizer


def word_error_rate(reference: str, observed: str) -> float:
    expected = re.findall(r"\w+", reference.casefold())
    actual = re.findall(r"\w+", observed.casefold())
    row = list(range(len(actual) + 1))
    for index, word in enumerate(expected, start=1):
        previous, row = row, [index]
        for column, candidate in enumerate(actual, start=1):
            row.append(
                min(
                    previous[column] + 1,
                    row[-1] + 1,
                    previous[column - 1] + (word != candidate),
                )
            )
    return row[-1] / len(expected)


async def file_asr_cases(
    arguments: argparse.Namespace, address: str, port: int, work: Path, evidence: Path
) -> dict[str, Any]:
    model = SileroOnnxCpuModel(
        model_path=arguments.file_asr_vad_model,
        expected_sha256=arguments.file_asr_vad_sha256,
    )
    endpoint = VllmAsrEndpoint(address, port, arguments.served_model_name, "binding-file-asr-probe")
    recognizer = VllmAsrRecognizer(endpoint)
    cases = synthetic(work)
    real_cases: dict[str, dict[str, object]] = {}
    if arguments.corpus_root is not None:
        selection = corpus_selection(arguments.corpus_root, work)
        private = []
        for case in selection:
            cases.append((case.group, case.path, AudioSourceFormat("audio/wav"), True))
            real_cases[case.group] = {
                "source_sha256": case.source_sha256,
                "excerpt_sha256": case.sha256,
                "excerpt_source_start": 0,
                "excerpt_source_frames": case.frames,
            }
            if arguments.file_asr_repeat_corpus:
                name = case.group + "-repeat"
                cases.append((name, case.path, AudioSourceFormat("audio/wav"), True))
                real_cases[name] = {**real_cases[case.group], "repeat_of": case.group}
            private.append({"group": case.group, "source_path": str(case.source_path)})
        (evidence / "private-corpus-selection.json").write_text(
            json.dumps(private, indent=2) + "\n"
        )
    records: list[dict[str, Any]] = []
    for name, path, source_format, speech in cases:
        policy = AsrPolicy("file-asr-probe-1", language="auto" if name in real_cases else "en")
        started = time.monotonic()
        texts: list[str] = []
        calls = 0
        tokens = 0
        windows = 0
        final_source_end = 0
        with path.open("rb") as source:
            audio = FileAsrAudio(
                decoder=PyAvAudioDecoder(),
                model=model,
                source=source,
                source_format=source_format,
                limits=AudioDecodeLimits(
                    max_encoded_bytes=16 * 1024**2,
                    max_duration_ms=60000,
                    timeout_seconds=300,
                ),
                vad_policy=VadPolicy(policy.config_revision),
                cancelled=lambda: False,
            )
            prepared = FileAsrTranscription(
                audio=audio,
                recognizer=recognizer,
                execution_id="execution-probe-" + uuid4().hex,
                policy=policy,
                cancelled=lambda: False,
            )
            try:
                async for result in prepared:
                    windows += 1
                    final_source_end = result.source_sample_span[1]
                    if result.request_id is not None:
                        calls += 1
                        tokens += result.completion_tokens
                        texts.append(result.text)
                summary = prepared.summary
            finally:
                await prepared.aclose()
        if summary is None or final_source_end != summary.source_samples:
            raise RuntimeError("file ASR did not complete through the source EOF")
        if (calls > 0) != speech:
            raise RuntimeError("the file ASR speech gate did not match the probe case")
        text = " ".join(texts)
        record = {
            "case": name,
            "synthetic": name not in real_cases,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "duration_ms": summary.duration_ms,
            "source_samples": summary.source_samples,
            "source_rate_hz": summary.clock.sample_rate_hz,
            "windows": windows,
            "engine_calls": calls,
            "completion_tokens": tokens,
            "text_empty": not text.strip(),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_eof_covered": True,
            **real_cases.get(name, {}),
        }
        if name.startswith("speech-"):
            record["synthetic_word_error_rate"] = round(
                word_error_rate(SYNTHETIC_REFERENCE, text), 4
            )
        records.append(record)
    # Prove a real server length limit is observable and cannot become a successful transcript.
    speech_path = next(path for name, path, _, _ in cases if name == "speech-wav")
    with speech_path.open("rb") as source:
        decoded = PyAvAudioDecoder().open(
            source,
            source_format=AudioSourceFormat("audio/wav"),
            limits=AudioDecodeLimits(),
            cancelled=lambda: False,
        )
        pcm = b"".join(block.pcm16 for block in decoded)
    try:
        await recognizer.transcribe(
            AsrAudio("limit-probe-" + uuid4().hex, pcm),
            replace(AsrPolicy("file-asr-probe-1", language="en"), max_completion_tokens=1),
            cancelled=lambda: False,
        )
    except AsrCallError as error:
        if error.code is not AsrFailure.OUTPUT_TRUNCATED or error.completion_tokens != 1:
            raise RuntimeError("the real server did not attest the expected token limit") from None
    else:
        raise RuntimeError("a deliberately truncated output was accepted as a complete transcript")
    return {
        "file_asr_recipe": "file-asr-1",
        "runtime_versions": {
            name: version(name)
            for name in ("av", "numpy", "onnxruntime", "httpx", "httpcore", "anyio")
        },
        "vad_model_sha256": model.model_sha256,
        "real_corpus_quality_validated": False,
        "request_status_abort_qualified": False,
        "resource_release_inferred_from_http": False,
        "token_limit_one_rejected_as_truncated": True,
        "records": records,
    }
