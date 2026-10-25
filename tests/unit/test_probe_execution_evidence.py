"""A named probe is evidence only when its subprocess actually completed successfully."""

from dataclasses import dataclass

import pytest

from tests.integration.probe_execution import ObservedProbe, require_probe_execution


@dataclass
class ProcessResult:
    returncode: int | None


SCRIPT = "scripts/dev-env/workbench-manuscripts-probe.mjs"
COMMAND = ("node", SCRIPT, "--output", "synthetic-result.json")


def test_a_retained_script_reference_without_execution_is_not_evidence() -> None:
    with pytest.raises(AssertionError, match="exactly one subprocess"):
        require_probe_execution(SCRIPT, [])


@pytest.mark.parametrize("returncode", [None, 1, -9])
def test_an_unfinished_or_failed_subprocess_is_not_evidence(returncode: int | None) -> None:
    with pytest.raises(AssertionError, match="completed successfully"):
        require_probe_execution(SCRIPT, [ObservedProbe(COMMAND, ProcessResult(returncode))])


def test_a_different_probe_or_duplicate_execution_cannot_fill_the_expected_slot() -> None:
    process = ProcessResult(0)
    wrong = ObservedProbe(("node", "scripts/dev-env/another-probe.mjs"), process)
    with pytest.raises(AssertionError, match="expected probe"):
        require_probe_execution(SCRIPT, [wrong])
    repeated = ObservedProbe(COMMAND, process)
    with pytest.raises(AssertionError, match="exactly one subprocess"):
        require_probe_execution(SCRIPT, [repeated, repeated])


def test_a_successful_expected_subprocess_produces_an_execution_record() -> None:
    evidence = require_probe_execution(SCRIPT, [ObservedProbe(COMMAND, ProcessResult(0))])
    assert evidence == {"script": SCRIPT, "command": list(COMMAND), "exit_code": 0}
