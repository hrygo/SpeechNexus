"""Tiny artifact fixtures; no CI jobs, databases or remote services are executed."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev-env/download-integration-evidence.py"
SPEC = importlib.util.spec_from_file_location("integration_evidence_download", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def archive(index: int, *, unexpected: bool = False) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        zipped.writestr(f"integration-shard-{index}.json", '{"synthetic": true}')
        zipped.writestr(
            "../outside.json" if unexpected else f"workbench-pg-shard-{index}.json", "{}"
        )
    return data.getvalue()


def setup_api(
    monkeypatch: pytest.MonkeyPatch, *, unexpected: bool = False
) -> tuple[dict[str, Any], list[str]]:
    archives = {i + 1: archive(i, unexpected=unexpected) for i in range(2)}
    data: dict[str, Any] = {
        "total_count": 2,
        "artifacts": [
            {
                "id": i + 1,
                "name": f"integration-shard-{i}",
                "size_in_bytes": len(archives[i + 1]),
                "digest": "sha256:" + hashlib.sha256(archives[i + 1]).hexdigest(),
                "expired": False,
                "workflow_run": {"id": 123},
            }
            for i in range(2)
        ],
    }
    calls: list[str] = []

    def fetch(endpoint: str, target: Path, limit: int) -> None:
        calls.append(endpoint)
        payload = (
            json.dumps(data).encode()
            if "?per_page=100" in endpoint
            else archives[int(endpoint.split("/")[-2])]
        )
        assert len(payload) <= limit
        target.write_bytes(payload)

    monkeypatch.setattr(MODULE, "fetch_api", fetch)
    return data, calls


def test_download_checks_real_zip_hash_and_only_publishes_expected_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls = setup_api(monkeypatch)
    output = tmp_path / "reports"
    MODULE.download("owner/repo", 123, 2, output)
    assert {p.name for p in output.iterdir()} == {
        f"{prefix}-shard-{i}.json" for prefix in ("integration", "workbench-pg") for i in range(2)
    }
    assert calls[0] == "repos/owner/repo/actions/runs/123/artifacts?per_page=100"
    assert set(calls[1:]) == {f"repos/owner/repo/actions/artifacts/{i}/zip" for i in (1, 2)}
    with pytest.raises(ValueError, match="must be new"):
        MODULE.download("owner/repo", 123, 2, output)


@pytest.mark.parametrize(
    "failure", ["hash", "missing", "duplicate", "wrong-run", "expired", "page", "size", "large"]
)
def test_download_rejects_invalid_evidence_before_publishing(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, _ = setup_api(monkeypatch)
    first = data["artifacts"][0]
    if failure == "hash":
        first["digest"] = "sha256:" + "0" * 64
    elif failure == "missing":
        data["artifacts"].pop()
        data["total_count"] = 1
    elif failure == "duplicate":
        data["artifacts"][1]["name"] = first["name"]
    elif failure == "wrong-run":
        first["workflow_run"]["id"] = 456
    elif failure == "expired":
        first["expired"] = True
    elif failure == "size":
        first["size_in_bytes"] += 1
    elif failure == "large":
        first["size_in_bytes"] = MODULE.ARCHIVE_LIMIT + 1
    else:
        data["total_count"] = 101
    output = tmp_path / "reports"
    with pytest.raises(ValueError):
        MODULE.download("owner/repo", 123, 2, output)
    assert not output.exists()
    assert not list(tmp_path.glob("integration-download-*"))


def test_zip_paths_cannot_escape_the_private_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_api(monkeypatch, unexpected=True)
    with pytest.raises(ValueError, match="archive members"):
        MODULE.download("owner/repo", 123, 2, tmp_path / "reports")
    assert not (tmp_path / "outside.json").exists()
    assert not (tmp_path / "reports").exists()


def test_report_size_limit_does_not_require_a_large_test_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_api(monkeypatch)
    monkeypatch.setattr(MODULE, "REPORT_LIMIT", 1)
    with pytest.raises(ValueError, match="oversized"):
        MODULE.download("owner/repo", 123, 2, tmp_path / "reports")
    assert not (tmp_path / "reports").exists()


def test_cli_process_is_bounded_and_never_receives_a_token_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command == ["gh", "api", "--hostname", "github.com", "repos/owner/repo/example"]
        assert kwargs["timeout"] == 15
        assert kwargs["stderr"] == subprocess.PIPE
        raise subprocess.TimeoutExpired(command, 15)

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        MODULE.fetch_api("repos/owner/repo/example", tmp_path / "response", 100)
