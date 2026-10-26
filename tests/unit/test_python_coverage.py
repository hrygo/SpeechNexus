"""Small synthetic checks for the parallel gate; no databases or business suite run here."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from coverage import CoverageData

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev-env/python-coverage.py"
SPEC = importlib.util.spec_from_file_location("python_coverage_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
HEAD = "a" * 40
NODEIDS = sorted(
    f"tests/unit/test_{file}.py::test_{case}" for file in range(4) for case in range(2)
)


def setup_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    app = root / "app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "sample.py").write_text(
        "def choose(flag):\n    if flag:\n        return 1\n    return 2\n"
    )
    (root / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\nsource = ["app"]\n'
        "[tool.coverage.report]\nfail_under = 80\n"
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
    )
    monkeypatch.setattr(GATE, "ROOT", root)
    monkeypatch.setattr(GATE, "DRIVER", root / "scripts/dev-env/workbench-pg-probe.py")
    return root


def coverage_file(root: Path, index: int, *, branches: bool = True) -> Path:
    path = root / f".runtime/team-c/pg-{index}/.coverage"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = CoverageData(basename=str(path))
    source = str(root / "app/sample.py")
    if branches:
        branch = [(2, 3), (3, -1)] if index == 0 else [(2, 4), (4, -1)]
        data.add_arcs({source: [(-1, 1), (1, -1), (-1, 2), *branch]})
    else:
        data.add_lines({source: [1, 2, 3]})
    data.write()
    return path


def reports(root: Path) -> list[Path]:
    paths = []
    for index in range(2):
        selected = GATE.partition(NODEIDS, index, 2)
        data = coverage_file(root, index)
        report = root / f"worker-{index}.json"
        report.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_head": HEAD,
                    "config_digest": GATE.digest(root / "pyproject.toml"),
                    "index": index,
                    "count": 2,
                    "collection": NODEIDS,
                    "selected": selected,
                    "outcomes": dict.fromkeys(selected, "passed"),
                    "exit_code": 0,
                    "executed": True,
                    "coverage_file": str(data.relative_to(root)),
                    "coverage_digest": GATE.digest(data),
                    "elapsed_seconds": 1,
                }
            )
        )
        paths.append(report)
    return paths


def test_file_affinity_has_a_complete_deterministic_balanced_union() -> None:
    full = sorted(
        f"tests/unit/test_{file}.py::test_{case}"
        for file, count in enumerate((7, 5, 3, 1))
        for case in range(count)
    )
    lanes = [GATE.partition(full, index, 2) for index in range(2)]
    assert [len(lane) for lane in lanes] == [8, 8]
    assert sorted(lanes[0] + lanes[1]) == full
    assert not set(lanes[0]).intersection(lanes[1])
    assert GATE.partition(full, 0, 1) == full
    for index in range(2):
        assert GATE.partition(list(reversed(full)), index, 2) == lanes[index]
        assert {item.split("::")[0] for item in lanes[index]}.isdisjoint(
            item.split("::")[0] for item in lanes[1 - index]
        )


@pytest.mark.parametrize(
    "case",
    [
        "missing-worker",
        "duplicate-worker",
        "head",
        "config",
        "count",
        "boolean-index",
        "different-collection",
        "wrong-partition",
        "missing-outcome",
        "skip",
        "failure",
        "collect-only",
        "failed-exit",
        "boolean-exit",
        "changed-data",
        "line-only",
        "outside-runtime",
        "nan-duration",
        "extra-field",
        "duplicate-json-field",
    ],
)
def test_incomplete_execution_or_coverage_cannot_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    paths = reports(root)
    assert GATE.verify_reports(paths, head=HEAD, count=2)[1] == len(NODEIDS)
    data = json.loads(paths[0].read_text())
    if case == "missing-worker":
        paths.pop()
    elif case == "duplicate-worker":
        paths[1] = paths[0]
    elif case == "head":
        data["source_head"] = "b" * 40
    elif case == "config":
        data["config_digest"] = "0" * 64
    elif case == "count":
        data["count"] = 1
    elif case == "boolean-index":
        data["index"] = False
    elif case == "different-collection":
        data["collection"] = sorted([*NODEIDS, "tests/unit/test_new.py::test_new"])
    elif case == "wrong-partition":
        data["selected"] = GATE.partition(NODEIDS, 1, 2)
    elif case == "missing-outcome":
        data["outcomes"].pop(data["selected"][0])
    elif case in ("skip", "failure"):
        data["outcomes"][data["selected"][0]] = case
    elif case == "collect-only":
        data["executed"] = False
    elif case == "failed-exit":
        data["exit_code"] = 1
    elif case == "boolean-exit":
        data["exit_code"] = False
    elif case == "changed-data":
        data["coverage_digest"] = "0" * 64
    elif case == "line-only":
        (root / data["coverage_file"]).unlink()
        data["coverage_digest"] = GATE.digest(coverage_file(root, 0, branches=False))
    elif case == "outside-runtime":
        data["coverage_file"] = "../elsewhere"
    elif case == "nan-duration":
        data["elapsed_seconds"] = float("nan")
    elif case == "extra-field":
        data["private"] = True
    paths[0].write_text(json.dumps(data))
    if case == "duplicate-json-field":
        paths[0].write_text(json.dumps(data)[:-1] + ',"exit_code":0}')
    with pytest.raises(ValueError):
        GATE.verify_reports(paths, head=HEAD, count=2)


@pytest.mark.parametrize("complete", [False, True])
def test_only_combined_data_enforces_the_unchanged_eighty_percent_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete: bool,
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    left, right = coverage_file(root, 0), coverage_file(root, 1)
    output = root / "combined"
    output.mkdir()
    percent, code = GATE.combine_coverage([left, right] if complete else [left], output)
    assert (code == 0) == complete
    assert (percent >= 80) == complete
    assert left.is_file() and right.is_file()  # Evidence remains available after combine.
    assert (
        json.loads((output / "coverage.json").read_text())["totals"]["percent_covered"] == percent
    )


@pytest.mark.parametrize("condition", ["success", "failed-exit", "unstopped", "missing-report"])
def test_parent_requires_both_probe_lifecycles_before_combining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    originals = reports(root)
    monkeypatch.setattr(GATE, "source_head", lambda: HEAD)
    actual_run = subprocess.run
    completed: list[int] = []
    prepared: list[bool] = []
    monkeypatch.setattr(GATE, "prepare_shared_bucket", lambda: prepared.append(True))

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if str(GATE.DRIVER) not in command:
            return actual_run(command, **kwargs)
        assert prepared == [True]
        index = int(command[command.index("--python-shard") + 1])
        report = Path(command[command.index("--python-shard-report") + 1])
        output = Path(command[command.index("--output") + 1])
        if condition != "missing-report" or index != 0:
            shutil.copyfile(originals[index], report)
        output.write_text(
            json.dumps({"tests_passed": True, "stopped": condition != "unstopped" or index != 0})
        )
        completed.append(index)
        return subprocess.CompletedProcess(
            command, int(condition == "failed-exit" and index == 0), stdout="", stderr=""
        )

    monkeypatch.setattr(GATE.subprocess, "run", run)
    args = argparse.Namespace(output=root / "result.json", pg_bin=root / "pg", workers=2)
    if condition == "success":
        assert GATE.run_parallel(args) == 0
    else:
        with pytest.raises((ValueError, OSError)):
            GATE.run_parallel(args)
    assert sorted(completed) == [0, 1]
    directory = root / "result-parts"
    assert all((directory / f"worker-{index}.log").is_file() for index in range(2))
    summary = json.loads(args.output.read_text())
    assert summary["coverage_passed"] == (condition == "success")
    if condition == "success":
        assert summary["collected"] == len(NODEIDS)


@pytest.mark.parametrize("exists", [False, True])
def test_shared_bucket_is_prepared_once_without_concurrent_fixture_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exists: bool,
) -> None:
    setup_root(tmp_path, monkeypatch)
    GATE.DRIVER.parent.mkdir(parents=True)
    GATE.DRIVER.write_text("def require_object_storage(env):\n    pass\n")
    for name, value in {
        "ENDPOINT": "http://synthetic.invalid:59000",
        "ACCESS_KEY": "synthetic-access",
        "SECRET_KEY": "synthetic-secret",
    }.items():
        monkeypatch.setenv(f"SPEECHNEXUS_TEST_MINIO_{name}", value)
    calls: list[str] = []

    class Client:
        def __init__(self, host: str, **kwargs: Any) -> None:
            assert host == "synthetic.invalid:59000" and kwargs["secure"] is False

        def bucket_exists(self, bucket: str) -> bool:
            assert bucket == "speechnexus-workbench-saved"
            calls.append("exists")
            return exists

        def make_bucket(self, bucket: str) -> None:
            assert bucket == "speechnexus-workbench-saved"
            calls.append("create")

    monkeypatch.setattr("minio.Minio", Client)
    GATE.prepare_shared_bucket()
    assert calls == (["exists"] if exists else ["exists", "create"])


@pytest.mark.parametrize("condition", ["pass", "failure", "partial-selection", "external-addopts"])
def test_real_worker_collects_and_executes_only_the_canonical_synthetic_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    directory = root / "scripts/dev-env"
    directory.mkdir(parents=True)
    target = directory / SCRIPT.name
    shutil.copyfile(SCRIPT, target)
    options = ["tests/unit", "-q", "--cov", "--cov-report=term-missing", "--durations=20"]
    GATE.DRIVER.write_text(
        "import sys\ndef build_pytest_command(*, full_coverage=False):\n"
        f"    return [sys.executable, '-m', 'pytest', *{options!r}]\n"
    )
    tests = root / "tests/unit"
    tests.mkdir(parents=True)
    (tests / "test_synthetic.py").write_text(
        "from app.sample import choose\ndef test_choose():\n"
        f"    assert choose(True) == {0 if condition == 'failure' else 1}\n"
    )
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Synthetic CI",
            "-c",
            "user.email=synthetic@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "synthetic",
        ],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    data = root / ".runtime/team-c/pg-synthetic/.coverage"
    data.parent.mkdir(parents=True)
    report = root / "worker.json"
    env = {**os.environ, "COVERAGE_FILE": str(data)}
    env.pop("PYTEST_ADDOPTS", None)
    if condition == "external-addopts":
        env["PYTEST_ADDOPTS"] = "-k missing"
    extra = ["-k", "missing"] if condition == "partial-selection" else []
    result = subprocess.run(
        [
            sys.executable,
            str(target),
            "worker",
            "--index",
            "0",
            "--count",
            "1",
            "--report",
            str(report),
            "--",
            *options,
            "--cov-fail-under=0",
            *extra,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == (condition == "pass"), result.stdout + result.stderr
    if condition in ("pass", "failure"):
        evidence = json.loads(report.read_text())
        assert evidence["exit_code"] == int(condition == "failure")
        assert set(evidence["outcomes"].values()) == {"passed" if condition == "pass" else "failed"}
    else:
        assert not report.exists()
