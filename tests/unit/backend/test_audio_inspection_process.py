"""Real Linux namespaces, kernel limits and kill/wait; synthetic input only."""

from __future__ import annotations

import _ctypes
import io
import json
import os
import platform
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, BinaryIO

import pytest

from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    AudioInspectionProcess,
    InspectionProcessLimits,
    InspectionProcessResult,
)

LIMITS = InspectionProcessLimits(1024, 256 * 1024**2, 2, 5.0)
_OWNED: list[AudioInspectionProcess] = []


@pytest.fixture(autouse=True)
def close_owned_processes() -> Iterator[None]:
    yield
    while _OWNED:
        _OWNED.pop().close()


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    # A small image of the current interpreter/stdlib, never the host root,
    # project environment, site-packages, credentials or deployment contents.
    root = tmp_path_factory.mktemp("inspection-image")
    executable = Path(sys.executable).resolve()
    for binary in (executable, Path("/usr/bin/prlimit")):
        target = root / binary.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
        libraries = subprocess.run(
            ["ldd", str(binary)], check=True, capture_output=True, text=True
        ).stdout
        for library in re.findall(r"/[^\s()]+", libraries):
            source = Path(library)
            destination = root / source.relative_to("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    extension = getattr(_ctypes, "__file__", None)
    if isinstance(extension, str):
        linked = subprocess.run(
            ["ldd", extension], check=True, capture_output=True, text=True
        ).stdout
        for library in re.findall(r"/[^\s()]+", linked):
            source = Path(library)
            destination = root / source.relative_to("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    stdlib = Path(sysconfig.get_path("stdlib"))
    shutil.copytree(
        stdlib,
        root / stdlib.relative_to("/"),
        ignore=shutil.ignore_patterns(
            "site-packages", "__pycache__", "test", "tests", "ensurepip", "idlelib", "tkinter"
        ),
        dirs_exist_ok=True,
    )
    for directory in ("tmp", "proc", "dev", "input", "inspection"):
        (root / directory).mkdir(exist_ok=True)
    (root / "input/media").touch()
    (root / "inspection/launch.py").touch()
    # Only this fixed synthetic probe captures startup diagnostics. Production
    # workers keep stderr closed and never expose vendor/input information.
    probe_runner = AudioInspectionProcess(
        root,
        (str(executable), "-I", "-S", "-c", "print('inspection-probe')"),
        LIMITS,
        launcher_python=str(executable),
    )
    with TemporaryFile() as data, TemporaryFile() as diagnostics:
        data.write(b"synthetic-probe")
        data.seek(0)
        original = subprocess.Popen

        def diagnosed(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            kwargs["stderr"] = diagnostics
            return original(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(subprocess, "Popen", diagnosed)
            probe = probe_runner.run(data, cancelled=lambda: False)
        diagnostics.seek(0)
        message = diagnostics.read(1024)
        assert probe.status == "complete" and probe.output == b"inspection-probe\n", (
            f"Synthetic sandbox startup rejected ({probe.status}): {message!r}"
        )
        assert probe_runner.close()
    return root, str(executable)


@pytest.fixture
def body() -> Iterator[BinaryIO]:
    with TemporaryFile() as source:
        source.write(b"synthetic-private-media")
        source.seek(7)
        yield source


def worker(
    image: tuple[Path, str], code: str, limits: InspectionProcessLimits = LIMITS
) -> AudioInspectionProcess:
    root, executable = image
    runner = AudioInspectionProcess(
        root, (executable, "-I", "-S", "-c", code), limits, launcher_python=executable
    )
    _OWNED.append(runner)
    return runner


def test_real_worker_sees_only_readonly_input_and_no_host_environment(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNTHETIC_INSPECTION_CREDENTIAL", "must-not-reach-worker")
    runner = worker(
        image,
        "import json,os; "
        "caps=next(line.split()[1] for line in open('/proc/self/status') "
        "if line.startswith('CapEff:')); "
        "print(json.dumps({'body':open('/input/media','rb').read().decode(),"
        "'env':dict(os.environ),'pid':os.getpid(),'net':os.readlink('/proc/self/ns/net'),"
        "'caps':caps}))",
    )
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "complete"
    facts = json.loads(result.output)
    assert facts["body"] == "synthetic-private-media"
    assert "SYNTHETIC_INSPECTION_CREDENTIAL" not in facts["env"]
    assert facts["net"] != str(Path("/proc/self/ns/net").readlink())
    assert facts["pid"] != os.getpid()
    assert int(facts["caps"], 16) == 0
    assert body.tell() == 7
    assert runner._process is not None and runner._process.poll() == 0


@pytest.mark.parametrize("path", ["/input/media", "/tmp/new-file", "/dev/new-file"])
def test_readonly_mounts_prevent_input_changes_and_unbounded_scratch(
    image: tuple[Path, str], body: BinaryIO, path: str
) -> None:
    runner = worker(
        image,
        "import errno,json; "
        f"path={path!r}\n"
        "try:\n open(path,'wb').write(b'not allowed')\n"
        "except OSError as error:\n print(error.errno)\n",
    )
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "complete" and int(result.output) == 30  # EROFS
    body.seek(0)
    assert body.read() == b"synthetic-private-media"


def test_worker_cannot_connect_to_parent_loopback(image: tuple[Path, str], body: BinaryIO) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        runner = worker(
            image,
            "import socket; s=socket.socket(); s.settimeout(0.2); "
            f"print(s.connect_ex(('127.0.0.1',{listener.getsockname()[1]})))",
        )
        result = runner.run(body, cancelled=lambda: False)
        assert result.status == "complete" and int(result.output) != 0


def test_seccomp_prevents_fork_and_thread_memory_multiplication(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    runner = worker(
        image,
        "import os,threading\n"
        "try:\n os.fork()\nexcept PermissionError:\n print('fork-denied')\n"
        "try:\n threading.Thread(target=lambda:None).start()\n"
        "except RuntimeError:\n print('thread-denied')\n",
    )
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "complete"
    assert result.output == b"fork-denied\nthread-denied\n"


def test_kernel_address_space_limit_prevents_large_allocation(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    runner = worker(
        image,
        "try:\n bytearray(384*1024*1024)\nexcept MemoryError:\n print('memory-denied')\n",
    )
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "complete" and result.output == b"memory-denied\n"


def test_kernel_cpu_limit_stops_noncooperative_worker(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    runner = worker(image, "while True: pass", InspectionProcessLimits(1024, 256 * 1024**2, 1, 8))
    started = time.monotonic()
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "failed" and result.output == b""
    # bwrap waits for its namespace child and reports 128 + SIGKILL. The CPU
    # limit fires well before the independent eight-second wall deadline.
    assert runner._process is not None and runner._process.returncode == 137
    assert time.monotonic() - started < 5


def test_wall_timeout_kills_and_reaps_before_slot_reuse(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    runner = worker(
        image,
        "import time; time.sleep(10)",
        InspectionProcessLimits(1024, 256 * 1024**2, 1, 0.3),
    )
    started = time.monotonic()
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "timeout" and not result.output
    assert time.monotonic() - started < 3
    assert runner._process is not None and runner._process.poll() is not None
    # The same slot admits a second original inspection only after wait/reap.
    assert runner.run(body, cancelled=lambda: False).status == "timeout"


def test_output_is_bounded_without_exposing_partial_bytes(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    runner = worker(image, "import os; os.write(1,b'x'*100000)")
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "output_limit" and not result.output


def test_cancel_and_busy_preserve_one_owned_process(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    runner = worker(image, "import time; time.sleep(10)")
    cancelled = threading.Event()
    results: list[InspectionProcessResult] = []
    thread = threading.Thread(
        target=lambda: results.append(runner.run(body, cancelled=cancelled.is_set))
    )
    thread.start()
    deadline = time.monotonic() + 3
    while (
        runner._worker_pidfd is None or runner._control is not None
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert runner._process is not None
        original = runner._process
        assert runner.run(body, cancelled=lambda: False).status == "busy"
        cancelled.set()
    finally:
        cancelled.set()
        thread.join(timeout=3)
        runner.close()
    assert not thread.is_alive()
    assert len(results) == 1 and results[0].status == "cancelled"
    assert original.poll() is not None
    assert runner.run(body, cancelled=lambda: False).status == "unavailable"


def test_no_worker_is_started_for_invalid_or_unavailable_boundaries(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = worker(image, "print('unexpected')")
    assert runner.run(body, cancelled=lambda: True).status == "cancelled"
    assert (
        runner.run(io.BytesIO(b"not a private file"), cancelled=lambda: False).status
        == "unavailable"
    )
    monkeypatch.setattr(platform, "machine", lambda: "unknown-architecture")
    assert runner.run(body, cancelled=lambda: False).status == "unavailable"
    assert runner._process is None


def test_unconfirmed_kill_keeps_original_slot_and_never_spawns_again(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = worker(image, "import time; time.sleep(10)")
    cancelled = threading.Event()
    completed, release_creator = threading.Event(), threading.Event()
    results: list[InspectionProcessResult] = []

    def invoke() -> None:
        try:
            results.append(runner.run(body, cancelled=cancelled.is_set))
        finally:
            completed.set()
            release_creator.wait(timeout=10)

    thread = threading.Thread(target=invoke)
    thread.start()
    deadline = time.monotonic() + 3
    while (
        runner._worker_pidfd is None or runner._control is not None
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert runner._process is not None
        original = runner._process
        with monkeypatch.context() as patch:
            send_signal = signal.pidfd_send_signal

            def denied(fd: int, number: int) -> None:
                if number == 0:
                    send_signal(fd, number)
                    return
                raise PermissionError("synthetic kill unavailable")

            patch.setattr(signal, "pidfd_send_signal", denied)
            cancelled.set()
            assert completed.wait(timeout=3) and results[0].status == "stop_unknown"
            assert runner.run(body, cancelled=lambda: False).status == "stop_unknown"
            assert runner._process is original and original.poll() is None
            assert runner.close() is False
    finally:
        cancelled.set()
        runner.close()
        release_creator.set()
        thread.join(timeout=3)
    assert original.poll() is not None


def test_anonymous_encoded_input_can_exceed_worker_output_limit(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    body.seek(0)
    body.write(b"x" * 8192)
    runner = worker(
        image,
        "print(len(open('/input/media','rb').read()))",
        InspectionProcessLimits(16384, 256 * 1024**2, 1, 5, 16),
    )
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "complete" and result.output == b"8192\n"


def test_base_exception_in_cancellation_still_kills_and_reaps(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    runner = worker(image, "import time; time.sleep(10)")

    def interrupted() -> bool:
        if runner._worker_pidfd is not None:
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        runner.run(body, cancelled=interrupted)
    assert runner._process is not None and runner._process.poll() is not None


def test_missing_sandbox_never_falls_back_to_host_execution(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    root, executable = image
    runner = AudioInspectionProcess(
        root,
        (executable, "-c", "print('must not run')"),
        LIMITS,
        launcher_python=executable,
        bubblewrap=root / "no-such-bwrap",
    )
    assert runner.run(body, cancelled=lambda: False).status == "unavailable"
    assert runner._process is None


@pytest.mark.parametrize("wall", [0, -1, float("nan"), float("inf"), True])
def test_invalid_deadlines_are_rejected(wall: float) -> None:
    with pytest.raises(ValueError, match="deadline"):
        InspectionProcessLimits(1024, 256 * 1024**2, 1, wall)


def test_host_root_cannot_be_used_as_worker_image() -> None:
    with pytest.raises(ValueError, match="private inspection runtime"):
        AudioInspectionProcess(
            Path("/"), ("/usr/bin/true",), LIMITS, launcher_python="/usr/bin/python3"
        )


def test_shutdown_cannot_confirm_a_concurrent_creator_before_its_handle_exists(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = worker(image, "import time; time.sleep(10)")
    entered, resume = threading.Event(), threading.Event()
    original = runner._arguments

    def paused(data_fd: int, seccomp_fd: int, deadline: float) -> list[str]:
        entered.set()
        if not resume.wait(timeout=3):
            raise RuntimeError("Synthetic creation barrier expired")
        return original(data_fd, seccomp_fd, deadline)

    monkeypatch.setattr(runner, "_arguments", paused)
    results: list[InspectionProcessResult] = []
    thread = threading.Thread(
        target=lambda: results.append(runner.run(body, cancelled=lambda: False))
    )
    thread.start()
    try:
        assert entered.wait(timeout=3)
        pending = runner._process
        assert pending is None
        assert runner.close() is False
    finally:
        resume.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and results[0].status == "cancelled"
    assert runner._process is not None and runner._process.poll() is not None
    assert runner.close() is True


@pytest.mark.parametrize("phase", ["running", "before_ack"])
def test_api_process_crash_stops_its_owned_namespace_worker(
    image: tuple[Path, str], phase: str
) -> None:
    from tests.unit.backend.test_audio_inspection_parent_death import assert_api_crash_stops_owned

    assert_api_crash_stops_owned(image, phase=phase)


def test_detached_payload_cannot_clear_parent_death_and_has_actual_exit_proof(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = (
        "import ctypes,os; c=ctypes.CDLL(None,use_errno=True); "
        "assert c.prctl(1,0,0,0,0)==-1 and ctypes.get_errno()==1; "
        "os.setsid(); assert os.read(0,1)==b''; print('guarded-detached',flush=True)\n"
        "while True: pass"
    )
    runner = worker(image, code, InspectionProcessLimits(1024, 256 * 1024**2, 3, 0.7))
    capture: list[int] = []
    original = subprocess.Popen

    def observe(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        capture.append(os.dup(kwargs["stdout"].fileno()))
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", observe)
    try:
        result = runner.run(body, cancelled=lambda: False)
        assert result.status == "timeout" and result.output == b""
        assert os.pread(capture[0], 128, 0) == b"guarded-detached\n"
        identity = runner._worker_pidfd
        assert identity is not None
        observer = select.poll()
        observer.register(identity, select.POLLIN)
        assert any(flags & select.POLLIN for _, flags in observer.poll(0))
        assert runner._process is not None and runner._process.poll() is not None
    finally:
        runner.close()
        for descriptor in capture:
            os.close(descriptor)


@pytest.mark.parametrize("live_identity", ["worker", "guardian"])
def test_reaped_outer_with_live_actual_identity_retains_slot(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch, live_identity: str
) -> None:
    # Controlled real OS identities model an already-dead wrapper and a live
    # payload. No media, permissions or unrelated process is involved.
    runner = worker(image, "print('must not be dispatched')")
    outer = subprocess.Popen(["/usr/bin/true"])
    runner._outer_pidfd = os.pidfd_open(outer.pid)
    outer.wait(timeout=3)
    actual = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runner._process = outer
    live_fd = os.pidfd_open(actual.pid)
    exited_fd = os.dup(runner._outer_pidfd)
    runner._worker_pidfd, runner._guardian_pidfd = (
        (live_fd, exited_fd) if live_identity == "worker" else (exited_fd, live_fd)
    )
    runner._reaped = False
    signal_identity = signal.pidfd_send_signal
    try:
        with monkeypatch.context() as patch:

            def deny_actual(fd: int, number: int) -> None:
                if fd == live_fd and number == signal.SIGKILL:
                    raise PermissionError("Synthetic actual stop unavailable")
                signal_identity(fd, number)

            patch.setattr(signal, "pidfd_send_signal", deny_actual)
            assert runner.run(body, cancelled=lambda: False).status == "stop_unknown"
            assert outer.poll() == 0 and actual.poll() is None
            assert runner.close() is False
            assert actual.poll() is None
        assert runner.close() is True
        actual.wait(timeout=3)
    finally:
        if actual.poll() is None:
            actual.kill()
        actual.wait(timeout=3)
        runner.close()


def test_payload_cannot_tamper_with_guardian_or_inherit_control_capabilities(
    image: tuple[Path, str], body: BinaryIO
) -> None:
    code = """
import ctypes, errno, os, platform
lib = ctypes.CDLL(None, use_errno=True)
calls = {
    'x86_64': [(101, 2, 1, 0, 0), (310, 1, 0, 0, 0, 0, 0),
               (311, 1, 0, 0, 0, 0, 0), (438, -1, 0, 0),
               (62, 1, 0), (129, 1, 0, 0), (297, 1, 1, 0, 0),
               (425, 0, 0), (426, -1, 0, 0, 0, 0, 0), (427, -1, 0, 0, 0)],
    'aarch64': [(117, 2, 1, 0, 0), (270, 1, 0, 0, 0, 0, 0),
                (271, 1, 0, 0, 0, 0, 0), (438, -1, 0, 0),
                (129, 1, 0), (138, 1, 0, 0), (240, 1, 1, 0, 0),
                (425, 0, 0), (426, -1, 0, 0, 0, 0, 0), (427, -1, 0, 0, 0)]
}
# Nonmutating/invalid-argument probes only, all within this owned namespace.
for args in calls[platform.machine()]:
    ctypes.set_errno(0)
    assert lib.syscall(*args) == -1 and ctypes.get_errno() == errno.EPERM, args[0]
try:
    fd = os.open('/proc/1/mem', os.O_RDONLY)
except PermissionError:
    pass
else:
    os.close(fd)
    raise AssertionError('Guardian memory must be inaccessible')
for entry in os.listdir('/proc/self/fd'):
    try:
        target = os.readlink('/proc/self/fd/' + entry)
    except FileNotFoundError:
        continue
    assert target != 'anon_inode:[pidfd]' and not target.startswith('socket:')
print('sealed')
"""
    result = worker(image, code).run(body, cancelled=lambda: False)
    assert result.status == "complete" and result.output == b"sealed\n"


def test_concurrent_closers_cannot_close_a_pidfd_while_another_uses_it(
    image: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Controlled run-slot/exit barriers model run finishing between two closes.
    # The signal hook never forwards a signal; only our own sleeper is killed.
    runner = worker(image, "print('not dispatched')")
    outer = subprocess.Popen(["/usr/bin/true"])
    runner._outer_pidfd = os.pidfd_open(outer.pid)
    outer.wait(timeout=3)
    actual = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity = os.pidfd_open(actual.pid)
    runner._process, runner._worker_pidfd, runner._reaped = outer, identity, False
    runner._guardian_pidfd = os.dup(runner._outer_pidfd)
    entered, resume, stale = threading.Event(), threading.Event(), threading.Event()
    first_result: list[bool] = []

    def paused_signal(fd: int, number: int) -> None:
        if fd == identity and number == signal.SIGKILL:
            entered.set()
            if not resume.wait(timeout=3):
                raise RuntimeError("Synthetic signal barrier expired")
        try:
            os.fstat(fd)
        except OSError:
            stale.set()

    runner._slot.acquire()
    slot_owned = True
    thread = threading.Thread(target=lambda: first_result.append(runner.close()))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(signal, "pidfd_send_signal", paused_signal)
            thread.start()
            assert entered.wait(timeout=3)
            actual.kill()
            actual.wait(timeout=3)
            runner._slot.release()
            slot_owned = False
            try:
                assert runner.close() is False
                os.fstat(identity)  # the paused user still owns a valid handle
            finally:
                resume.set()
                thread.join(timeout=3)
        assert not thread.is_alive() and first_result == [False]
        assert not stale.is_set()
        assert runner.close() is True
    finally:
        resume.set()
        if slot_owned:
            runner._slot.release()
        if thread.ident is not None:
            thread.join(timeout=3)
        if actual.poll() is None:
            actual.kill()
        actual.wait(timeout=3)
        runner.close()
