"""An unmigrated schema must name the repair in one line, not a generic refusal."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import rfc8785
from tests.support.portable_pg import connect as portable_pg_connect

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "deploy/private/transcription-recipe.example.json"
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")


def database_url() -> str:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    return url


def write_recipe(directory: Path) -> Path:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    body = {key: value for key, value in document.items() if key != "recipe_digest"}
    document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    path = directory / "transcription-recipe.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def run_check(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real ``python -m`` entry a deployer runs, not an imported ``main``.

    ``--once`` sits above the module guard and worked either way; only the module
    entry proves that ``--check`` resolves after ``if __name__ == "__main__"``.
    """
    inherited = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    inherited.update(environment)
    return subprocess.run(
        [sys.executable, "-m", "speechnexus_backend.asr_settlement_worker", "--check"],
        cwd=ROOT,
        env=inherited,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_asr_check_prints_one_fail_line_with_the_migration(tmp_path: Path) -> None:
    url = database_url()
    schema = "sn_asr_gate_" + uuid.uuid4().hex[:12]
    recipe = write_recipe(tmp_path)
    with portable_pg_connect(url, autocommit=True) as administrator:
        administrator.execute(f'CREATE SCHEMA "{schema}"')
    try:
        result = run_check(
            {
                "SPEECHNEXUS_ENVIRONMENT": "test",
                "SPEECHNEXUS_DATABASE_URL": url,
                "SPEECHNEXUS_DATABASE_SCHEMA": schema,
                "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:1",
                "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": "http://127.0.0.1:1/",
                "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
                "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL": url,
                "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE": str(recipe),
                "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://127.0.0.1:1",
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "gate-credential",
            }
        )
    finally:
        with portable_pg_connect(url, autocommit=True) as administrator:
            administrator.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    text = result.stdout + result.stderr
    assert result.returncode == 1, text[-2000:]
    assert "Traceback" not in text, text[-2000:]
    lines = [line for line in text.splitlines() if line.startswith("FAIL:")]
    assert len(lines) == 1, lines
    assert "ASR settlement preflight" in lines[0]
    assert "migration profile" in lines[0]
    for forbidden in ("postgresql://", schema, "gate-credential"):
        assert forbidden not in text, f"the check echoed {forbidden!r}"
