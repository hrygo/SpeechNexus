"""Large boundary values must not be copied into collection and evidence identities."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "conftest.py"
PARAMETER_ID = runpy.run_path(str(HOOK))["pytest_make_parametrize_id"]


@pytest.mark.parametrize("value", [None, 1, True, "", "short", "界" * 256, b"x" * 256])
def test_small_values_keep_pytest_default_id_policy(value: object) -> None:
    assert PARAMETER_ID(value, "payload") is None


def test_long_value_ids_are_stable_bounded_and_do_not_echo_payload() -> None:
    values = ["x" * 257, "😀" * 257, "\ud800" * 257, b"x" * 257]
    identities = []
    for value in values:
        identity = PARAMETER_ID(value, "payload")
        assert identity is not None and len(identity) < 128
        assert identity == PARAMETER_ID(value, "payload")
        assert identity.startswith("payload-") and "sha256-" in identity
        identity.encode("ascii")
        identities.append(identity)
    assert len(set(identities)) == len(values)


def test_real_collection_keeps_duplicates_unique_and_explicit_ids_without_large_names(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copyfile(HOOK, tests / "conftest.py")
    (tests / "test_payloads.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('payload', "
        "['😀'*1700, 'x'*1700, b'x'*1700, '😀'*1700, 'small'])\n"
        "def test_boundary(payload):\n    raise AssertionError('must only collect')\n"
        "@pytest.mark.parametrize('payload', ['x'*1700], ids=['explicit'])\n"
        "def test_explicit(payload):\n    raise AssertionError('must only collect')\n"
    )
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    nodeids = [line for line in result.stdout.splitlines() if line.startswith("tests/")]
    assert len(nodeids) == len(set(nodeids)) == 6
    longest = max(map(len, nodeids))
    assert longest < 200, f"collection copied a payload into an ID: {longest} characters"
    assert len(json.dumps(nodeids)) < 2048
    assert any(nodeid.endswith("[explicit]") for nodeid in nodeids)
    assert any(nodeid.endswith("[small]") for nodeid in nodeids)
