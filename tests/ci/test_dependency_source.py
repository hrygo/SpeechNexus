"""CI installs the exact lock from official PyPI and retries only transient errors."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "uv.lock"
CI = ROOT / ".github/workflows/ci.yml"
RETRY = ROOT / "scripts/dev-env/uv-sync-retry.sh"
WRAPPER = "run: bash scripts/dev-env/uv-sync-retry.sh"


def test_lock_contains_only_official_pypi_sources() -> None:
    text = LOCK.read_text(encoding="utf-8")
    hosts = set(re.findall(r"https://([a-z0-9.-]+)/", text))
    assert hosts == {"files.pythonhosted.org", "pypi.org"}, hosts
    assert set(re.findall(r'registry = "([^"]+)"', text)) == {"https://pypi.org/simple"}
    assert "pypi.tuna" not in text


def test_every_locked_sync_step_uses_the_retry_wrapper() -> None:
    ci = CI.read_text(encoding="utf-8")
    assert "run: uv sync --locked" not in ci
    assert ci.count(WRAPPER) == 4
    assert RETRY.is_file()
    script = RETRY.read_text(encoding="utf-8")
    assert "uv sync --locked --all-packages --all-groups" in script
    assert "--locked" in script


def _run_with_fake_uv(
    tmp_path: Path, output: str, exit_code: int, attempts: str = "3"
) -> tuple[subprocess.CompletedProcess[str], int]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls"
    outfile = tmp_path / "output.txt"
    outfile.write_text(output, encoding="utf-8")
    fake = bin_dir / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'printf "x" >> "$FAKE_UV_COUNTER"\n'
        'cat "$FAKE_UV_OUTPUT"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_SYNC_ATTEMPTS": attempts,
        "UV_SYNC_RETRY_DELAY": "0",
        "FAKE_UV_COUNTER": str(counter),
        "FAKE_UV_OUTPUT": str(outfile),
    }
    result = subprocess.run(
        ["bash", str(RETRY)], env=env, capture_output=True, text=True, check=False
    )
    calls = counter.read_text().count("x") if counter.exists() else 0
    return result, calls


def test_transient_index_failure_is_retried_with_a_hard_limit(tmp_path: Path) -> None:
    output = (
        "error: Failed to download `click==8.5.0`\n"
        "  cause: HTTP status client error (403 Forbidden) for url (https://example.invalid)\n"
    )
    result, calls = _run_with_fake_uv(tmp_path, output, 2)
    assert result.returncode == 1
    assert calls == 3
    assert "failed after 3 transient network attempts" in result.stdout


def test_non_transient_failure_fails_immediately(tmp_path: Path) -> None:
    output = "error: The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.\n"
    result, calls = _run_with_fake_uv(tmp_path, output, 2)
    assert result.returncode == 1
    assert calls == 1
    assert "non-transient error; not retrying" in result.stdout


def test_successful_sync_is_not_retried(tmp_path: Path) -> None:
    result, calls = _run_with_fake_uv(tmp_path, "Checked 113 packages in 3ms\n", 0)
    assert result.returncode == 0
    assert calls == 1
    assert "Checked 113 packages" in result.stdout
