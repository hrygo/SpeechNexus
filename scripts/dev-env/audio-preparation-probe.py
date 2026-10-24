"""Probe bounded file decode plus CPU VAD, optionally on verified local corpus inputs.

No ASR requests, GPU use, public publication or transcript/PCM logging. Corpus
selection paths remain only in the ignored local evidence directory.
"""

from __future__ import annotations

import argparse
import json
import resource
import tempfile
import time
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

import av
from audio_probe_inputs import corpus_selection, synthetic

from speechnexus_gateway.application.file_asr_audio import FileAsrAudio
from speechnexus_gateway.domain.media import (
    MEDIA_RECIPE_VERSION,
    AudioDecodeLimits,
    AudioSourceFormat,
    MediaError,
)
from speechnexus_gateway.domain.vad import VAD_RECIPE_VERSION, VadPolicy
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.infrastructure.silero_onnx import SileroOnnxCpuModel


def prepare(
    model: SileroOnnxCpuModel, path: Path, source_format: AudioSourceFormat
) -> dict[str, object]:
    started = time.monotonic()
    candidates = 0
    windows = 0
    last_source_end = 0
    with path.open("rb") as source:
        prepared = FileAsrAudio(
            decoder=PyAvAudioDecoder(),
            model=model,
            source=source,
            source_format=source_format,
            limits=AudioDecodeLimits(max_encoded_bytes=16 * 1024**2, max_duration_ms=60000),
            vad_policy=VadPolicy("file-probe-1"),
            cancelled=lambda: False,
        )
        try:
            for segment in prepared:
                windows += 1
                candidates += segment.vad.has_speech
                last_source_end = segment.source_sample_span[1]
            summary = prepared.summary
        finally:
            prepared.close()
    if summary is None or last_source_end != summary.source_samples:
        raise MediaError("file preparation did not complete through original EOF")
    return {
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "duration_ms": summary.duration_ms,
        "source_sample_rate_hz": summary.clock.sample_rate_hz,
        "source_channels": summary.clock.channels,
        "source_samples": summary.source_samples,
        "model_samples": summary.output_samples,
        "original_timestamp_known": summary.clock.first_timestamp is not None,
        "asr_candidate_segments": candidates,
        "windows": windows,
        "final_source_eof_covered": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path)
    args = parser.parse_args()
    model = SileroOnnxCpuModel(model_path=args.model, expected_sha256=args.model_sha256)
    records: list[dict[str, object]] = []
    private_selection: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="speechnexus-file-audio-") as directory:
        for name, path, source_format, speech in synthetic(Path(directory)):
            result = prepare(model, path, source_format)
            if (int(str(result["asr_candidate_segments"])) > 0) != speech:
                raise MediaError("synthetic speech/no-speech expectation was not met")
            records.append({"case": name, "synthetic": True, **result})
        selected = (
            corpus_selection(args.corpus_root, Path(directory))
            if args.corpus_root is not None
            else []
        )
        for case in selected:
            records.append(
                {
                    "case": case.group,
                    "synthetic": False,
                    "source_sha256": case.source_sha256,
                    "excerpt_sha256": case.sha256,
                    "excerpt_source_start": 0,
                    "excerpt_source_frames": case.frames,
                    **prepare(model, case.path, AudioSourceFormat("audio/wav")),
                }
            )
            private_selection.append(
                {
                    "group": case.group,
                    "path": str(case.source_path),
                    "source_sha256": case.source_sha256,
                    "excerpt_sha256": case.sha256,
                }
            )
    report = {
        "model_sha256": model.model_sha256,
        "media_recipe": MEDIA_RECIPE_VERSION,
        "vad_recipe": VAD_RECIPE_VERSION,
        "limits": asdict(AudioDecodeLimits(max_encoded_bytes=16 * 1024**2, max_duration_ms=60000)),
        "av_version": version("av"),
        "numpy_version": version("numpy"),
        "onnxruntime_version": version("onnxruntime"),
        "ffmpeg_libraries": av.library_versions,
        "gpu_used": False,
        "asr_dispatched": False,
        "quality_validated": False,
        "max_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "records": records,
        "passed": True,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if private_selection:
        (args.report.parent / "private-corpus-selection.json").write_text(
            json.dumps(private_selection, indent=2) + "\n"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
