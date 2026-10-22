from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import av
import imageio_ffmpeg
import numpy as np
import soundfile as sf


def main() -> None:
    sample_rate = 24_000
    frames = 2_400
    time = np.arange(frames, dtype=np.float32) / sample_rate
    samples = (0.1 * np.sin(2 * np.pi * 440 * time) * 32767).astype(np.int16)

    with tempfile.TemporaryDirectory(prefix="speechnexus-media-") as directory:
        root = Path(directory)
        wav = root / "fixture.wav"
        asr_wav = root / "fixture-16k.wav"
        mp3 = root / "fixture.mp3"
        sf.write(wav, samples, sample_rate, subtype="PCM_16", format="WAV")

        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(wav),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-codec:a",
                "pcm_s16le",
                str(asr_wav),
            ],
            check=True,
        )
        asr_samples, asr_rate = sf.read(asr_wav, dtype="int16")
        assert asr_rate == 16_000
        assert asr_samples.size == 1_600

        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(wav),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(mp3),
            ],
            check=True,
        )
        with av.open(str(mp3)) as container:
            stream = next(iter(container.streams.audio))
            frames_decoded = sum(frame.samples for frame in container.decode(stream))
            assert stream.codec_context.sample_rate == sample_rate
            assert stream.codec_context.channels == 1
            assert frames_decoded > 0

        decoded, decoded_rate = sf.read(mp3, dtype="int16")
        assert decoded_rate == sample_rate
        assert decoded.ndim == 1
        assert decoded.size > 0

    print(
        "media toolchain: ready "
        f"(PCM16 {sample_rate} Hz mono, 16 kHz ASR conversion, WAV, "
        f"MP3 decode {frames_decoded} samples)"
    )


if __name__ == "__main__":
    main()
