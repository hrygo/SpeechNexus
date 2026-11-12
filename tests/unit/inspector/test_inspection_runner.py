"""Deployment limits, prlimit wiring and the parent-side status classification."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from tests.unit.inspector.support import PRLIMIT, TOKEN, settings

from speechnexus_asset_inspector.runner import SubprocessRunner
from speechnexus_asset_inspector.service import self_check_wav
from speechnexus_asset_inspector.settings import ConfigurationError, InspectionSettings


def test_env_requires_only_the_token() -> None:
    resolved = InspectionSettings.from_env({"SPEECHNEXUS_INSPECTION_TOKEN": TOKEN})
    assert resolved.token == TOKEN.encode("ascii")
    assert resolved.host == "0.0.0.0"
    assert resolved.port == 8080
    assert resolved.concurrency == 2
    assert resolved.max_encoded_bytes == 4 * 1024 * 1024
    assert resolved.max_duration_ms == 120_000
    assert resolved.max_wall_seconds == 20.0
    assert resolved.memory_bytes == 256 * 1024 * 1024
    assert resolved.cpu_seconds == 5
    assert resolved.stdout_limit == 4096


def test_env_overrides_are_parsed_and_validated() -> None:
    resolved = InspectionSettings.from_env(
        {
            "SPEECHNEXUS_INSPECTION_TOKEN": TOKEN,
            "SPEECHNEXUS_INSPECTION_HOST": "127.0.0.1",
            "SPEECHNEXUS_INSPECTION_PORT": "9000",
            "SPEECHNEXUS_INSPECTION_CONCURRENCY": "4",
            "SPEECHNEXUS_INSPECTION_MAX_ENCODED_BYTES": "1024",
            "SPEECHNEXUS_INSPECTION_MAX_DURATION_MS": "500",
            "SPEECHNEXUS_INSPECTION_MAX_WALL_SECONDS": "2.5",
            "SPEECHNEXUS_INSPECTION_MEMORY_BYTES": "67108864",
            "SPEECHNEXUS_INSPECTION_CPU_SECONDS": "1",
            "SPEECHNEXUS_INSPECTION_OUTPUT_BYTES": "512",
            "SPEECHNEXUS_INSPECTION_MAX_RECEIPT_BYTES": "256",
        }
    )
    assert resolved.host == "127.0.0.1"
    assert resolved.port == 9000
    assert resolved.concurrency == 4
    assert resolved.max_wall_seconds == 2.5
    assert resolved.stdout_limit == 256


@pytest.mark.parametrize(
    "env,name",
    [
        ({}, "SPEECHNEXUS_INSPECTION_TOKEN"),
        ({"SPEECHNEXUS_INSPECTION_TOKEN": ""}, "SPEECHNEXUS_INSPECTION_TOKEN"),
        ({"SPEECHNEXUS_INSPECTION_TOKEN": "has space"}, "SPEECHNEXUS_INSPECTION_TOKEN"),
        (
            {"SPEECHNEXUS_INSPECTION_TOKEN": TOKEN, "SPEECHNEXUS_INSPECTION_PORT": "nope"},
            "SPEECHNEXUS_INSPECTION_PORT",
        ),
        (
            {"SPEECHNEXUS_INSPECTION_TOKEN": TOKEN, "SPEECHNEXUS_INSPECTION_CONCURRENCY": "5"},
            "SPEECHNEXUS_INSPECTION_CONCURRENCY",
        ),
        (
            {"SPEECHNEXUS_INSPECTION_TOKEN": TOKEN, "SPEECHNEXUS_INSPECTION_MAX_WALL_SECONDS": "0"},
            "SPEECHNEXUS_INSPECTION_MAX_WALL_SECONDS",
        ),
    ],
)
def test_bad_configuration_names_the_variable(env: dict[str, str], name: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        InspectionSettings.from_env(env)


def test_token_is_absent_from_the_dataclass_repr() -> None:
    resolved = InspectionSettings.from_env({"SPEECHNEXUS_INSPECTION_TOKEN": TOKEN})
    assert TOKEN not in repr(resolved)
    assert "token" not in repr(resolved).lower()


def test_stdout_limit_takes_the_smaller_receipt_bound() -> None:
    assert settings(output_bytes=100, max_receipt_bytes=50).stdout_limit == 50
    assert settings(output_bytes=50, max_receipt_bytes=100).stdout_limit == 50


def test_prlimit_wrapper_installs_every_child_limit(tmp_path: Path) -> None:
    fake = tmp_path / "prlimit"
    fake.write_text("")
    resolved = settings(memory_bytes=256 * 1024 * 1024, cpu_seconds=5, output_bytes=4096)
    runner = SubprocessRunner(resolved, prlimit=fake)

    argv = runner.command(
        input_fd=7,
        max_encoded_bytes=1024,
        max_duration_ms=1000,
        timeout_seconds=1.5,
    )

    assert argv[:1] == [str(fake)]
    assert "--as=268435456:268435456" in argv
    assert "--cpu=5:5" in argv
    assert "--fsize=4097:4097" in argv
    assert "--nofile=64:64" in argv
    assert "--core=0:0" in argv
    assert argv[argv.index("--") + 1 :] == [
        *runner.child_command(),
        "--input-fd",
        "7",
        "--max-encoded-bytes",
        "1024",
        "--max-duration-ms",
        "1000",
        "--timeout-seconds",
        "1.500000",
    ]


def test_removing_prlimit_leaves_the_bare_child(tmp_path: Path) -> None:
    runner = SubprocessRunner(settings(), prlimit=None)
    argv = runner.command(
        input_fd=3,
        max_encoded_bytes=1024,
        max_duration_ms=1000,
        timeout_seconds=1.0,
    )
    assert argv == [
        *runner.child_command(),
        "--input-fd",
        "3",
        "--max-encoded-bytes",
        "1024",
        "--max-duration-ms",
        "1000",
        "--timeout-seconds",
        "1.000000",
    ]


def test_child_environment_carries_no_service_secret() -> None:
    environment = SubprocessRunner(settings(), prlimit=None).child_environment
    assert not any("TOKEN" in name for name in environment)
    assert not any(name.startswith("SPEECHNEXUS_") for name in environment)
    assert environment["OPENBLAS_NUM_THREADS"] == "1"
    assert environment["OMP_NUM_THREADS"] == "1"
    assert environment["MKL_NUM_THREADS"] == "1"


def test_classify_distinguishes_cooperative_limit_from_wall_deadline() -> None:
    async def probe() -> None:
        loop = asyncio.get_running_loop()
        runner = SubprocessRunner(settings(), prlimit=None)
        now = loop.time()
        assert (
            runner._classify(1, b'{"status":"media_limit_exceeded"}', now + 10).status
            == "media_limit_exceeded"
        )
        assert (
            runner._classify(1, b'{"status":"media_limit_exceeded"}', now - 1).status == "deadline"
        )

    asyncio.run(probe())


def test_classify_rejects_inconsistent_child_output() -> None:
    async def probe() -> None:
        loop = asyncio.get_running_loop()
        runner = SubprocessRunner(settings(), prlimit=None)
        now = loop.time()
        assert runner._classify(0, b"warning\n{}", now + 10).status == "unavailable"
        assert runner._classify(0, b'{"status":"complete"}', now + 10).status == "unavailable"
        assert (
            runner._classify(0, b'{"schema_version":1,"status":"complete"}', now + 10).status
            == "unavailable"
        )
        assert (
            runner._classify(1, b'{"status":"invalid_media"}', now + 10).status == "invalid_media"
        )
        assert runner._classify(1, b'{"status":"device_lost"}', now + 10).status == "unavailable"
        assert runner._classify(0, b"[]", now + 10).status == "unavailable"
        assert runner._classify(0, b"\xff", now + 10).status == "unavailable"

    asyncio.run(probe())


def test_parent_kills_a_child_that_ignores_its_cooperative_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent wall clock is the hard stop, not the child's own timeout."""

    async def probe() -> None:
        runner = SubprocessRunner(settings(), prlimit=None)
        monkeypatch.setattr(runner, "command", lambda **_kwargs: ["/bin/sleep", "5"])
        input_path = tmp_path / "input"
        input_path.write_bytes(b"")
        descriptor = os.open(input_path, os.O_RDONLY)
        try:
            started = time.monotonic()
            outcome = await runner.run(
                input_fd=descriptor,
                max_encoded_bytes=1024,
                max_duration_ms=1000,
                wall_seconds=0.05,
            )
            assert outcome.status == "deadline"
            assert time.monotonic() - started < 1.5
        finally:
            os.close(descriptor)

    asyncio.run(probe())


