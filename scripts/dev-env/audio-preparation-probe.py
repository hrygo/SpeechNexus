"""Probe bounded file decode plus CPU VAD, optionally on verified local corpus inputs.

No ASR requests, GPU use, public publication or transcript/PCM logging. Corpus
selection paths remain only in the ignored local evidence directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path

import av
import numpy as np
from av.audio.stream import AudioStream

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

_SPEECH = "This is a synthetic audio preparation test. Please check the sound."
_GROUPS = {
    "aishell4": "issue370-aishell4-test-20261017/",
    "alimeeting-derived": "issue370-sortformer-1to4-20261017/",
}


@dataclass(frozen=True)
class CorpusExcerpt:
    group: str
    source_path: Path
    path: Path
    source_sha256: str
    sha256: str
    frames: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def synthetic(directory: Path) -> list[tuple[str, Path, AudioSourceFormat, bool]]:
    espeak = shutil.which("espeak-ng")
    if espeak is None:
        raise MediaError("espeak-ng is required for the synthetic probe")
    speech = directory / "speech.wav"
    subprocess.run(
        [espeak, "-v", "en-us", "-s", "155", "-w", str(speech), _SPEECH],
        check=True,
        capture_output=True,
        timeout=30,
    )
    cases = [("speech-wav", speech, AudioSourceFormat("audio/wav"), True)]
    raw = directory / "speech.pcm"
    with wave.open(str(speech), "rb") as original:
        raw.write_bytes(original.readframes(original.getnframes()))
        cases.append(
            (
                "speech-pcm",
                raw,
                AudioSourceFormat("audio/pcm", original.getframerate(), original.getnchannels()),
                True,
            )
        )
    for kind, codec, media in [
        ("mp3", "libmp3lame", "audio/mpeg"),
        ("flac", "flac", "audio/flac"),
        ("ogg", "libopus", "audio/ogg"),
    ]:
        path = directory / f"speech.{kind}"
        with av.open(str(speech)) as original, av.open(str(path), "w", format=kind) as output:
            stream = output.add_stream(codec, rate=48000)
            assert isinstance(stream, AudioStream)
            stream.layout = "mono"
            for frame in original.decode(audio=0):
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
        cases.append((f"speech-{kind}", path, AudioSourceFormat(media), True))
    for name, seconds, frequency in [("silence", 4, 0), ("tone", 30, 440)]:
        path = directory / f"{name}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            samples = (
                4000 * np.sin(np.arange(seconds * 16000) * 2 * np.pi * frequency / 16000)
            ).astype("<i2")
            output.writeframes(samples.tobytes())
        cases.append((name, path, AudioSourceFormat("audio/wav"), False))
    return cases


def corpus_selection(root: Path, directory: Path) -> list[CorpusExcerpt]:
    with (root / "corpus-files.tsv").open() as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    selected: list[CorpusExcerpt] = []
    for group, prefix in _GROUPS.items():
        candidates = sorted(
            (
                row
                for row in rows
                if row["path"].startswith(prefix)
                and row["path"].endswith(".wav")
                and int(row["bytes"]) >= 96000
            ),
            key=lambda row: int(row["bytes"]),
        )
        for row in candidates:
            path = (root / row["path"]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise MediaError("corpus index references an unexpected location")
            with wave.open(str(path), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
                # Read a bounded prefix after verifying the complete source.
                frames = min(
                    audio.getnframes(),
                    audio.getframerate() * 20,
                    (8 * 1024**2 - 4096) // (audio.getnchannels() * audio.getsampwidth()),
                )
                excerpt_seconds = frames / audio.getframerate()
            if duration < 3 or excerpt_seconds < 3:
                continue
            if path.stat().st_size != int(row["bytes"]) or sha256_file(path) != row["sha256"]:
                raise MediaError("selected corpus input differs from the upload index")
            excerpt = directory / f"{group}.wav"
            with wave.open(str(path), "rb") as audio, wave.open(str(excerpt), "wb") as output:
                output.setnchannels(audio.getnchannels())
                output.setsampwidth(audio.getsampwidth())
                output.setframerate(audio.getframerate())
                output.writeframes(audio.readframes(frames))
            selected.append(
                CorpusExcerpt(group, path, excerpt, row["sha256"], sha256_file(excerpt), frames)
            )
            break
        else:
            raise MediaError("a required corpus group has no eligible bounded excerpt")
    return selected


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
