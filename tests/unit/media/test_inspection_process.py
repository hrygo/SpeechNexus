"""The actual media worker in the unchanged private process boundary."""

from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryFile

import pytest
from tests.unit.backend.test_audio_inspection_process import image as image
from tests.unit.media.test_audio_inspection import wav

from speechnexus_backend.speech.domain.assets import AssetPolicy, MediaFacts
from speechnexus_backend.speech.infrastructure.audio_asset_inspector import (
    ProcessAudioAssetInspector,
)
from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    AudioInspectionProcess,
    InspectionProcessLimits,
)


@pytest.fixture(scope="module")
def media_image(image: tuple[Path, str]) -> tuple[Path, str]:
    root, executable = image
    site = root / "runtime/site-packages"
    site.mkdir(parents=True)
    libraries: list[Path] = []
    for distribution, packages in (
        ("av", ("av", "av.libs")),
        ("numpy", ("numpy", "numpy.libs")),
    ):
        installed = importlib.metadata.distribution(distribution)
        for name in packages:
            source = Path(str(installed.locate_file(name))).resolve()
            if source.is_dir():
                destination = site / name
                shutil.copytree(
                    source,
                    destination,
                    ignore=shutil.ignore_patterns("__pycache__", "tests"),
                )
                libraries.extend(source.rglob("*.so*"))
    shared = Path(__file__).resolve().parents[3] / "packages/media/src/speechnexus_media"
    shutil.copytree(
        shared, site / "speechnexus_media", ignore=shutil.ignore_patterns("__pycache__")
    )
    for library in libraries:
        linked = subprocess.run(["ldd", str(library)], capture_output=True, text=True)
        assert linked.returncode == 0 and "not found" not in linked.stdout
        for filename in re.findall(r"/[^\s()]+", linked.stdout):
            source = Path(filename)
            # Wheel-local dependencies keep their relative RPATH under site.
            if source.parent.name.endswith(".libs"):
                continue
            target = root / source.relative_to("/")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    return root, executable


@pytest.mark.parametrize("valid", [True, False])
def test_actual_decoder_runs_in_no_network_no_fork_inspection_image(
    media_image: tuple[Path, str], valid: bool
) -> None:
    root, executable = media_image
    command = (
        executable,
        "-I",
        "-c",
        "import sys; sys.path.insert(0, '/runtime/site-packages'); "
        "from speechnexus_media.inspection import main; raise SystemExit(main())",
        "--media-type",
        "audio/wav",
        "--max-encoded-bytes",
        "65536",
        "--max-duration-ms",
        "1000",
        "--timeout-seconds",
        "5",
    )
    runner = AudioInspectionProcess(
        root,
        command,
        InspectionProcessLimits(65536, 1024**3, 5, 10.0),
        launcher_python=executable,
    )
    try:
        with TemporaryFile() as source:
            source.write(wav() if valid else b"synthetic-private-corrupt-audio")
            source.seek(0)
            result = runner.run(source, cancelled=lambda: False)
            # The private formal AssetInspector port consumes the same actual
            # worker, through its generated receipt validator and input binding.
            source.seek(0)
            consumed = ProcessAudioAssetInspector(runner, max_duration_ms=1000).inspect(
                "asr_input", source, AssetPolicy(604800)
            )
        if valid:
            assert result.status == "complete", result.status
            report = json.loads(result.output)
            assert report["status"] == "complete"
            assert report["source_samples"] == 4800 and report["duration_ms"] == 100
            assert report["sample_rate_hz"] == 48000 and report["channels"] == 2
            assert consumed.available and consumed.facts == MediaFacts("audio/wav", 100, 48000, 2)
        else:
            assert result.status == "failed" and result.output == b""
            assert not consumed.available and consumed.facts is None and consumed.error_code is None
    finally:
        assert runner.close()
