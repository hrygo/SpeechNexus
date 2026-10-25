"""The two self-contained realtime probes, run as evidence instead of orphaned scripts.

Both probes stand up their own loopback synthetic speech producer, serve the production bundle
and drive it with Chromium: the ASR one runs three synthetic sessions and the TTS one drives a
synthetic dub through local rendering. They existed but were wrapped by nothing, so no gate ran
them and their evidence was untraceable.

This is synthetic-producer evidence: no model, no physical device, no speech base, no audio
quality claim. It is the C-side realtime consumer path only.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from tests.integration.workbench_real_runtime import ROOT

pytestmark = pytest.mark.integration

PROBES = {
    "asr": "scripts/dev-env/workbench-realtime-asr-probe.mjs",
    "tts": "scripts/dev-env/workbench-realtime-tts-probe.mjs",
}


async def run_probe(name: str, output: Path, *arguments: str) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "node",
        PROBES[name],
        "--output",
        str(output),
        *arguments,
        cwd=ROOT,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout and process.stderr
    stderr_task = asyncio.create_task(process.stderr.read())
    lines: list[str] = []
    try:
        async with asyncio.timeout(600):
            while line := await process.stdout.readline():
                lines.append(line.decode())
            assert await process.wait() == 0, (await stderr_task).decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        (output / "stdout.log").write_text("".join(lines))
        (output / "stderr.log").write_bytes(await stderr_task)
    assert lines, f"the {name} probe must report what it did"
    payload: dict[str, Any] = json.loads(lines[-1])
    return payload


def output_dir(name: str) -> Path:
    root = Path(
        os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
    )
    output = root / f"realtime-synthetic-{name}"
    output.mkdir(parents=True, exist_ok=True)
    return output


def directory_exists(path: str) -> bool:
    # A small sync helper: the async tests must not call blocking path operations directly.
    return Path(path).is_dir()


async def test_the_synthetic_asr_producer_drives_the_production_consumer() -> None:
    result = await run_probe("asr", output_dir("asr"))
    assert result["passed"] is True, result
    # Three synthetic sessions with ten committed writes: the page accepted the synthetic
    # producer's segments and committed the confirmed text it received.
    assert result["sessions"] == 3, result
    assert result["writes"] == 10, result
    assert directory_exists(result["output"])


async def test_the_synthetic_tts_producer_is_rendered_without_loss() -> None:
    result = await run_probe("tts", output_dir("tts"))
    assert result["ok"] is True, result
    assert result["sessions"] == 1, result
    assert result["writes"] == 5, result
    # The strong property: every sample the synthetic producer delivered was rendered locally.
    assert result["received"] == 48000, result
    assert result["rendered"] == result["received"], result


@pytest.mark.parametrize("mode", ["delayed", "rejected"])
async def test_tts_native_cleanup_is_observed_after_remote_done(mode: str) -> None:
    result = await run_probe("tts", output_dir(f"tts-cleanup-{mode}"), "--cleanup-mode", mode)
    assert result["ok"] is True, result
    assert result["cleanup_mode"] == mode, result
    assert result["actual_audio_worklet"] is True, result
    assert result["native_close_fault_injection"] is True, result
    assert result["physical_device"] is False, result
    assert 0 <= result["stopped_at"] < result["received"] == 48000, result
    assert result["close_calls"] == 1, result
    assert result["remote_cancel_commands"] == 0, result
    assert result["errors"] == result["serverErrors"] == result["outbound"] == [], result
