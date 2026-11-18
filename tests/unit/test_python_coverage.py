"""Small synthetic checks for the parallel gate; no databases or business suite run here."""

from __future__ import annotations

import argparse
import ast
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


def git_repository(root: Path) -> str:
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
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def collected(root: Path, data: Path, code: str, name: str) -> Path:
    """Real branch data from a real nested coverage run; never hand-written arcs."""
    script = root / f"_probe_{name}.py"
    script.write_text(code)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE", "COVERAGE", "PYTEST_ADDOPTS"))
    }
    env["COVERAGE_FILE"] = str(data)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--rcfile",
            str(root / "pyproject.toml"),
            str(script),
        ],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return data


def job_evidence(root: Path, head: str, *, workers: int = 2) -> tuple[Path, Path]:
    (root / "app/integration.py").write_text("def handle(value):\n    return value + 1\n")
    parts = root / "job-parts"
    parts.mkdir(parents=True)
    entries = []
    for index in range(workers):
        flag = "True" if index == 0 else "False"
        copy = collected(
            root,
            parts / f"coverage-{index}.data",
            f"from app.sample import choose\nassert choose({flag})\n",
            f"job{index}",
        )
        entries.append(
            {"index": index, "file": copy.name, "digest": GATE.require_branch_data(copy)}
        )
    summary = {
        "kind": "parallel-full-python-coverage",
        "source_head": head,
        "config_digest": GATE.digest(root / "pyproject.toml"),
        "workers": workers,
        "partitions": workers,
        "index_base": 0,
        "tests_passed": True,
        "stopped": True,
        "collected": len(NODEIDS),
        "passed": len(NODEIDS),
        "coverage_parts": entries,
        "coverage_percent": 75.0,
    }
    path = root / "workbench-pg-coverage.json"
    path.write_text(json.dumps(summary))
    return path, parts


def shard_evidence(root: Path, directory: Path, head: str, *, count: int = 2) -> None:
    directory.mkdir(parents=True)
    for index in range(count):
        path = collected(
            root,
            directory / f"coverage-{index}.data",
            "from app.integration import handle\nassert handle(1) == 2\n",
            f"shard{index}",
        )
        report = {
            "version": 2,
            "source_head": head,
            "shard_index": index,
            "shard_count": count,
            "collection_digest": "sha256:" + "0" * 64,
            "collection": [f"tests/integration/test_demo.py::test_{item}" for item in range(4)],
            "selected": [f"tests/integration/test_demo.py::test_{index}"],
            "outcomes": {f"tests/integration/test_demo.py::test_{index}": "passed"},
            "exit_code": 0,
            "executed": True,
            "coverage_file": path.name,
            "coverage_digest": GATE.require_branch_data(path),
            "elapsed_seconds": 1.0,
        }
        (directory / f"integration-shard-{index}.json").write_text(json.dumps(report))


def combine_args(
    root: Path, head: str, *, count: int = 2, probe_rcfile: Path | None = None
) -> argparse.Namespace:
    summary, parts = job_evidence(root, head)
    integration = root / "integration"
    shard_evidence(root, integration, head, count=count)
    return argparse.Namespace(
        job_summary=[summary],
        job_parts=[parts],
        integration_dir=integration,
        count=count,
        output=root / "merged",
        probe_rcfile=probe_rcfile,
    )


@pytest.mark.parametrize(
    "failure",
    [
        "missing-shard",
        "missing-shard-data",
        "changed-shard-data",
        "stray-shard-data",
        "missing-job-part",
        "changed-job-part",
        "wrong-head",
        "wrong-config",
        "incomplete-job",
        "wrong-shard-version",
        "duplicate-summary-field",
    ],
)
def test_merge_requires_every_dataset_before_it_judges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    head = git_repository(root)
    args = combine_args(root, head)
    report = args.integration_dir / "integration-shard-1.json"
    summary = args.job_summary[0]
    if failure == "missing-shard":
        report.unlink()
    elif failure == "missing-shard-data":
        (args.integration_dir / "coverage-1.data").unlink()
    elif failure == "changed-shard-data":
        (args.integration_dir / "coverage-1.data").write_bytes(b"replaced")
    elif failure == "stray-shard-data":
        (args.integration_dir / "coverage-7.data").write_bytes(b"stray")
    elif failure == "missing-job-part":
        (args.job_parts[0] / "coverage-0.data").unlink()
    elif failure == "changed-job-part":
        (args.job_parts[0] / "coverage-0.data").write_bytes(b"replaced")
    elif failure == "wrong-head":
        data = json.loads(summary.read_text())
        data["source_head"] = "b" * 40
        summary.write_text(json.dumps(data))
    elif failure == "wrong-config":
        data = json.loads(summary.read_text())
        data["config_digest"] = "0" * 64
        summary.write_text(json.dumps(data))
    elif failure == "incomplete-job":
        data = json.loads(summary.read_text())
        data["tests_passed"] = False
        summary.write_text(json.dumps(data))
    elif failure == "wrong-shard-version":
        data = json.loads(report.read_text())
        data["version"] = 1
        report.write_text(json.dumps(data))
    else:
        text = summary.read_text()
        summary.write_text(text[:-1] + ',"kind":"other"}')
    expected = "shard 1" if failure.startswith(("missing-shard", "wrong-shard")) else "."
    with pytest.raises(ValueError, match=expected):
        GATE.run_combine(args)
    # A rejected dataset cannot leave a combined database behind for a later report.
    assert not (args.output / "coverage.data").exists()
    assert json.loads((args.output / "merged.json").read_text())["coverage_passed"] is False


