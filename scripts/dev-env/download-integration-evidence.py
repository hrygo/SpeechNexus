"""Fetch same-run integration evidence through bounded GitHub CLI processes.

Use only the stdlib so the coordinator need not install project dependencies.
Verify the API's archive digest before reading only the expected report and
coverage-data members, including the measured dataset behind the merged gate.

Rerunning failed jobs keeps the earlier attempt's artifact as a peer of the same
name inside one run, so the measured dataset is selected by creation time and the
chosen artifact is then verified exactly like a single one.
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
from datetime import datetime
from pathlib import Path
from typing import Any

ARCHIVE_LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 8 * 1024 * 1024
COVERAGE_DATA_LIMIT = 32 * 1024 * 1024
COVERAGE_ARTIFACT = "workbench-pg-full-coverage"
COVERAGE_ARTIFACT_LIMIT = 32 * 1024 * 1024
COVERAGE_PARTS_DIR = "workbench-pg-coverage-parts"
COVERAGE_SUMMARY = "workbench-pg-coverage.json"
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


def download_shards(
    repository: str, run_id: int, count: int, output: Path, root: Path, artifacts: list[Any]
) -> None:
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
        names = {
            f"integration-shard-{index}.json",
            f"workbench-pg-shard-{index}.json",
            f"coverage-{index}.data",
        }
        limits = {
            f"integration-shard-{index}.json": REPORT_LIMIT,
            f"workbench-pg-shard-{index}.json": REPORT_LIMIT,
            f"coverage-{index}.data": COVERAGE_DATA_LIMIT,
        }
        with zipfile.ZipFile(archive) as zipped:
            members = zipped.infolist()
            if len(members) != len(names) or {m.filename for m in members} != names:
                raise ValueError("Unexpected integration archive members")
            for member in members:
                if not 0 < member.file_size <= limits[member.filename]:
                    raise ValueError("Integration archive member is oversized or empty")
                # Never extract arbitrary paths, directories or symlinks from the ZIP.
                (reports / member.filename).write_bytes(zipped.read(member))
        print(f"Verified integration artifact {index}: {size} bytes", flush=True)

    with ThreadPoolExecutor(max_workers=min(3, count)) as workers:
        list(workers.map(fetch_one, selected))
    output.mkdir()
    for report in reports.iterdir():
        report.rename(output / report.name)


def artifact_created_at(artifact: Any) -> datetime:
    """A rerun's peer artifact is distinguishable only by its creation time."""
    created_at = artifact.get("created_at") if isinstance(artifact, dict) else None
    if not isinstance(created_at, str):
        raise ValueError("Python coverage artifact has no creation timestamp")
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Python coverage artifact has an unusable creation timestamp") from None
    if stamp.tzinfo is None:
        raise ValueError("Python coverage artifact timestamp is not timezone aware")
    return stamp


def select_coverage_artifact(artifacts: list[Any]) -> tuple[Any, int]:
    """Pick the attempt that produced the evidence when a rerun left a peer behind.

    One job uploads this name once per attempt, so peers can only come from an
    earlier attempt of the same run and the newest creation time is the current
    evidence. Anything else stays fatal: no artifact, an unusable or ambiguous
    inventory, and every later verification failure all refuse the gate.
    """
    matches = [artifact for artifact in artifacts if artifact["name"] == COVERAGE_ARTIFACT]
    if not matches:
        raise ValueError("Python coverage artifact is missing")
    if len(matches) == 1:
        return matches[0], 1
    stamps = [artifact_created_at(artifact) for artifact in matches]
    newest = max(stamps)
    if stamps.count(newest) != 1:
        raise ValueError("Python coverage artifact inventory is ambiguous")
    return matches[stamps.index(newest)], len(matches)


def download_coverage(
    repository: str, run_id: int, output: Path, root: Path, artifacts: list[Any], workers: int
) -> None:
    """Fetch the measured dataset; an absent artifact is a failing gate, not a smaller one."""
    if type(workers) is not int or workers not in (1, 2):
        raise ValueError("Invalid Python coverage worker count")
    artifact, inventory = select_coverage_artifact(artifacts)
    artifact_id = artifact["id"]
    size = artifact["size_in_bytes"]
    digest = artifact["digest"]
    if (
        type(artifact_id) is not int
        or artifact_id < 1
        or type(size) is not int
        or not 0 < size <= COVERAGE_ARTIFACT_LIMIT
        or artifact["expired"] is not False
        or artifact["workflow_run"]["id"] != run_id
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        raise ValueError("Invalid or expired Python coverage artifact metadata")
    archive = root / "coverage.zip"
    try:
        fetch_api(f"repos/{repository}/actions/artifacts/{artifact_id}/zip", archive, size)
    except ArtifactFetchError as error:
        raise ArtifactFetchError(error.category, error.exit_status, shard=None) from None
    if archive.stat().st_size != size:
        raise ValueError("Downloaded coverage archive size does not match API metadata")
    with archive.open("rb") as stream:
        actual_digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    if actual_digest != digest:
        raise ValueError("Python coverage artifact digest mismatch")
    required = {COVERAGE_SUMMARY} | {
        f"{COVERAGE_PARTS_DIR}/coverage-{index}.data" for index in range(workers)
    }
    allowed = {COVERAGE_SUMMARY, f"{COVERAGE_PARTS_DIR}/coverage.data"} | {
        f"{COVERAGE_PARTS_DIR}/coverage-{index}.data" for index in range(workers)
    }
    for index in range(workers):
        for name in (f"worker-{index}.json", f"worker-{index}.log", f"postgres-{index}.json"):
            allowed.add(f"{COVERAGE_PARTS_DIR}/{name}")
    limits = {COVERAGE_SUMMARY: REPORT_LIMIT} | {
        f"{COVERAGE_PARTS_DIR}/coverage-{index}.data": COVERAGE_DATA_LIMIT
        for index in range(workers)
    }
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        names = {member.filename for member in members}
        if not required <= names or names - allowed:
            raise ValueError("Unexpected or missing Python coverage archive members")
        if len(members) > 64:
            raise ValueError("Python coverage archive has too many members")
        published = 0
        for member in members:
            if member.filename not in required:
                continue
            if not 0 < member.file_size <= limits[member.filename]:
                raise ValueError("Python coverage archive member is oversized or empty")
            destination = output / member.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(zipped.read(member))
            published += 1
        if published != len(required):
            raise ValueError("Python coverage archive omitted a required member")
    print(
        f"Verified Python coverage artifact: {size} bytes "
        f"(id={artifact_id}, inventory={inventory})",
        flush=True,
    )


def download(
    repository: str,
    run_id: int,
    count: int,
    output: Path,
    *,
    coverage_output: Path | None = None,
    coverage_workers: int = 2,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if type(run_id) is not int or run_id < 1 or type(count) is not int or not 1 <= count <= 16:
        raise ValueError("Invalid run or partition count")
    if output.exists() or output.is_symlink():
        raise ValueError("Evidence output must be new")
    if coverage_output is not None and (coverage_output.exists() or coverage_output.is_symlink()):
        raise ValueError("Coverage output must be new")
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
        download_shards(repository, run_id, count, output, root, artifacts)
        if coverage_output is not None:
            download_coverage(
                repository, run_id, coverage_output, root, artifacts, coverage_workers
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--coverage-output", type=Path)
    parser.add_argument("--coverage-workers", type=int, default=2)
    args = parser.parse_args()
    try:
        download(
            args.repository,
            args.run_id,
            args.count,
            args.output,
            coverage_output=args.coverage_output,
            coverage_workers=args.coverage_workers,
        )
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
