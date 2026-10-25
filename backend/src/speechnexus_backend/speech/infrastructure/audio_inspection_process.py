"""Private Linux inspection supervisor; no decoder, public DTO or ready claim.

The deployment supplies an immutable, minimal runtime image and its command.
Each worker sees one read-only input and a private PID/network/filesystem view.
Limits are installed before it starts. A seccomp filter prohibits additional
processes/threads so the address-space limit cannot be multiplied by forking.
"""

from __future__ import annotations

import array
import math
import os
import platform
import select
import signal
import socket
import stat
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryFile
from typing import BinaryIO, Literal

type InspectionProcessStatus = Literal[
    "complete",
    "unavailable",
    "busy",
    "failed",
    "output_limit",
    "timeout",
    "cancelled",
    "stop_unknown",
]


@dataclass(frozen=True, slots=True)
class InspectionProcessLimits:
    input_bytes: int
    memory_bytes: int
    cpu_seconds: int
    wall_seconds: float
    output_bytes: int = 4096

    def __post_init__(self) -> None:
        for value, low, high in (
            (self.input_bytes, 1, 1024**3),
            (self.memory_bytes, 64 * 1024**2, 1024**3),
            (self.cpu_seconds, 1, 30),
            (self.output_bytes, 1, 65536),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid inspection process limit")
        if (
            type(self.wall_seconds) not in (int, float)
            or not math.isfinite(self.wall_seconds)
            or not 0 < self.wall_seconds <= 60
        ):
            raise ValueError("Invalid inspection wall deadline")


@dataclass(frozen=True, slots=True)
class InspectionProcessResult:
    status: InspectionProcessStatus
    output: bytes = b""

    def __post_init__(self) -> None:
        if self.status != "complete" and self.output:
            raise ValueError("Incomplete inspection cannot expose worker output")


def _single_process_filter() -> bytes | None:
    # seccomp_data: nr at 0, audit architecture at 4. Fail closed for another
    # ABI; x32 cannot bypass the x86_64 syscall comparisons below.
    targets = {
        "x86_64": (0xC000003E, (56, 57, 58, 435, 272, 308), 157),
        "aarch64": (0xC00000B7, (220, 435, 97, 268), 167),
    }
    target = targets.get(platform.machine())
    if target is None:
        return None
    architecture, denied, prctl = target
    instructions = [
        (0x20, 0, 0, 4),  # load architecture
        (0x15, 1, 0, architecture),
        (0x06, 0, 0, 0x80000000),  # SECCOMP_RET_KILL_PROCESS
        (0x20, 0, 0, 0),  # load syscall number
        (0x35, 0, 1, 0x40000000),  # reject x32 / invalid high syscall numbers
        (0x06, 0, 0, 0x80000000),
    ]
    for syscall in denied:
        instructions.extend(((0x15, 0, 1, syscall), (0x06, 0, 0, 0x00050001)))  # EPERM
    instructions.extend(
        (
            (0x15, 0, 3, prctl),
            (0x20, 0, 0, 16),  # seccomp_data.args[0], prctl option
            (0x15, 0, 1, 1),  # PR_SET_PDEATHSIG cannot be cleared/replaced by payload
            (0x06, 0, 0, 0x00050001),
            (0x06, 0, 0, 0x7FFF0000),  # SECCOMP_RET_ALLOW
        )
    )
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in instructions)


