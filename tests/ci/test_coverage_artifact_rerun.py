"""A rerun must keep the six-shard evidence gate decidable.

Rerunning failed jobs leaves the earlier attempt's coverage artifact as a peer of
the same name inside one run, so the download side selects the attempt by creation
time and then verifies that artifact exactly as strictly as a single one.

Standard library only: this module runs in the dependency-free routing lane.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/download-integration-evidence.py"
SPEC = importlib.util.spec_from_file_location("coverage_artifact_rerun", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)

RUN_ID = 41_000_000
REPOSITORY = "synthetic/repository"
ARTIFACT = str(EVIDENCE.COVERAGE_ARTIFACT)
SUMMARY = str(EVIDENCE.COVERAGE_SUMMARY)
PARTS = str(EVIDENCE.COVERAGE_PARTS_DIR)
STALE_AT = "2026-11-12T23:59:36Z"
CURRENT_AT = "2026-11-13T00:11:40Z"


def coverage_members(workers: int = 2, *, undeclared: bool = False) -> dict[str, bytes]:
    payloads = {SUMMARY: b'{"synthetic": true}'}
    for index in range(workers):
        payloads[f"{PARTS}/coverage-{index}.data"] = b"synthetic branch data"
    if undeclared:
        payloads[f"{PARTS}/undeclared.txt"] = b"unexpected"
    return payloads


def shard_members(index: int) -> dict[str, bytes]:
    return {
        f"integration-shard-{index}.json": b'{"synthetic": true}',
        f"workbench-pg-shard-{index}.json": b"{}",
        f"coverage-{index}.data": b"synthetic branch data",
    }


def archive(payloads: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, content in payloads.items():
            zipped.writestr(name, content)
    return buffer.getvalue()


def entry(
    archive_id: int,
    payload: bytes,
    created_at: str | None = CURRENT_AT,
    *,
    run_id: int = RUN_ID,
    expired: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "id": archive_id,
        "name": ARTIFACT,
        "size_in_bytes": len(payload),
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "expired": expired,
        "workflow_run": {"id": run_id},
    }
    if created_at is not None:
        metadata["created_at"] = created_at
    return metadata


class FakeArtifactApi:
    """Serve the two API shapes from a fixed inventory; no network, no GitHub CLI."""

    def __init__(self, inventory: list[dict[str, Any]], archives: dict[int, bytes]) -> None:
        self.inventory = inventory
        self.archives = archives
        self.downloads: list[int] = []

    def __call__(self, endpoint: str, target: Path, limit: int) -> None:
        if "?per_page=100" in endpoint:
            payload = json.dumps(
                {"total_count": len(self.inventory), "artifacts": self.inventory}
            ).encode()
        else:
            archive_id = int(endpoint.split("/")[-2])
            self.downloads.append(archive_id)
            payload = self.archives[archive_id]
        assert len(payload) <= limit
        target.write_bytes(payload)


class CoverageDownloadCase(unittest.TestCase):
    def publish(self, api: FakeArtifactApi) -> tuple[list[int], dict[str, bytes], str]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / "staging"
            staging.mkdir()
            output = root / "coverage"
            log = io.StringIO()
            with (
                patch.object(EVIDENCE, "fetch_api", api),
                contextlib.redirect_stdout(log),
            ):
                EVIDENCE.download_coverage(REPOSITORY, RUN_ID, output, staging, api.inventory, 2)
                published = {
                    path.relative_to(output).as_posix(): path.read_bytes()
                    for path in sorted(output.rglob("*"))
                    if path.is_file()
                }
        return api.downloads, published, log.getvalue()

    def refuse(self, api: FakeArtifactApi, expected: str) -> list[int]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / "staging"
            staging.mkdir()
            output = root / "coverage"
            with (
                patch.object(EVIDENCE, "fetch_api", api),
                self.assertRaisesRegex(ValueError, expected),
            ):
                EVIDENCE.download_coverage(REPOSITORY, RUN_ID, output, staging, api.inventory, 2)
            self.assertFalse(output.exists())
        return api.downloads


class SingleAttemptTests(CoverageDownloadCase):
    def test_the_normal_path_publishes_only_the_declared_members(self) -> None:
        payload = archive(coverage_members())
        api = FakeArtifactApi([entry(7, payload)], {7: payload})
        downloads, published, _ = self.publish(api)
        self.assertEqual(downloads, [7])
        self.assertEqual(set(published), set(coverage_members()))


class RerunInventoryTests(CoverageDownloadCase):
    def test_a_rerun_peer_is_selected_by_creation_time(self) -> None:
        stale = archive(coverage_members(workers=1))
        current = archive(coverage_members())
        api = FakeArtifactApi(
            [entry(11, stale, STALE_AT), entry(12, current, CURRENT_AT)],
            {11: stale, 12: current},
        )
        downloads, published, log = self.publish(api)
        self.assertEqual(downloads, [12])
        self.assertIn(f"{PARTS}/coverage-1.data", published)
        self.assertIn("inventory=2", log)

    def test_selection_does_not_depend_on_inventory_order(self) -> None:
        stale = archive(coverage_members(workers=1))
        current = archive(coverage_members())
        api = FakeArtifactApi(
            [entry(21, current, CURRENT_AT), entry(22, stale, STALE_AT)],
            {21: current, 22: stale},
        )
        downloads, published, _ = self.publish(api)
        self.assertEqual(downloads, [21])
        self.assertIn(f"{PARTS}/coverage-1.data", published)

    def test_the_selected_attempt_is_verified_and_never_falls_back(self) -> None:
        stale = archive(coverage_members())
        current = archive(coverage_members(workers=1))
        api = FakeArtifactApi(
            [entry(31, stale, STALE_AT), entry(32, current, CURRENT_AT)],
            {31: stale, 32: current},
        )
        downloads = self.refuse(api, "Unexpected or missing Python coverage archive members")
        self.assertEqual(downloads, [32])

    def test_an_expired_newest_attempt_never_falls_back(self) -> None:
        stale = archive(coverage_members())
        current = archive(coverage_members())
        api = FakeArtifactApi(
            [
                entry(41, stale, STALE_AT),
                entry(42, current, CURRENT_AT, expired=True),
            ],
            {41: stale, 42: current},
        )
        downloads = self.refuse(api, "Invalid or expired Python coverage artifact metadata")
        self.assertEqual(downloads, [])

    def test_a_newest_attempt_from_another_run_never_falls_back(self) -> None:
        stale = archive(coverage_members())
        current = archive(coverage_members())
        api = FakeArtifactApi(
            [
                entry(51, stale, STALE_AT),
                entry(52, current, CURRENT_AT, run_id=RUN_ID + 1),
            ],
            {51: stale, 52: current},
        )
        downloads = self.refuse(api, "Invalid or expired Python coverage artifact metadata")
        self.assertEqual(downloads, [])

    def test_an_ambiguous_inventory_is_still_fatal(self) -> None:
        payload = archive(coverage_members())
        api = FakeArtifactApi(
            [entry(61, payload, CURRENT_AT), entry(62, payload, CURRENT_AT)],
            {61: payload, 62: payload},
        )
        downloads = self.refuse(api, "ambiguous")
        self.assertEqual(downloads, [])

    def test_a_peer_without_a_creation_timestamp_is_still_fatal(self) -> None:
        payload = archive(coverage_members())
        api = FakeArtifactApi(
            [entry(71, payload, STALE_AT), entry(72, payload, None)],
            {71: payload, 72: payload},
        )
        downloads = self.refuse(api, "creation timestamp")
        self.assertEqual(downloads, [])


class FailClosedTests(CoverageDownloadCase):
    def test_a_missing_artifact_is_still_fatal(self) -> None:
        downloads = self.refuse(FakeArtifactApi([], {}), "Python coverage artifact is missing")
        self.assertEqual(downloads, [])

    def test_an_undeclared_member_is_still_fatal(self) -> None:
        payload = archive(coverage_members(undeclared=True))
        api = FakeArtifactApi([entry(81, payload)], {81: payload})
        downloads = self.refuse(api, "Unexpected or missing Python coverage archive members")
        self.assertEqual(downloads, [81])

    def test_a_missing_worker_dataset_is_still_fatal(self) -> None:
        payload = archive(coverage_members(workers=1))
        api = FakeArtifactApi([entry(91, payload)], {91: payload})
        self.refuse(api, "Unexpected or missing Python coverage archive members")

    def test_a_digest_mismatch_is_still_fatal(self) -> None:
        payload = archive(coverage_members())
        metadata = entry(101, payload)
        metadata["digest"] = "sha256:" + "0" * 64
        api = FakeArtifactApi([metadata], {101: payload})
        self.refuse(api, "digest mismatch")


class GateEntryPointTests(CoverageDownloadCase):
    def test_the_gate_entry_point_judges_a_rerun_inventory(self) -> None:
        shards = {200 + index: archive(shard_members(index)) for index in range(2)}
        stale = archive(coverage_members(workers=1))
        current = archive(coverage_members())
        inventory: list[dict[str, Any]] = [
            *(
                {
                    "id": 200 + index,
                    "name": f"integration-shard-{index}",
                    "size_in_bytes": len(shards[200 + index]),
                    "digest": "sha256:" + hashlib.sha256(shards[200 + index]).hexdigest(),
                    "expired": False,
                    "created_at": STALE_AT,
                    "workflow_run": {"id": RUN_ID},
                }
                for index in range(2)
            ),
            entry(300, stale, STALE_AT),
            entry(301, current, CURRENT_AT),
        ]
        api = FakeArtifactApi(inventory, {**shards, 300: stale, 301: current})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports, coverage = root / "reports", root / "coverage"
            with patch.object(EVIDENCE, "fetch_api", api):
                EVIDENCE.download(
                    REPOSITORY,
                    RUN_ID,
                    2,
                    reports,
                    coverage_output=coverage,
                    coverage_workers=2,
                )
            self.assertTrue((coverage / SUMMARY).is_file())
            self.assertTrue((coverage / PARTS / "coverage-1.data").is_file())
            self.assertEqual(
                {path.name for path in reports.iterdir()},
                {*shard_members(0), *shard_members(1)},
            )
        self.assertEqual(sorted(api.downloads), [200, 201, 301])


if __name__ == "__main__":
    unittest.main()
