"""Execution evidence for browser probes; scenario assertions remain the caller's responsibility."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict


class ProcessExit(Protocol):
    @property
    def returncode(self) -> int | None: ...


@dataclass(frozen=True)
class ObservedProbe:
    command: tuple[str, ...]
    process: ProcessExit


class ProbeExecutionEvidence(TypedDict):
    script: str
    command: list[str]
    exit_code: int


def require_probe_execution(
    script: str, observed: Sequence[ObservedProbe]
) -> ProbeExecutionEvidence:
    assert len(observed) == 1, f"{script} must launch exactly one subprocess, got {len(observed)}"
    run = observed[0]
    assert run.command[:2] == ("node", script), f"expected probe {script}, got {run.command}"
    assert run.process.returncode == 0, (
        f"{script} must have completed successfully, got {run.process.returncode}"
    )
    return {"script": script, "command": list(run.command), "exit_code": 0}
