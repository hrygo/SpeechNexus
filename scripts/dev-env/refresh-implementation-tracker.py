"""Read GitHub or a snapshot and refresh the delivery matrix; never mutate remote state."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "docs/plans/delivery-issues.json"
DEFAULT_OUTPUT = ROOT / "docs/plans/delivery-tracking.md"
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_FIELDS = (
    "number",
    "html_url",
    "repository_url",
    "title",
    "state",
    "state_reason",
    "labels",
    "parent_issue_url",
)
LIVE_MODE = "实时读取 GitHub"
SNAPSHOT_MODE = "GitHub 快照"


def load_issues(
    repository: str, snapshot: Path | None, tracked_numbers: list[int] | None = None
) -> list[dict[str, Any]]:
    if snapshot:
        payload: Any = json.loads(snapshot.read_text(encoding="utf-8"))
    else:
        payload = []
        page = 1
        while True:
            response = subprocess.run(
                ["gh", "api", f"repos/{repository}/issues?state=open&per_page=100&page={page}"],
                check=True,
                capture_output=True,
                text=True,
            )
            items = json.loads(response.stdout)
            if not isinstance(items, list):
                raise ValueError("Expected a GitHub issue page")
            payload.extend(items)
            if len(items) < 100:
                break
            page += 1
        seen = {issue["number"] for issue in payload}
        for number in tracked_numbers or []:
            if number not in seen:
                response = subprocess.run(
                    ["gh", "api", f"repos/{repository}/issues/{number}"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                payload.append(json.loads(response.stdout))
    return _issues(payload)


def _issues(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("GitHub snapshot must contain an issue list or list of pages")
    pages = payload if payload and isinstance(payload[0], list) else [payload]
    if any(not isinstance(page, list) for page in pages):
        raise ValueError("Invalid issue pages")
    issues = [issue for page in pages for issue in page]
    if any(not isinstance(issue, dict) for issue in issues):
        raise ValueError("Invalid issue record")
    return [issue for issue in issues if "pull_request" not in issue]


def parse_time(value: str, label: str) -> str:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO 8601 timestamp: {value}") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset: {value}")
    return value


def current_time() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def read_snapshot(
    path: Path, snapshot_at: str | None, repository: str
) -> tuple[list[dict[str, Any]], str]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if set(payload) != {"schema_version", "repository", "fetched_at", "issues"}:
            raise ValueError("Invalid delivery snapshot fields")
        if payload["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("Unsupported delivery snapshot schema")
        if payload["repository"] != repository:
            raise ValueError(
                f"Snapshot repository {payload['repository']} does not match {repository}"
            )
        recorded_at = payload["fetched_at"]
        if not isinstance(recorded_at, str):
            raise ValueError("Snapshot requires a fetch time")
        parse_time(recorded_at, "Snapshot fetched_at")
        issues = _issues(payload["issues"])
        if snapshot_at:
            return issues, parse_time(snapshot_at, "--snapshot-at")
        return issues, recorded_at
    if snapshot_at:
        return _issues(payload), parse_time(snapshot_at, "--snapshot-at")
    raise ValueError("A raw GitHub snapshot has no fetch time; pass --snapshot-at")


def write_snapshot(
    path: Path, repository: str, issues: list[dict[str, Any]], fetched_at: str
) -> None:
    records: list[dict[str, Any]] = []
    for issue in sorted(issues, key=lambda item: item["number"]):
        labels = issue.get("labels") or []
        if any(not isinstance(label, dict) or "name" not in label for label in labels):
            raise ValueError("Invalid issue label")
        record = {field: issue.get(field) for field in SNAPSHOT_FIELDS}
        record["labels"] = [{"name": label["name"]} for label in labels]
        records.append(record)
    document = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "repository": repository,
        "fetched_at": fetched_at,
        "issues": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _tracked_numbers(manifest: dict[str, Any]) -> list[int]:
    return [
        manifest["program"],
        *[epic["number"] for epic in manifest["epics"]],
        *[number for requirement in manifest["requirements"] for number in requirement["issues"]],
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--snapshot", type=Path, help="Render from a saved GitHub snapshot without network"
    )
    parser.add_argument(
        "--refresh-snapshot",
        type=Path,
        help="Fetch GitHub, write the committed snapshot, and render from it",
    )
    parser.add_argument(
        "--snapshot-at",
        help="Fixed ISO 8601 snapshot time; makes the rendered output byte-reproducible",
    )
    args = parser.parse_args()
    if args.snapshot and args.refresh_snapshot:
        parser.error("--snapshot and --refresh-snapshot are mutually exclusive")
    spec = importlib.util.spec_from_file_location(
        "delivery_tracker", Path(__file__).with_name("delivery_tracker.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load delivery tracker")
    delivery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(delivery)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    delivery.validate_manifest(manifest)
    prd_text = delivery.validate_documents(manifest, ROOT)
    try:
        if args.snapshot:
            issues, snapshot_at = read_snapshot(
                args.snapshot, args.snapshot_at, manifest["repository"]
            )
            mode = SNAPSHOT_MODE
        else:
            issues = load_issues(manifest["repository"], None, _tracked_numbers(manifest))
            snapshot_at = (
                parse_time(args.snapshot_at, "--snapshot-at")
                if args.snapshot_at
                else current_time()
            )
            mode = SNAPSHOT_MODE if args.refresh_snapshot else LIVE_MODE
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"GitHub read failed: {(error.stderr or '').strip()}") from error
    body = delivery.render(manifest, issues, mode, prd_text, snapshot_at)
    if args.refresh_snapshot:
        write_snapshot(args.refresh_snapshot, manifest["repository"], issues, snapshot_at)
        print(f"Snapshot written: {args.refresh_snapshot}")
    args.output.write_text(body, encoding="utf-8", newline="\n")
    print(f"Refreshed {len(manifest['requirements'])} requirements: {args.output}")


if __name__ == "__main__":
    main()
