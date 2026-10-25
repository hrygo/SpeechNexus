"""Check both Linux syscall ABIs without requiring a second host or policy load."""

from __future__ import annotations

import platform
import struct
from typing import Literal

import pytest

from speechnexus_backend.speech.infrastructure import audio_inspection_process as process


def verdict(program: bytes, arch: int, number: int, argument: int = 0) -> int:
    """Evaluate only the emitted classic-BPF subset against seccomp_data words."""
    instructions = list(struct.iter_unpack("=HBBI", program))
    words = {0: number, 4: arch, 16: argument}
    accumulator, index = 0, 0
    while index < len(instructions):
        code, yes, no, constant = instructions[index]
        if code == 0x06:  # RET K
            return int(constant)
        if code == 0x20:  # LD W ABS
            accumulator = words[constant]
        elif code == 0x15:  # JMP JEQ K
            index += yes if accumulator == constant else no
        elif code == 0x35:  # JMP JGE K
            index += yes if accumulator >= constant else no
        else:
            raise AssertionError(f"Unexamined BPF instruction: {code}")
        index += 1
    raise AssertionError("BPF fell through without a decision")


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
@pytest.mark.parametrize("role", ["bootstrap", "guardian", "payload"])
def test_guardian_policy_preserves_trust_and_abi_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    role: Literal["bootstrap", "guardian", "payload"],
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: machine)
    program = process._single_process_filter(role=role)
    assert program is not None
    # Linux asm syscall tables, including queued signals absent from kill/tgkill.
    if machine == "x86_64":
        arch, prctl, read = 0xC000003E, 157, 0
        forks = [56, 57, 58, 435]
        peer = [101, 310, 311, 312, 438, 272, 308, 425, 426, 427]
        signals = [62, 129, 200, 234, 297, 424]
    else:
        arch, prctl, read = 0xC00000B7, 167, 63
        forks = [220, 435]
        peer = [117, 270, 271, 272, 438, 97, 268, 425, 426, 427]
        signals = [129, 130, 131, 138, 240, 424]
    for number in peer:
        assert verdict(program, arch, number) == 0x00050001
    for number in forks:
        assert verdict(program, arch, number) == (0x7FFF0000 if role == "bootstrap" else 0x00050001)
    for number in signals:
        assert verdict(program, arch, number) == (0x00050001 if role == "payload" else 0x7FFF0000)
    assert verdict(program, arch, prctl, 1) == 0x00050001  # SET_PDEATHSIG
    assert verdict(program, arch, prctl, 4) == 0x7FFF0000  # SET_DUMPABLE
    assert verdict(program, arch, read) == 0x7FFF0000
    assert verdict(program, arch ^ 1, read) == 0x80000000
    assert verdict(program, arch, 0x40000000 | read) == 0x80000000
