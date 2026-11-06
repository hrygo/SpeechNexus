"""Regression tests for the local MySQL oracle launcher."""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/mysql-oracle.sh"


def bash(program: str, *args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", program, "bash", str(SCRIPT), *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def test_urlencode_escapes_password_reserved_characters() -> None:
    password = "abc%41@:/#? +ü"

    result = bash('source "$1"; urlencode "$2"', password)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "abc%2541%40%3A%2F%23%3F%20%2B%C3%BC"
    parsed = urlparse(f"mysql://root:{result.stdout}@127.0.0.1:55434/")
    assert unquote(parsed.password or "") == password


def test_repo_relative_test_path_resolves_from_a_subdirectory() -> None:
    result = bash(
        'source "$1"; resolve_test_path "$2"',
        "tests/integration/test_gateway_abort_ledger.py",
        cwd=ROOT / "tests/integration",
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == ROOT / "tests/integration/test_gateway_abort_ledger.py"


def test_missing_module_reports_the_merged_pr() -> None:
    result = bash(
        'source "$1"; resolve_test_path "$2"',
        "tests/integration/test_gateway_abort_ledger_missing.py",
    )

    assert result.returncode == 2
    assert "test module not found" in result.stderr
    assert "PR #435" in result.stderr


def test_invalid_paths_are_not_reported_as_missing_modules() -> None:
    cases = (
        ("../outside.py", "must not traverse outside the repository"),
        ("/tmp/outside.py", "must be inside the repository"),
        ("docs/operations/README.md", "must name a .py test module"),
    )
    for requested, expected in cases:
        result = bash('source "$1"; resolve_test_path "$2"', requested)
        assert result.returncode == 2
        assert expected in result.stderr
        assert "test module not found" not in result.stderr
