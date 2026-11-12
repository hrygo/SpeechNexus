"""One bounded decoder child per inspection, with a hard parent-owned deadline.

The parent applies ``prlimit`` before the child starts, passes only the input
descriptor, discards stderr, caps stdout, and kills the child's process group
when the wall clock expires. ``prlimit`` may exec the command or fork it, so the
runner always signals the whole session and never relies on one numeric PID.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, Protocol

from speechnexus_asset_inspector.protocol import SCHEMA_VERSION
from speechnexus_asset_inspector.settings import InspectionSettings

_LOG = logging.getLogger(__name__)

type RunStatus = Literal[
    "complete",
    "invalid_media",
    "media_limit_exceeded",
    "unavailable",
    "deadline",
]

# Minimal child environment: no inspection bearer token, no service credentials
# and no inherited PATH that could resolve an unexpected executable.
_CHILD_ENVIRONMENT: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


class InspectionRunner(Protocol):
    async def run(
        self,
        *,
        input_fd: int,
        max_encoded_bytes: int,
        max_duration_ms: int,
        wall_seconds: float,
    ) -> RunOutcome: ...


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunStatus
    output: bytes = b""

    def __post_init__(self) -> None:
        if self.status != "complete" and self.output:
            raise ValueError("Incomplete inspection cannot expose child output")


def fatal_exit(code: int) -> NoReturn:
    # A child that cannot be reaped inside one second is not recoverable in
    # this process; namespace destruction on container restart is the only
    # deterministic stop. Nothing is logged with request content.
    _LOG.critical("inspection service exiting fatally for restart (code %s)", code)
    os._exit(code)


class SubprocessRunner:
    """Launch and bound exactly one decoder child."""

    def __init__(
        self,
        settings: InspectionSettings,
        *,
        prlimit: Path | None,
        module: str = "speechnexus_asset_inspector.child",
        executable: str | None = None,
        exit_process: Callable[[int], NoReturn] = fatal_exit,
    ) -> None:
        if prlimit is not None and not prlimit.is_file():
            raise ValueError("Configured prlimit executable does not exist")
        self._settings = settings
        self._prlimit = prlimit
        self._module = module
        self._executable = sys.executable if executable is None else executable
        self._exit_process = exit_process
        self._stdout_limit = settings.stdout_limit

    @property
    def stdout_limit(self) -> int:
        return self._stdout_limit

    @property
    def child_environment(self) -> dict[str, str]:
        """The exact environment passed to the decoder, without secrets."""
        return dict(_CHILD_ENVIRONMENT)

    def child_command(self) -> list[str]:
        """The unwrapped decoder argv; also the probe used by limit tests."""
        return [self._executable, "-I", "-m", self._module]

    def probe_command(self) -> list[str]:
        """Run the child's limit report under the same wrapper as a real decode."""
        return self._wrap([*self.child_command(), "--report-limits"])

    def command(
        self,
        *,
        input_fd: int,
        max_encoded_bytes: int,
        max_duration_ms: int,
        timeout_seconds: float,
    ) -> list[str]:
        if input_fd < 0 or max_encoded_bytes < 1 or max_duration_ms < 1:
            raise ValueError("Invalid decoder invocation")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Invalid decoder deadline")
        child = [
            *self.child_command(),
            "--input-fd",
            str(input_fd),
            "--max-encoded-bytes",
            str(max_encoded_bytes),
            "--max-duration-ms",
            str(max_duration_ms),
            "--timeout-seconds",
            f"{timeout_seconds:.6f}",
        ]
        return self._wrap(child)

    def _wrap(self, child: Sequence[str]) -> list[str]:
        settings = self._settings
        if self._prlimit is None:
            return list(child)
        memory = settings.memory_bytes
        cpu = settings.cpu_seconds
        fsize = settings.output_bytes + 1
        return [
            str(self._prlimit),
            f"--as={memory}:{memory}",
            f"--cpu={cpu}:{cpu}",
            f"--fsize={fsize}:{fsize}",
            "--nofile=64:64",
            "--core=0:0",
            "--",
            *child,
        ]

    async def run(
        self,
        *,
        input_fd: int,
        max_encoded_bytes: int,
        max_duration_ms: int,
        wall_seconds: float,
    ) -> RunOutcome:
        deadline = asyncio.get_running_loop().time() + wall_seconds
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return RunOutcome("deadline")
        argv = self.command(
            input_fd=input_fd,
            max_encoded_bytes=max_encoded_bytes,
            max_duration_ms=max_duration_ms,
            timeout_seconds=remaining,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            pass_fds=(input_fd,),
            start_new_session=True,
            env=_CHILD_ENVIRONMENT,
        )
        try:
            async with asyncio.timeout_at(deadline):
                output, overflow = await self._read_bounded(process)
                if overflow:
                    await self._terminate(process)
                    return RunOutcome("unavailable")
                await process.wait()
        except TimeoutError:
            await self._terminate(process)
            return RunOutcome("deadline")
        finally:
            if process.returncode is None:
                await self._terminate(process)
        return self._classify(process.returncode, output, deadline)

    async def _read_bounded(self, process: asyncio.subprocess.Process) -> tuple[bytes, bool]:
        if process.stdout is None:
            return b"", False
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                return b"".join(chunks), False
            total += len(chunk)
            if total > self._stdout_limit:
                return b"", True
            chunks.append(chunk)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            # The child is a session leader, so the group covers prlimit's own
            # status and any grandchild that inherited the stdout pipe.
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
        try:
            async with asyncio.timeout(1.0):
                await process.wait()
        except TimeoutError:
            self._exit_process(70)

    def _classify(self, returncode: int | None, output: bytes, deadline: float) -> RunOutcome:
        if returncode is None:
            return RunOutcome("unavailable")
        try:
            payload = json.loads(output)
        except UnicodeDecodeError, ValueError:
            return RunOutcome("unavailable")
        if not isinstance(payload, dict):
            return RunOutcome("unavailable")
        status = payload.get("status")
        if returncode == 0:
            if (
                status != "complete"
                or payload.get("schema_version") != SCHEMA_VERSION
                or not isinstance(payload.get("recipe_version"), str)
            ):
                return RunOutcome("unavailable")
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if len(canonical) > self._settings.max_receipt_bytes:
                return RunOutcome("unavailable")
            return RunOutcome("complete", canonical)
        if status == "media_limit_exceeded":
            expired = asyncio.get_running_loop().time() >= deadline
            return RunOutcome("deadline" if expired else "media_limit_exceeded")
        if status == "invalid_media":
            return RunOutcome("invalid_media")
        return RunOutcome("unavailable")
