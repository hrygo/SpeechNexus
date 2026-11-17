"""Start the API entry point against an empty schema and read the line it prints (#928).

`async-job-runbook.md` §8 promises that an unmigrated database makes the api container
print the settled-capacity refusal as one `FAIL:` line and exit `1` instead of
restart-looping on a traceback. The unit case that guarded this called the entry
function with `uvicorn.run` patched out, so it could not see either the traceback or
the exit code (#926 found the same shape on the Job schema gate).

This module starts the real `speechnexus-api` entry point as a subprocess against a
freshly created, deliberately empty PostgreSQL schema, with a valid private speech
recipe so the synchronous preflight is the first gate that refuses. It then asserts on
the bytes an operator would read: one `FAIL:` line naming the migration profile, no
`Traceback`, exit code `1`, and no deployment input (DSN, password, host, database) in
either stream.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
import rfc8785

ROOT = Path(__file__).resolve().parents[2]
RECIPE_EXAMPLE = ROOT / "deploy/private/speech-recipe.example.json"
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
# The entry point is `speechnexus_backend.main:main`; the console script only wraps
# this call, and `-c` keeps the case independent of the installed script names.
API_ENTRY = "from speechnexus_backend.main import main; main()"


def database_url() -> str:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    return url


def write_recipe(directory: Path) -> Path:
    """Copy the documented example and re-sign it, so the loader accepts it."""
    document = json.loads(RECIPE_EXAMPLE.read_text(encoding="utf-8"))
    body = {key: value for key, value in document.items() if key != "recipe_digest"}
    document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    path = directory / "speech-recipe.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def run_api(
    environment: dict[str, str], *, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    inherited = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    inherited.update(environment)
    return subprocess.run(
        [sys.executable, "-c", API_ENTRY],
        cwd=ROOT,
        env=inherited,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_api_settlement_gate_prints_one_fail_line_and_no_traceback(tmp_path: Path) -> None:
    """The synchronous preflight refuses in one line, before uvicorn can start serving."""
    url = database_url()
    schema = "sn_gate_output_" + uuid.uuid4().hex[:12]
    recipe = write_recipe(tmp_path)
    with psycopg.connect(url, autocommit=True) as administrator:
        administrator.execute(f'CREATE SCHEMA "{schema}"')
    try:
        result = run_api(
            {
                "SPEECHNEXUS_ENVIRONMENT": "test",
                "SPEECHNEXUS_PROCESS_ROLE": "api",
                "SPEECHNEXUS_HOST": "127.0.0.1",
                # The refusal happens in synchronous assembly, so no socket is ever
                # bound; a fixed high port keeps the case from racing a real server.
                "SPEECHNEXUS_PORT": "18099",
                "SPEECHNEXUS_DATABASE_URL": url,
                "SPEECHNEXUS_DATABASE_SCHEMA": schema,
                "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:1",
                "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": "http://127.0.0.1:1/",
                "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
                "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": "gate-output-access",
                "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": "gate-output-secret",
                "SPEECHNEXUS_SPEECH_RECIPE_FILE": str(recipe),
                "SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE": "en",
                "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL": url,
                "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://127.0.0.1:1",
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "gate-output-credential",
            }
        )
    finally:
        with psycopg.connect(url, autocommit=True) as administrator:
            administrator.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    text = result.stdout + result.stderr
    assert result.returncode == 1, text[-2000:]
    assert "Traceback" not in text, text[-2000:]
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("FAIL:")]
    assert len(lines) == 1, lines
    assert "the settled-capacity preflight cannot read the admission tables" in lines[0]
    assert "migration profile" in lines[0]
    # The table name is the documented repair surface; the deployment input is not.
    for forbidden in ("postgresql://", schema, "gate-output-secret"):
        assert forbidden not in text, f"the api echoed {forbidden!r}"
    # The refusal is the first gate: uvicorn never reports a started server process.
    assert "Started server process" not in text
