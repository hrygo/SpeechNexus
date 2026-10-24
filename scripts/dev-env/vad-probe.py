"""Local pinned CPU VAD probe. Only generated speech, silence and tone are processed.

Run with the separately pinned CPU model runtime; this does not qualify real
speech quality, wire resampling, public streaming, ASR dispatch or cluster recovery.
No GPU/container control, network content transfer, transcript or audio logging.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import shutil
import struct
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

import imageio_ffmpeg

from speechnexus_gateway.application.vad import CpuVadSession
from speechnexus_gateway.domain.vad import (
    VAD_FRAME_BYTES,
    VAD_RECIPE_VERSION,
    EmptyVadBufferError,
    VadPolicy,
    VadSegment,
)
from speechnexus_gateway.infrastructure.silero_onnx import SileroOnnxCpuModel

_SPEECH = "The speech system is ready. Please check the sound and save the result."


def synthetic_samples(directory: Path) -> list[tuple[str, bytes]]:
    espeak = shutil.which("espeak-ng")
    if espeak is None:
        raise RuntimeError("espeak-ng is required for synthetic speech")
    wav = directory / "synthetic.wav"
    subprocess.run(
        [espeak, "-v", "en-us", "-s", "155", "-w", str(wav), _SPEECH],
        check=True,
        capture_output=True,
        timeout=30,
    )
    speech = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    tone = b"".join(
        struct.pack("<h", round(4000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(30 * 16000)
    )
    return [
        ("synthetic-speech", b"\x00" * 9600 + speech + b"\x00" * 32000),
        ("silence-4s", b"\x00" * (4 * 16000 * 2)),
        ("tone-30s", tone),
    ]


def segments_for(
    model: SileroOnnxCpuModel, policy: VadPolicy, audio: bytes, *, chunk_bytes: int
) -> tuple[list[VadSegment], int]:
    session = CpuVadSession(model=model, policy=policy)
    result: list[VadSegment] = []
    maximum_buffer = 0
    try:
        for start in range(0, len(audio), chunk_bytes):
            result.extend(
                session.push(
                    audio[start : start + chunk_bytes], sample_start=session.accepted_samples
                )
            )
            maximum_buffer = max(maximum_buffer, session.buffered_bytes)
            if session.buffered_bytes > session.buffer_limit_bytes:
                raise RuntimeError("VAD exceeded its declared private buffer ceiling")
        with suppress(EmptyVadBufferError):
            result.extend(session.commit())
    finally:
        session.close()
    return result, maximum_buffer


def verify_isolation(model: SileroOnnxCpuModel, audio: bytes) -> bool:
    solo, interleaved, other = (model.new_detector() for _ in range(3))
    for start in range(0, min(len(audio), 100 * VAD_FRAME_BYTES), VAD_FRAME_BYTES):
        frame = audio[start : start + VAD_FRAME_BYTES].ljust(VAD_FRAME_BYTES, b"\x00")
        expected = solo.probability(frame)
        other.probability(b"\x00" * VAD_FRAME_BYTES)
        other.reset()
        if abs(interleaved.probability(frame) - expected) > 1e-6:
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    model = SileroOnnxCpuModel(model_path=args.model, expected_sha256=args.model_sha256)
    loaded_seconds = time.monotonic() - started
    warmup = model.new_detector()
    started = time.monotonic()
    warmup.probability(b"\x00" * VAD_FRAME_BYTES)
    warmed_seconds = time.monotonic() - started
    policy = VadPolicy("probe-1")
    samples: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="speechnexus-cpu-vad-") as directory:
        audio_samples = synthetic_samples(Path(directory))
        isolated = verify_isolation(model, audio_samples[0][1])
        for name, audio in audio_samples:
            started = time.monotonic()
            segments, maximum_buffer = segments_for(model, policy, audio, chunk_bytes=3200)
            elapsed = time.monotonic() - started
            repeated, _ = segments_for(model, policy, audio, chunk_bytes=734)
            speech = [segment for segment in segments if segment.has_speech]
            samples.append(
                {
                    "sample": name,
                    "audio_seconds": len(audio) / 32000,
                    "elapsed_seconds": round(elapsed, 6),
                    "asr_candidate_segments": len(speech),
                    "no_speech_windows": len(segments) - len(speech),
                    "maximum_buffer_bytes": maximum_buffer,
                    "chunk_partition_equal": repeated == segments,
                    "segments": [
                        {
                            "sequence": segment.sequence,
                            "sample_span_16k": [segment.sample_start, segment.sample_end],
                            "wire_coverage_24k": list(segment.wire_sample_span),
                            "reason": segment.reason.value,
                            "has_speech": segment.has_speech,
                        }
                        for segment in segments
                    ],
                }
            )
    passed = (
        isolated
        and all(sample["chunk_partition_equal"] for sample in samples)
        and int(str(samples[0]["asr_candidate_segments"])) > 0
        and all(sample["asr_candidate_segments"] == 0 for sample in samples[1:])
    )
    report = {
        "synthetic_only": True,
        "gpu_used": False,
        "asr_dispatched": False,
        "model_sha256": model.model_sha256,
        "recipe_version": VAD_RECIPE_VERSION,
        "policy": asdict(policy),
        "policy_digest": policy.digest,
        "onnxruntime_version": version("onnxruntime"),
        "numpy_version": version("numpy"),
        "providers": ["CPUExecutionProvider"],
        "model_load_seconds": round(loaded_seconds, 6),
        "first_frame_seconds": round(warmed_seconds, 6),
        "max_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "session_state_isolation": isolated,
        "samples": samples,
        "passed": passed,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit("synthetic CPU VAD probe did not meet its declared checks")


if __name__ == "__main__":
    main()