class AudioInspectionProcess:
    """One local slot, retained until the original supervisor is actually reaped.

    This is not a gateway stop receipt or a global compute-budget authority.
    A caller must not interpret output as media facts without its separate
    decoder/result validation. The public upload runtime does not install this.
    """

    def __init__(
        self,
        runtime_root: Path,
        command: tuple[str, ...],
        limits: InspectionProcessLimits,
        *,
        launcher_python: str,
        bubblewrap: Path = Path("/usr/bin/bwrap"),
        prlimit: Path = Path("/usr/bin/prlimit"),
    ) -> None:
        root = runtime_root.resolve()
        if root == Path("/") or not root.is_dir() or not command:
            raise ValueError("Configure a private inspection runtime image")
        executable = PurePosixPath(command[0])
        if not executable.is_absolute() or ".." in executable.parts:
            raise ValueError("Configure an absolute image executable")
        if any(not isinstance(arg, str) or "\0" in arg or len(arg) > 8192 for arg in command):
            raise ValueError("Invalid inspection command")
        interpreter = PurePosixPath(launcher_python)
        if not interpreter.is_absolute() or ".." in interpreter.parts or "\0" in launcher_python:
            raise ValueError("Configure an absolute inspection interpreter")
        self._root, self._command, self._limits = root, command, limits
        self._python = launcher_python
        self._bubblewrap, self._prlimit = bubblewrap, prlimit
        self._slot = threading.Lock()
        self._identity = threading.RLock()
        self._closed = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._outer_pidfd: int | None = None
        self._worker_pidfd: int | None = None
        self._control: socket.socket | None = None
        self._reaped = True

    def run(self, source: BinaryIO, *, cancelled: Callable[[], bool]) -> InspectionProcessResult:
        if not self._slot.acquire(blocking=False):
            return InspectionProcessResult("busy")
        identity_owned = False
        try:
            self._identity.acquire()
            identity_owned = True
            if self._closed.is_set():
                return InspectionProcessResult("unavailable")
            # A previous kill with no wait receipt retains this instance's slot.
            if not self._original_exited():
                return InspectionProcessResult("stop_unknown")
            self._close_identities()
            self._process = None
            policy = _single_process_filter() if platform.system() == "Linux" else None
            if policy is None or not self._bubblewrap.is_file() or not self._prlimit.is_file():
                return InspectionProcessResult("unavailable")
            if cancelled():
                return InspectionProcessResult("cancelled")
            # Check kernel/API support without launching a worker. No PID-only
            # fallback can establish actual payload exit.
            probe = os.pidfd_open(os.getpid())
            try:
                signal.pidfd_send_signal(probe, 0)
            finally:
                os.close(probe)
            source.flush()
            facts = os.fstat(source.fileno())
            if not stat.S_ISREG(facts.st_mode) or not 0 < facts.st_size <= self._limits.input_bytes:
                return InspectionProcessResult("unavailable")
            control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            with (
                control,
                child_control,
                os.fdopen(os.open(f"/proc/self/fd/{source.fileno()}", os.O_RDONLY), "rb") as data,
                TemporaryFile() as seccomp,
                TemporaryFile() as output,
            ):
                # bwrap consumes the explicit FD into a bounded, anonymous
                # read-only input mount. No named private copy survives a crash.
                deadline = time.monotonic() + self._limits.wall_seconds
                if self._closed.is_set() or cancelled():
                    return InspectionProcessResult("cancelled")
                if time.monotonic() >= deadline:
                    return InspectionProcessResult("timeout")
                seccomp.write(policy)
                seccomp.seek(0)
                args = self._arguments(data.fileno(), seccomp.fileno())
                self._control = control
                control.setblocking(False)
                self._reaped = False
                with self._identity:
                    self._process = subprocess.Popen(
                        args,
                        stdin=child_control.fileno(),
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        pass_fds=(data.fileno(), seccomp.fileno()),
                        start_new_session=True,
                        env={},
                    )
                    # No concurrent closer can reap/reuse the numeric PID
                    # between child creation and opening its kernel identity.
                    self._outer_pidfd = os.pidfd_open(self._process.pid)
                child_control.close()
                starting = self._await_identity(cancelled, deadline)
                if starting is not None:
                    stopped = self._stop_original()
                    return InspectionProcessResult(starting if stopped else "stop_unknown")
                if self._closed.is_set() or cancelled() or time.monotonic() >= deadline:
                    status: InspectionProcessStatus = (
                        "cancelled" if self._closed.is_set() or cancelled() else "timeout"
                    )
                    stopped = self._stop_original()
                    return InspectionProcessResult(status if stopped else "stop_unknown")
                if control.send(b"G") != 1:
                    raise ValueError("Inspection start was not acknowledged")
                control.close()
                self._control = None
                status = "complete"
                while self._process.poll() is None:
                    if self._closed.is_set() or cancelled():
                        status = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        status = "timeout"
                        break
                    time.sleep(0.01)
                if status != "complete":
                    if not self._stop_original():
                        return InspectionProcessResult("stop_unknown")
                    return InspectionProcessResult(status)
                if not self._original_exited():
                    stopped = self._stop_original()
                    return InspectionProcessResult("failed" if stopped else "stop_unknown")
                output.seek(0)
                body = output.read(self._limits.output_bytes + 1)
                if len(body) > self._limits.output_bytes:
                    return InspectionProcessResult("output_limit")
                if self._process.returncode != 0:
                    return InspectionProcessResult("failed")
                # No success after the deadline or a cancellation racing EOF.
                if self._closed.is_set() or cancelled():
                    return InspectionProcessResult("cancelled")
                if time.monotonic() >= deadline:
                    return InspectionProcessResult("timeout")
                return InspectionProcessResult("complete", body)
        except BaseException as error:
            # No vendor text, command, environment, filename or input in errors.
            if identity_owned and self._process is None:
                self._reaped = True  # Popen did not return an owned child.
            stopped = self._stop_original()
            if not isinstance(error, Exception):
                raise
            if not stopped:
                return InspectionProcessResult("stop_unknown")
            return InspectionProcessResult("unavailable")
        finally:
            if identity_owned:
                self._control = None
                self._identity.release()
            self._slot.release()

    def _arguments(self, data_fd: int, seccomp_fd: int) -> list[str]:
        limits = self._limits
        return [
            str(self._prlimit),
            f"--as={limits.memory_bytes}:{limits.memory_bytes}",
            f"--cpu={limits.cpu_seconds}:{limits.cpu_seconds}",
            # Setup's anonymous encoded input is bounded separately. The image
            # prlimit narrows the file/output limit before the worker executable.
            f"--fsize={max(limits.input_bytes, limits.output_bytes + 1)}:"
            f"{max(limits.input_bytes, limits.output_bytes + 1)}",
            "--nofile=64:64",
            "--core=0:0",
            "--",
            str(self._bubblewrap),
            "--unshare-user",
            "--unshare-all",
            "--as-pid-1",
            "--disable-userns",
            "--die-with-parent",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            str(self._root),
            "/",
            "--ro-bind",
            str(Path(__file__).with_name("_audio_inspection_launcher.py")),
            "/inspection/launch.py",
            "--ro-bind-data",
            str(data_fd),
            "/input/media",
            "--proc",
            "/proc",
            "--remount-ro",
            "/proc",
            "--dev",
            "/dev",
            "--remount-ro",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--remount-ro",
            "/tmp",
            "--clearenv",
            "--setenv",
            "OPENBLAS_NUM_THREADS",
            "1",
            "--setenv",
            "OMP_NUM_THREADS",
            "1",
            "--chdir",
            "/tmp",
            "--seccomp",
            str(seccomp_fd),
            "--",
            "/usr/bin/prlimit",
            f"--fsize={limits.output_bytes + 1}:{limits.output_bytes + 1}",
            "--",
            self._python,
            "-I",
            "-S",
            "/inspection/launch.py",
            *self._command,
        ]

    def _receive_identity(self) -> bool:
        """Called only while the identity lifetime lock is owned."""
        if self._worker_pidfd is not None:
            return True
        if self._control is None:
            return False
        try:
            data, ancillary, flags, _ = self._control.recvmsg(
                64,
                socket.CMSG_SPACE(array.array("i").itemsize),
                socket.MSG_DONTWAIT | socket.MSG_CMSG_CLOEXEC,
            )
        except BlockingIOError, InterruptedError:
            return False
        descriptors = array.array("i")
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors.frombytes(value[: len(value) - len(value) % descriptors.itemsize])
        if not data and not descriptors:
            return False
        if (
            data != b"speechnexus-inspection-v1"
            or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            or len(descriptors) != 1
        ):
            for descriptor in descriptors:
                os.close(descriptor)
            raise ValueError("Invalid private inspection identity")
        descriptor = descriptors[0]
        try:
            signal.pidfd_send_signal(descriptor, 0)
        except ProcessLookupError:
            pass  # A pidfd for an already exited worker is still exit evidence.
        except BaseException:
            os.close(descriptor)
            raise
        self._worker_pidfd = descriptor
        return True

    def _await_identity(
        self, cancelled: Callable[[], bool], deadline: float
    ) -> InspectionProcessStatus | None:
        assert self._process is not None
        while not self._receive_identity():
            if self._closed.is_set() or cancelled():
                return "cancelled"
            if time.monotonic() >= deadline:
                return "timeout"
            if self._process.poll() is not None:
                return "failed"
            time.sleep(0.01)
        return None

    def _original_exited(self) -> bool:
        if self._reaped:
            return True
        if self._worker_pidfd is None or self._process is None:
            return False
        observer = select.poll()
        observer.register(self._worker_pidfd, select.POLLIN)
        exited = any(flags & select.POLLIN for _, flags in observer.poll(0))
        if not exited or self._process.poll() is None:
            return False
        self._reaped = True
        return True

    def _stop_original(self) -> bool:
        if not self._identity.acquire(blocking=False):
            return False
        try:
            return self._stop_with_identity()
        finally:
            self._identity.release()

    def _stop_with_identity(self) -> bool:
        # Caller retains the same identity lock through every signal, wait and
        # later close; another closer cannot recycle any referenced integer fd.
        process = self._process
        if process is None:
            return self._reaped
        if self._original_exited():
            return True
        # Startup may already have queued an identity before cancellation. If no
        # actual identity can be recovered, outer exit alone never frees a slot.
        with suppress(OSError, ValueError):
            self._receive_identity()
        # A trusted launcher may still be starting. It cannot execute a payload
        # without our acknowledgement, so collect its identity before killing it.
        identity_deadline = time.monotonic() + 1
        while (
            self._worker_pidfd is None
            and process.poll() is None
            and time.monotonic() < identity_deadline
        ):
            try:
                if self._receive_identity():
                    break
            except OSError, ValueError:
                break
            time.sleep(0.01)
        for descriptor in (self._worker_pidfd, self._outer_pidfd):
            if descriptor is not None:
                with suppress(OSError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        if self._outer_pidfd is None:
            # Best-effort cleanup of Python's still-owned child if pidfd_open
            # itself failed; this cannot establish the payload's exit proof.
            with suppress(OSError):
                process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
        with suppress(OSError, ValueError):
            self._receive_identity()
        if self._worker_pidfd is None:
            return False
        if not self._original_exited():
            with suppress(OSError):
                signal.pidfd_send_signal(self._worker_pidfd, signal.SIGKILL)
            observer = select.poll()
            observer.register(self._worker_pidfd, select.POLLIN)
            observer.poll(1000)
        return self._original_exited()

    def _close_identities(self) -> None:
        """Caller owns the identity lock through the last use and descriptor close."""
        assert self._reaped
        for descriptor in (self._worker_pidfd, self._outer_pidfd):
            if descriptor is not None:
                os.close(descriptor)
        self._worker_pidfd = self._outer_pidfd = None

    def close(self) -> bool:
        self._closed.set()
        # A concurrent creator may not have published its Popen handle yet.
        # Never confirm closure until that run leaves the slot and no original
        # process remains to be reaped.
        if not self._slot.acquire(blocking=False):
            self._stop_original()
            return False
        try:
            if not self._identity.acquire(blocking=False):
                return False
            try:
                stopped = self._stop_original()
                if stopped:
                    self._close_identities()
                return stopped
            finally:
                self._identity.release()
        finally:
            self._slot.release()
