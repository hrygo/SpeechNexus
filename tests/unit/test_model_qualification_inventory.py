"""Keep real-model qualification visible and strict without inventing CI model success."""

import ast
import os
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    "tests/integration/test_workbench_minutes_live.py",
    "tests/integration/test_workbench_real_runtime_minutes_browser.py",
)


def test_all_three_real_model_cases_remain_in_explicit_qualification() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert any(
        value.startswith("qualification:")
        for value in config["tool"]["pytest"]["ini_options"]["markers"]
    )
    cases: list[str] = []
    for name in MODULES:
        source = (ROOT / name).read_text()
        assert "pytest.mark.qualification" in source
        assert "pytest.mark.integration" not in source
        cases.extend(
            f"{name}::{node.name}"
            for node in ast.parse(source).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_")
        )
    assert set(cases) == {
        MODULES[0] + "::test_a_real_local_model_completes_a_queued_minutes_task",
        MODULES[0] + "::test_the_serving_lifespan_dispatch_loop_completes_a_queued_task",
        MODULES[1] + "::test_real_runtime_minutes_page_shows_the_configured_model_output",
    }
    recipe = (
        (ROOT / "Makefile")
        .read_text()
        .split("\nworkbench-llm-qualification:\n", 1)[1]
        .split("\ncoordination-test:", 1)[0]
    )
    assert "pytest -m qualification" in recipe
    assert all(name in recipe for name in MODULES)


def test_qualification_command_refuses_missing_approved_deployment_before_building() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "SPEECHNEXUS_WORKBENCH_TEST_DSN",
            "SPEECHNEXUS_TEST_LLM_ENDPOINT",
            "SPEECHNEXUS_TEST_LLM_MODEL",
        }
    }
    result = subprocess.run(
        ["make", "-s", "workbench-llm-qualification"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "approved real intranet LLM" in result.stderr
    assert "pytest" not in result.stdout and "build" not in result.stdout
