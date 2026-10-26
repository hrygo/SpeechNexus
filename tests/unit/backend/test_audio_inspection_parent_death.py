"""Owned pidfd crash probes; injected exec clearing never changes host policy."""

from __future__ import annotations

import array
import os
import select
import signal
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryFile

import pytest

BOOTSTRAP = r"""
import ctypes, importlib.util, os, sys
class Filter(ctypes.Structure):
    _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_=[('length',ctypes.c_ushort),('filters',ctypes.POINTER(Filter))]
lib=ctypes.CDLL(None,use_errno=True)
# A controlled stand-in for LSM exec clearing, before the production filter.
# This affects only our own synthetic child, with no host policy changes.
assert lib.prctl(1,0,0,0,0)==0
assert lib.prctl(38,1,0,0,0)==0
payload=os.pread(int(sys.argv[1]),8192,0)
filters=(Filter*(len(payload)//8)).from_buffer_copy(payload)
program=Program(len(filters),filters)
assert lib.prctl(22,2,ctypes.byref(program),0,0)==0
path=sys.argv[2]
sys.argv=[path,*sys.argv[3:]]
spec=importlib.util.spec_from_file_location('owned_launcher',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module.main()
"""

API = r"""
import array,json,os,socket,subprocess,sys,threading,time
from pathlib import Path
from tempfile import TemporaryFile
from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    AudioInspectionProcess, InspectionProcessLimits,
)
root,executable,clear=sys.argv[1:4]
bootstrap=json.loads(sys.argv[4])
phase=sys.argv[5]
runner=AudioInspectionProcess(Path(root),(executable,'-I','-S','-c',
    "import time; print('payload-started',flush=True); time.sleep(20)"),
    InspectionProcessLimits(1024,256*1024**2,10,30),launcher_python=executable)
with socket.socket(fileno=0) as test_control, TemporaryFile() as data:
    original=subprocess.Popen;outputs=[];ready=threading.Event()
    receive=runner._receive_identity
    def hold_before_ack():
        found=receive()
        if found and phase=='before_ack':
            ready.set()
            # Deliberately crash the helper before it can acknowledge execution.
            threading.Event().wait(10)
        return found
    runner._receive_identity=hold_before_ack
    def launch(args,**kwargs):
        outputs.append(kwargs['stdout'])
        if clear=='yes':
            kwargs['stderr']=sys.stderr.fileno()
            offset=max(i for i,value in enumerate(args) if value=='/inspection/launch.py')
            source=Path(__import__('speechnexus_backend.speech.infrastructure.audio_inspection_process',fromlist=['x']).__file__).with_name('_audio_inspection_launcher.py')
            args=[sys.executable,'-I','-S','-c',bootstrap,args[args.index('--seccomp')+1],str(source),*args[offset+1:]]
        return original(args,**kwargs)
    subprocess.Popen=launch
    def observe():
        while True:
            started=outputs and b'payload-started' in os.pread(outputs[0].fileno(),256,0)
            if started or ready.is_set():
                if phase=='before_ack':
                    assert not started
                descriptors=[runner._outer_pidfd,getattr(runner,'_guardian_pidfd',None),runner._worker_pidfd]
                if descriptors[0] is not None and descriptors[-1] is not None:
                    # This helper performs one run, never calls close, and keeps
                    # all identity descriptors until its deliberate crash.
                    descriptors=array.array('i',[fd for fd in descriptors if fd is not None])
                    test_control.sendmsg([b'owned-ready'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,descriptors)])
                    return
            time.sleep(.01)
    threading.Thread(target=observe,daemon=True).start()
    data.write(b'synthetic-crash-input');data.seek(0)
    result=runner.run(data,cancelled=lambda:False)
    print(result.status,file=sys.stderr,flush=True)
"""


def assert_api_crash_stops_owned(
    image: tuple[Path, str], *, simulate_exec_clear: bool = False, phase: str = "running"
) -> None:
    import json

    root, executable = image
    owned = array.array("i")
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child, TemporaryFile() as diagnostics:
        helper = subprocess.Popen(
            [
                sys.executable,
                "-c",
                API,
                str(root),
                executable,
                "yes" if simulate_exec_clear else "no",
                json.dumps(BOOTSTRAP),
                phase,
            ],
            stdin=child.fileno(),
            stdout=subprocess.DEVNULL,
            stderr=diagnostics,
            env={"PYTHONPATH": str(Path("backend/src").resolve())},
        )
        child.close()
        try:
            events = select.poll()
            events.register(parent, select.POLLIN)
            assert events.poll(5000), "Synthetic API did not report actual worker readiness"
            data, ancillary, flags, _ = parent.recvmsg(32, socket.CMSG_SPACE(3 * owned.itemsize))
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    owned.frombytes(payload[: len(payload) - len(payload) % owned.itemsize])
            diagnostics.seek(0)
            assert data == b"owned-ready" and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC), (
                diagnostics.read(2048)
            )
            assert len(owned) == 3, "Keep actual worker, guardian and wrapper identities"
            helper.kill()
            helper.wait(timeout=3)
            pending = set(owned)
            observer = select.poll()
            for descriptor in owned:
                observer.register(descriptor, select.POLLIN)
            import time

            deadline = time.monotonic() + 3
            while pending and time.monotonic() < deadline:
                for descriptor, event in observer.poll(
                    max(1, int((deadline - time.monotonic()) * 1000))
                ):
                    if event & select.POLLIN:
                        observer.unregister(descriptor)
                        pending.discard(descriptor)
            assert not pending, f"Owned kernel identities survived API crash: {sorted(pending)}"
        finally:
            if helper.poll() is None:
                helper.kill()
            helper.wait(timeout=3)
            for descriptor in owned:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                observer = select.poll()
                observer.register(descriptor, select.POLLIN)
                assert observer.poll(3000), "Owned probe cleanup must confirm actual exit"
                os.close(descriptor)


@pytest.mark.parametrize("phase", ["running", "before_ack"])
def test_exec_cleared_death_signal_cannot_orphan_the_actual_worker(
    tmp_path: Path, phase: str
) -> None:
    assert_api_crash_stops_owned((tmp_path, sys.executable), simulate_exec_clear=True, phase=phase)
