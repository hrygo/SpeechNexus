"""Fetch same-run integration evidence through bounded GitHub CLI processes.

Use only the stdlib so the coordinator need not install project dependencies.
Verify the API's archive digest before reading only the two expected JSON files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ARCHIVE_LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 8 * 1024 * 1024
FETCH_ATTEMPTS = 3


class ArtifactFetchError(ValueError):
    """A bounded API failure containing no response text or endpoint."""

    def __init__(self, category: str, exit_status: int | None, *, shard: int | None = None):
        self.category = category
        self.exit_status = exit_status
        self.shard = shard
        super().__init__(category)


def fetch_api(endpoint: str, target: Path, limit: int) -> None:
    for attempt in range(FETCH_ATTEMPTS):
        # A failed process may have written a partial response; never reuse it.
        with tempfile.NamedTemporaryFile(mode="w+b", dir=target.parent) as stream:
            try:
                result = subprocess.run(
                    ["gh", "api", "--hostname", "github.com", endpoint],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                if attempt + 1 == FETCH_ATTEMPTS:
                    raise ArtifactFetchError("timeout", None) from None
                continue
            if result.returncode != 0:
                if attempt + 1 == FETCH_ATTEMPTS:
                    raise ArtifactFetchError("process_failure", result.returncode) from None
                continue
            stream.flush()
            size = stream.tell()
            if not 0 < size <= limit:
                raise ValueError("Artifact API returned an invalid size")
            stream.seek(0)
            with target.open("xb") as destination:
                shutil.copyfileobj(stream, destination)
            return


def download(repository: str, run_id: int, count: int, output: Path) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if type(run_id) is not int or run_id < 1 or type(count) is not int or not 1 <= count <= 16:
        raise ValueError("Invalid run or partition count")
    if output.exists() or output.is_symlink():
        raise ValueError("Evidence output must be new")
    with tempfile.TemporaryDirectory(prefix="integration-download-", dir=output.parent) as temp:
        root = Path(temp)
        metadata = root / "metadata.json"
        fetch_api(
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
            metadata,
            1024 * 1024,
        )
        data = json.loads(metadata.read_text())
        artifacts = data["artifacts"]
        if type(data["total_count"]) is not int or data["total_count"] != len(artifacts):
            raise ValueError("Artifact inventory is incomplete")
        selected = [a for a in artifacts if a["name"].startswith("integration-shard-")]
        expected = {f"integration-shard-{i}" for i in range(count)}
        if len(selected) != count or {a["name"] for a in selected} != expected:
            raise ValueError("Integration artifacts are missing, duplicate or unexpected")
        reports = root / "reports"
        reports.mkdir()

        def fetch_one(artifact: dict[str, Any]) -> None:
            artifact_id = artifact["id"]
            size = artifact["size_in_bytes"]
            digest = artifact["digest"]
            if (
                type(artifact_id) is not int
                or artifact_id < 1
                or type(size) is not int
                or not 0 < size <= ARCHIVE_LIMIT
                or artifact["expired"] is not False
                or artifact["workflow_run"]["id"] != run_id
                or not isinstance(digest, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            ):
                raise ValueError("Invalid or expired integration artifact metadata")
            index = int(artifact["name"].removeprefix("integration-shard-"))
            archive = root / f"{index}.zip"
            try:
                fetch_api(f"repos/{repository}/actions/artifacts/{artifact_id}/zip", archive, size)
            except ArtifactFetchError as error:
                raise ArtifactFetchError(error.category, error.exit_status, shard=index) from None
            if archive.stat().st_size != size:
                raise ValueError("Downloaded archive size does not match API metadata")
            with archive.open("rb") as stream:
                actual_digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_digest != digest:
                raise ValueError("Integration artifact digest mismatch")
            names = {f"integration-shard-{index}.json", f"workbench-pg-shard-{index}.json"}
            with zipfile.ZipFile(archive) as zipped:
                members = zipped.infolist()
                if len(members) != 2 or {m.filename for m in members} != names:
                    raise ValueError("Unexpected integration archive members")
                for member in members:
                    if not 0 < member.file_size <= REPORT_LIMIT:
                        raise ValueError("Integration archive report is oversized or empty")
                    # Never extract arbitrary paths, directories or symlinks from the ZIP.
                    (reports / member.filename).write_bytes(zipped.read(member))
            print(f"Verified integration artifact {index}: {size} bytes", flush=True)

        with ThreadPoolExecutor(max_workers=min(3, count)) as workers:
            list(workers.map(fetch_one, selected))
        output.mkdir()
        for report in reports.iterdir():
            report.rename(output / report.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        download(args.repository, args.run_id, args.count, args.output)
    except ArtifactFetchError as error:
        shard = "metadata" if error.shard is None else str(error.shard)
        status = "timeout" if error.exit_status is None else str(error.exit_status)
        print(
            f"::error::Integration artifact retrieval failed "
            f"(category={error.category}, shard={shard}, exit={status})"
        )
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as error:
        print(f"::error::Integration artifact retrieval failed ({type(error).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
