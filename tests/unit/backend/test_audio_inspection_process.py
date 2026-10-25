"""Real Linux namespaces, kernel limits and kill/wait; synthetic input only."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import selectors
import shutil
import socket
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryFile
from typing import BinaryIO

import pytest

from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    AudioInspectionProcess,
    InspectionProcessLimits,
    InspectionProcessResult,
)

LIMITS = InspectionProcessLimits(1024, 256 * 1024**2, 2, 5.0)


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
    stdlib = Path(sysconfig.get_path("stdlib"))
    shutil.copytree(
        stdlib,
        root / stdlib.relative_to("/"),
        ignore=shutil.ignore_patterns(
            "site-packages", "__pycache__", "test", "tests", "ensurepip", "idlelib", "tkinter"
        ),
    )
    for directory in ("tmp", "proc", "dev", "input"):
        (root / directory).mkdir(exist_ok=True)
    (root / "input/media").touch()
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
    return AudioInspectionProcess(root, (executable, "-I", "-S", "-c", code), limits)


def test_real_worker_sees_only_readonly_input_and_no_host_environment(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNTHETIC_INSPECTION_CREDENTIAL", "must-not-reach-worker")
    runner = worker(
        image,
        "import json,os; "
        "print(json.dumps({'body':open('/input/media','rb').read().decode(),"
        "'env':dict(os.environ),'pid':os.getpid(),'net':os.readlink('/proc/self/ns/net')}))",
    )
    result = runner.run(body, cancelled=lambda: False)
    assert result.status == "complete"
    facts = json.loads(result.output)
    assert facts["body"] == "synthetic-private-media"
    assert "SYNTHETIC_INSPECTION_CREDENTIAL" not in facts["env"]
    assert facts["net"] != str(Path("/proc/self/ns/net").readlink())
    assert facts["pid"] != os.getpid()
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
    while runner._process is None and time.monotonic() < deadline:
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
    results: list[InspectionProcessResult] = []
    thread = threading.Thread(
        target=lambda: results.append(runner.run(body, cancelled=cancelled.is_set))
    )
    thread.start()
    deadline = time.monotonic() + 3
    while runner._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert runner._process is not None
        original = runner._process
        with monkeypatch.context() as patch:

            def denied(*args: object) -> None:
                raise PermissionError("synthetic kill unavailable")

            patch.setattr(os, "killpg", denied)
            cancelled.set()
            thread.join(timeout=3)
            assert not thread.is_alive() and results[0].status == "stop_unknown"
            assert runner.run(body, cancelled=lambda: False).status == "stop_unknown"
            assert runner._process is original and original.poll() is None
            assert runner.close() is False
    finally:
        cancelled.set()
        runner.close()
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
        if runner._process is not None:
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
        AudioInspectionProcess(Path("/"), ("/usr/bin/true",), LIMITS)


def test_shutdown_cannot_confirm_a_concurrent_creator_before_its_handle_exists(
    image: tuple[Path, str], body: BinaryIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = worker(image, "import time; time.sleep(10)")
    entered, resume = threading.Event(), threading.Event()
    original = runner._arguments

    def paused(data_fd: int, seccomp_fd: int) -> list[str]:
        entered.set()
        if not resume.wait(timeout=3):
            raise RuntimeError("Synthetic creation barrier expired")
        return original(data_fd, seccomp_fd)

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


def test_api_process_crash_stops_its_owned_namespace_worker(image: tuple[Path, str]) -> None:
    root, executable = image
    script = """
import json, os, subprocess, sys, threading, time
from pathlib import Path
from tempfile import TemporaryFile
from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    AudioInspectionProcess, InspectionProcessLimits,
)
runner = AudioInspectionProcess(
    Path(sys.argv[1]), (sys.argv[2], '-I', '-S', '-c', 'import time; time.sleep(20)'),
    InspectionProcessLimits(1024, 256*1024**2, 10, 30),
)
with TemporaryFile() as data, TemporaryFile() as info:
    original = subprocess.Popen
    def launch(args, **kwargs):
        index = args.index('--unshare-user')
        args = args[:index] + ['--info-fd', str(info.fileno())] + args[index:]
        kwargs['pass_fds'] += (info.fileno(),)
        return original(args, **kwargs)
    subprocess.Popen = launch
    def observe():
        while True:
            try:
                child = json.loads(os.pread(info.fileno(), 4096, 0))['child-pid']
                if runner._process is not None:
                    print(runner._process.pid, child, flush=True)
                    return
            except (json.JSONDecodeError, KeyError):
                pass
            time.sleep(0.01)
    threading.Thread(target=observe, daemon=True).start()
    data.write(b'synthetic-crash-input'); data.seek(0)
    result = runner.run(data, cancelled=lambda: False)
    print(result.status, runner._process.returncode, flush=True)
"""
    helper = subprocess.Popen(
        [sys.executable, "-c", script, str(root), executable],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PYTHONPATH": str(Path("backend/src").resolve())},
    )
    worker_pid: int | None = None
    process_group: int | None = None
    try:
        assert helper.stdout is not None
        with selectors.DefaultSelector() as events:
            events.register(helper.stdout, selectors.EVENT_READ)
            assert events.select(timeout=5), "Synthetic API helper did not launch a worker"
            process_group, worker_pid = map(int, helper.stdout.readline(64).split())
        # bwrap reports its real namespace child. /proc/.../children requires
        # CONFIG_CHECKPOINT_RESTORE and is absent on the local kernel.
        owned = [process_group, worker_pid]
        assert all(Path(f"/proc/{pid}/stat").exists() for pid in owned)
        helper.kill()  # no finally/close in the synthetic API process
        helper.wait(timeout=3)

        def running(pid: int) -> bool:
            try:
                return Path(f"/proc/{pid}/stat").read_text().split()[2] not in ("Z", "X")
            except FileNotFoundError:
                return False

        deadline = time.monotonic() + 3
        while any(running(pid) for pid in owned) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(running(pid) for pid in owned)
    finally:
        if process_group is not None:
            with suppress(ProcessLookupError):
                os.killpg(process_group, 9)
        if helper.poll() is None:
            helper.kill()
        helper.wait(timeout=3)
        if helper.stdout is not None:
            helper.stdout.close()