def test_merge_judges_the_merged_percentage_through_the_project_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    head = git_repository(root)
    args = combine_args(root, head)

    # Without the integration datasets the reachable total is below the gate; the
    # merge is therefore what carries the same 80% threshold over the line.
    measured_only = root / "measured-only"
    measured_only.mkdir()
    GATE.combine_data(
        sorted(args.job_parts[0].glob("coverage-*.data")),
        measured_only,
        config_file=root / "pyproject.toml",
    )
    assert GATE.gate_report(measured_only, rcfile=root / "pyproject.toml") != 0
    captured = capfd.readouterr()
    assert "less than fail-under=80" in captured.out + captured.err

    assert GATE.run_combine(args) == 0
    merged = json.loads((args.output / "merged.json").read_text())
    assert merged["job_parts"] == 2 and merged["shards"] == 2
    assert merged["coverage_percent"] == 100.0 and merged["coverage_passed"] is True


def test_unexecuted_source_fails_on_the_merged_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    head = git_repository(root)
    # The module exists when the lanes measure, so it is part of the denominator
    # and stands at 0%: the merged report must fail, proving it is not a constant pass.
    (root / "app/wide.py").write_text("\n".join(f"VALUE_{index} = {index}" for index in range(10)))
    args = combine_args(root, head)
    assert GATE.run_combine(args) != 0
    assert json.loads((args.output / "merged.json").read_text())["coverage_passed"] is False


@pytest.mark.parametrize("fail_under", ["80", "101"])
def test_probe_rcfile_only_changes_the_local_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_under: str
) -> None:
    root = setup_root(tmp_path, monkeypatch)
    head = git_repository(root)
    probe = root / "probe.toml"
    probe.write_text(
        '[tool.coverage.run]\nbranch = true\nsource = ["app"]\n'
        f"[tool.coverage.report]\nfail_under = {fail_under}\n"
    )
    args = combine_args(root, head, probe_rcfile=probe)
    assert (GATE.run_combine(args) == 0) is (fail_under == "80")


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
    args = argparse.Namespace(
        output=root / "result.json",
        pg_bin=root / "pg",
        workers=2,
        partitions=None,
        index_base=0,
    )
    if condition == "success":
        assert GATE.run_parallel(args) == 0
    else:
        with pytest.raises((ValueError, OSError)):
            GATE.run_parallel(args)
    assert sorted(completed) == [0, 1]
    directory = root / "result-parts"
    assert all((directory / f"worker-{index}.log").is_file() for index in range(2))
    summary = json.loads(args.output.read_text())
    assert summary["tests_passed"] == (condition == "success")
    if condition == "success":
        assert summary["collected"] == len(NODEIDS)
        assert [part["file"] for part in summary["coverage_parts"]] == [
            "coverage-0.data",
            "coverage-1.data",
        ]
    else:
        assert summary["coverage_parts"] == []


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


def test_an_undeclared_skip_fails_the_merged_gate() -> None:
    # #992 的教训: 「没跑」和「通过」不能长得一样。未声明的 skip 一律算失败。
    assert GATE.outcome_verdict("tests/unit/test_x.py::test_y", "skipped") == "failed"


def test_a_declared_skip_is_accepted_and_still_visible() -> None:
    declared = sorted(GATE.EXPECTED_SKIPS)[0]
    assert GATE.outcome_verdict(declared, "skipped") == "skipped"


def test_a_pass_is_a_pass_even_when_the_nodeid_is_declared() -> None:
    declared = sorted(GATE.EXPECTED_SKIPS)[0]
    assert GATE.outcome_verdict(declared, "passed") == "passed"


def test_a_skip_that_is_no_longer_collected_is_reported() -> None:
    collection = set(GATE.EXPECTED_SKIPS) - {sorted(GATE.EXPECTED_SKIPS)[0]}
    missing = GATE.uncollected_expected_skips(collection)
    assert missing == [sorted(GATE.EXPECTED_SKIPS)[0]]


def test_a_complete_collection_reports_nothing_missing() -> None:
    assert GATE.uncollected_expected_skips(GATE.EXPECTED_SKIPS) == []


def test_every_declared_skip_is_a_real_nodeid_in_this_suite() -> None:
    # 声明与套件对不上时, 白名单会静默腐烂: 要么条目过期, 要么那条用例又回到了
    # 条件定义里。这里用一个真实的 collect-only 把两者钉在一起。
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/ci/test_build_sequences_without_minio.py",
        ],
        capture_output=True,
        text=True,
        cwd=SCRIPT.parents[2],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    missing = sorted(GATE.EXPECTED_SKIPS - collected)
    assert missing == [], missing


def test_every_declared_skip_still_carries_its_skip_decorator() -> None:
    # 白名单只有「还是跳过为真」时才成立: 删掉某条 skipUnless 后 nodeid 仍被收集,
    # 白名单就会变成对该方法未来任意 skip 的永久放行。
    tree = ast.parse(
        (SCRIPT.parents[2] / "tests/ci/test_build_sequences_without_minio.py").read_text(
            encoding="utf-8"
        )
    )
    decorated = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any("unittest.skipUnless" in ast.unparse(item) for item in node.decorator_list)
    }
    declared = {nodeid.split("::")[1] for nodeid in GATE.EXPECTED_SKIPS if "::" in nodeid}
    assert declared <= decorated, sorted(declared - decorated)