@pytest.mark.skipif(not PRLIMIT.is_file(), reason="prlimit is only installed on Linux")
def test_child_process_really_sees_the_configured_address_space() -> None:
    """The real child reports kernel limits; skipped where prlimit is absent."""

    async def probe() -> None:
        resolved = settings(memory_bytes=256 * 1024 * 1024, cpu_seconds=5, output_bytes=4096)
        runner = SubprocessRunner(resolved, prlimit=PRLIMIT)
        process = await asyncio.create_subprocess_exec(
            *runner.probe_command(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(20):
                out, _ = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        limits = json.loads(out)["limits"]
        assert limits["as"] == 256 * 1024 * 1024
        assert limits["cpu"] == 5
        assert limits["fsize"] == 4097
        assert limits["nofile"] == 64
        assert limits["core"] == 0

    asyncio.run(probe())


@pytest.mark.skipif(not PRLIMIT.is_file(), reason="prlimit is only installed on Linux")
def test_decoder_dependencies_import_inside_the_production_address_space() -> None:
    """Real Av/NumPy/inspection imports fit the 256 MiB child, not just the numbers."""

    async def probe() -> None:
        resolved = settings()
        assert resolved.memory_bytes == 256 * 1024 * 1024
        runner = SubprocessRunner(resolved, prlimit=PRLIMIT)
        process = await asyncio.create_subprocess_exec(
            *runner.dependency_probe_command(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=runner.child_environment,
        )
        try:
            async with asyncio.timeout(60):
                out, err = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, err.decode("utf-8", "replace")
        report = json.loads(out)
        assert set(report["dependencies"]) == {
            "av",
            "numpy",
            "speechnexus_media.inspection",
        }
        assert report["thread_environment"] == {
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }

    asyncio.run(probe())


@pytest.mark.skipif(not PRLIMIT.is_file(), reason="prlimit is only installed on Linux")
def test_real_decode_completes_inside_the_production_address_space(tmp_path: Path) -> None:
    """The built-in 5 ms WAV decodes through the real child at 256 MiB."""

    async def probe() -> None:
        resolved = settings()
        assert resolved.memory_bytes == 256 * 1024 * 1024
        runner = SubprocessRunner(resolved, prlimit=PRLIMIT)
        content = self_check_wav()
        with tempfile.TemporaryFile(dir=tmp_path) as body:
            body.write(content)
            body.flush()
            body.seek(0)
            outcome = await runner.run(
                input_fd=body.fileno(),
                max_encoded_bytes=resolved.max_encoded_bytes,
                max_duration_ms=resolved.max_duration_ms,
                wall_seconds=min(resolved.max_wall_seconds, 10.0),
            )
        assert outcome.status == "complete", outcome.status
        receipt = json.loads(outcome.output)
        assert receipt["status"] == "complete"
        assert receipt["media_type"] == "audio/wav"
        assert receipt["duration_ms"] == 5
        assert receipt["encoded_bytes"] == len(content)

    asyncio.run(probe())
