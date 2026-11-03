"""Read GitHub and refresh the current local delivery matrix; never mutate remote state."""

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


def load_issues(
    repository: str, snapshot: Path | None, tracked_numbers: list[int] | None = None
) -> list[dict[str, Any]]:
    if snapshot:
        result: Any = json.loads(snapshot.read_text())
    else:
        result = []
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
            result.extend(items)
            if len(items) < 100:
                break
            page += 1
        seen = {issue["number"] for issue in result}
        for number in tracked_numbers or []:
            if number not in seen:
                response = subprocess.run(
                    ["gh", "api", f"repos/{repository}/issues/{number}"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                result.append(json.loads(response.stdout))
    if not isinstance(result, list):
        raise ValueError("GitHub snapshot must contain an issue list or list of pages")
    pages = result if result and isinstance(result[0], list) else [result]
    if any(not isinstance(page, list) for page in pages):
        raise ValueError("Invalid issue pages")
    issues = [issue for page in pages for issue in page]
    if any(not isinstance(issue, dict) for issue in issues):
        raise ValueError("Invalid issue record")
    return [issue for issue in issues if "pull_request" not in issue]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--snapshot", type=Path, help="Read a saved GitHub response without network"
    )
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "delivery_tracker", Path(__file__).with_name("delivery_tracker.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load delivery tracker")
    delivery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(delivery)
    manifest = json.loads(args.manifest.read_text())
    delivery.validate_manifest(manifest)
    try:
        issues = load_issues(
            manifest["repository"],
            args.snapshot,
            [entry["number"] for entry in manifest["entries"]],
        )
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"GitHub read failed: {(error.stderr or '').strip()}") from error
    body = delivery.render(
        manifest,
        issues,
        "离线 GitHub 快照" if args.snapshot else "实时读取 GitHub",
        None
        if args.snapshot
        else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    )
    args.output.write_text(body)
    print(f"Refreshed {len(manifest['entries'])} current issues: {args.output}")


if __name__ == "__main__":
    main()
