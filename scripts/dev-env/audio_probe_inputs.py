"""Local-only synthetic samples and hash-verified bounded corpus excerpts for probes."""

from __future__ import annotations

import csv
import hashlib
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np
from av.audio.stream import AudioStream

from speechnexus_gateway.domain.media import AudioSourceFormat, MediaError

SYNTHETIC_REFERENCE = "This is a synthetic audio preparation test. Please check the sound."
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
        [espeak, "-v", "en-us", "-s", "155", "-w", str(speech), SYNTHETIC_REFERENCE],
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
