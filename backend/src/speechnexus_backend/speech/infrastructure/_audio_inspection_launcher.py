"""Read-only non-exec guardian; actual worker and API lifetimes use pidfds."""

from __future__ import annotations

import array
import ctypes
import math
import os
import select
import signal
import socket
import struct
import sys
import time
from contextlib import suppress
from pathlib import Path


class _Filter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(_Filter))]


def _install_filter(raw: bytes) -> None:
    if not raw or len(raw) % 8 or len(raw) > 8192:
        raise ValueError("Invalid private syscall program")
    filters = (_Filter * (len(raw) // 8)).from_buffer_copy(raw)
    program = _Program(len(filters), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise ValueError("Inspection syscall sealing failed")


def _exited(descriptor: int) -> bool:
    observer = select.poll()
    observer.register(descriptor, select.POLLIN)
    events = observer.poll(0)
    if any(flags & select.POLLIN for _, flags in events):
        return True
    if events:
        raise ValueError("Unknown private process state")
    return False


def _receive_parent(control: socket.socket) -> tuple[int, bytes, bytes]:
    data, ancillary, flags, _ = control.recvmsg(
        8192, socket.CMSG_SPACE(array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC
    )
    descriptors = array.array("i")
    for level, kind, raw in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors.frombytes(raw[: len(raw) - len(raw) % descriptors.itemsize])
    try:
        if (
            flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            or len(descriptors) != 1
            or len(data) < 6
            or data[:2] != b"P3"
        ):
            raise ValueError("Invalid private parent binding")
        guardian_length, worker_length = struct.unpack(">HH", data[2:6])
        if guardian_length + worker_length != len(data) - 6:
            raise ValueError("Invalid private syscall lengths")
        if str(Path(f"/proc/self/fd/{descriptors[0]}").readlink()) != "anon_inode:[pidfd]":
            raise ValueError("Parent binding is not a kernel identity")
        return descriptors[0], data[6 : 6 + guardian_length], data[6 + guardian_length :]
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _wait_channel(channel: socket.socket, parent_fd: int, worker_fd: int, deadline: float) -> None:
    observer = select.poll()
    for descriptor in (channel.fileno(), parent_fd, worker_fd):
        observer.register(descriptor, select.POLLIN | select.POLLHUP)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Inspection start expired")
        events = dict(observer.poll(max(1, math.ceil(remaining * 1000))))
        if parent_fd in events or _exited(parent_fd):
            raise ValueError("Original API exited before inspection start")
        if worker_fd in events:
            raise ValueError("Inspection child exited before start")
        if channel.fileno() in events:
            return


def _worker(
    control: socket.socket,
    parent_gate: socket.socket,
    gate: socket.socket,
    parent_fd: int,
    guardian_fd: int,
    policy: bytes,
    command: list[str],
    deadline: float,
) -> None:
    # No API/control capability survives into untrusted code. The guardian was
    # made nondumpable before fork; final exec may reset only this child's flag.
    control.close()
    parent_gate.close()
    os.close(parent_fd)
    os.close(guardian_fd)
    null = os.open("/dev/null", os.O_RDONLY)
    if null != 0:
        os.dup2(null, 0)
        os.close(null)
    os.set_inheritable(0, True)
    _install_filter(policy)
    gate.settimeout(max(0.001, deadline - time.monotonic()))
    gate.send(b"R")  # Trusted sealing readiness, before any vendor/command exec.
    if gate.recv(1) != b"G":
        raise ValueError("Inspection start was not acknowledged")
    gate.close()
    os.execv(command[0], command)


def main() -> None:
    deadline = float(sys.argv[1])
    command = sys.argv[2:]
    if (
        not math.isfinite(deadline)
        or deadline <= time.monotonic()
        or not command
        or not command[0].startswith("/")
    ):
        raise ValueError("Invalid private inspection command")
    parent_fd: int | None = None
    guardian_fd: int | None = None
    worker_fd: int | None = None
    child: int | None = None
    waited = False
    result = 78
    with socket.socket(fileno=0) as control:
        control.settimeout(max(0.001, deadline - time.monotonic()))
        parent_fd, guardian_policy, worker_policy = _receive_parent(control)
        try:
            if _exited(parent_fd):
                raise ValueError("Original API is no longer alive")
            # Protect the non-exec guardian without relying on host Yama.
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
                raise ValueError("Inspection guardian could not become nondumpable")
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            guardian_fd = os.pidfd_open(os.getpid())
            parent_gate, child_gate = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            with parent_gate, child_gate:
                child = os.fork()  # The only trusted fork, before untrusted execution.
                if child == 0:
                    try:
                        _worker(
                            control,
                            parent_gate,
                            child_gate,
                            parent_fd,
                            guardian_fd,
                            worker_policy,
                            command,
                            deadline,
                        )
                    except BaseException:
                        os._exit(78)
                child_gate.close()
                # Our unreaped child cannot have its numeric PID recycled.
                worker_fd = os.pidfd_open(child)
                _install_filter(guardian_policy)  # Guardian can no longer fork either.
                control.sendmsg(
                    [b"speechnexus-inspection-v3"],
                    [
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_RIGHTS,
                            array.array("i", [guardian_fd, worker_fd]),
                        )
                    ],
                )
                _wait_channel(parent_gate, parent_fd, worker_fd, deadline)
                if parent_gate.recv(1) != b"R":
                    raise ValueError("Inspection child was not sealed")
                _wait_channel(control, parent_fd, worker_fd, deadline)
                if control.recv(1) != b"G" or _exited(parent_fd) or time.monotonic() >= deadline:
                    raise ValueError("Inspection start was not acknowledged")
                parent_gate.send(b"G")
                parent_gate.close()
                control.close()
                observer = select.poll()
                observer.register(parent_fd, select.POLLIN)
                observer.register(worker_fd, select.POLLIN)
                timed_out = False
                while not _exited(worker_fd):
                    remaining = deadline - time.monotonic()
                    if _exited(parent_fd) or remaining <= 0:
                        timed_out = remaining <= 0
                        signal.pidfd_send_signal(worker_fd, signal.SIGKILL)
                        break
                    observer.poll(max(1, math.ceil(remaining * 1000)))
                _, status = os.waitpid(child, 0)
                waited = True
                code = os.waitstatus_to_exitcode(status)
                result = 124 if timed_out else code if code >= 0 else 128 - code
        finally:
            if child is not None and child > 0 and not waited:
                if worker_fd is None:
                    # Own unreaped child only; not a PID-only stop receipt.
                    with suppress(ProcessLookupError):
                        os.kill(child, signal.SIGKILL)
                elif not _exited(worker_fd):
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(worker_fd, signal.SIGKILL)
                os.waitpid(child, 0)
            for descriptor in (worker_fd, guardian_fd, parent_fd):
                if descriptor is not None:
                    os.close(descriptor)
    os._exit(result)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        os._exit(78)
