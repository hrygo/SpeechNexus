"""Check known HTTP structural changes; speech is an explicit additional surface.

This is not a proof of complete protocol compatibility. It adds no compatibility
promise for versioned internal APIs, event ordering, authorization or side effects.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_KERNEL = Path(__file__).with_name("check-openai-contract-compat.py")
SOURCES = (
    "contracts/admin-openapi.yaml",
    "contracts/workbench-openapi.yaml",
    "contracts/internal/runtime-control.openapi.yaml",
    "contracts/internal/execution-control.openapi.yaml",
)
SPEECH_SOURCE = "contracts/speech-openapi.yaml"
UNVERIFIED = (
    "Complete JSON Schema semantic inclusion and standalone JSON contracts",
    "HTTP serialization settings, added statuses/media and security scheme evolution",
    "WS/event ordering, epoch, authorization and business effects",
    "Consumer upgrade, migration and actual deployment qualification",
)


def _kernel() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_http_contract_change_kernel", _KERNEL)
    if spec is None or spec.loader is None:
        raise RuntimeError("HTTP comparison kernel unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    vars(module)["_ROOT"] = _ROOT
    return module


def _version(document: Any) -> str:
    if (
        not isinstance(document, Mapping)
        or document.get("openapi") != "3.1.0"
        or not isinstance(document.get("paths"), Mapping)
        or not isinstance(document.get("info"), Mapping)
        or not isinstance(document["info"].get("version"), str)
        or not document["info"]["version"]
    ):
        raise ValueError("Expected a versioned OpenAPI 3.1.0 HTTP document")
    return str(document["info"]["version"])


def inspect_sources(
    base: str, *, surface: Literal["nonstandard", "speech"] = "nonstandard"
) -> dict[str, Any]:
    if surface not in {"nonstandard", "speech"}:
        raise ValueError("Unknown HTTP comparison surface")
    # Resolve once: every external reference on the baseline side uses the same
    # immutable commit, even if a moving branch changes during the comparison.
    resolved = subprocess.check_output(
        ["git", "-C", str(_ROOT), "rev-parse", "--verify", "--end-of-options", base + "^{commit}"],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()
    baseline_paths = set(
        subprocess.check_output(
            ["git", "-C", str(_ROOT), "ls-tree", "-r", "--name-only", resolved, "--", "contracts"],
            text=True,
        ).splitlines()
    )
    compare = _kernel()._compare_openapi
    rows: list[dict[str, Any]] = []
    findings: list[str] = []
    selected = (SPEECH_SOURCE,) if surface == "speech" else SOURCES
    for path in selected:
        row: dict[str, Any] = {"path": path, "base_version": None, "current_version": None}
        rows.append(row)
        if path not in baseline_paths:
            findings.append(f"{path}: frozen baseline source missing; not compared")
            continue
        if not (_ROOT / path).is_file():
            findings.append(f"{path}: current source removed or unavailable")
            continue
        try:
            old = yaml.safe_load(
                subprocess.check_output(
                    ["git", "-C", str(_ROOT), "show", resolved + ":" + path], text=True
                )
            )
            new = yaml.safe_load((_ROOT / path).read_text(encoding="utf-8"))
            row["base_version"], row["current_version"] = _version(old), _version(new)
            row["paths"] = len(new["paths"])
            detected = list(compare(old, new, path, base_git_ref=resolved, standard_only=False))
            findings.extend(detected)
            row["findings"] = len(detected)
        except ValueError, OSError, yaml.YAMLError, subprocess.CalledProcessError, RecursionError:
            # Broken sources/refs or an unbounded schema cannot turn into a pass.
            findings.append(f"{path}: source or reference comparison failed; not proven")
    return {
        "baseline": resolved,
        "scope": "known-speech-http-structure"
        if surface == "speech"
        else "known-nonstandard-http-structure",
        "sources": rows,
        "findings": findings,
        "unverified": list(UNVERIFIED),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git commit/ref of the frozen baseline")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--surface", choices=("nonstandard", "speech"), default="nonstandard")
    args = parser.parse_args()
    try:
        report = inspect_sources(args.base, surface=args.surface)
    except OSError, subprocess.CalledProcessError, RuntimeError:
        print("HTTP structural comparison failed: baseline or kernel unavailable", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, sort_keys=True))
    else:
        if args.surface == "speech":
            print("HTTP comparison surface: speech (all paths, full fields)")
        print("HTTP structural baseline: " + report["baseline"])
        for row in report["sources"]:
            print(f"{row['path']}: {row['base_version']} -> {row['current_version']}")
        for finding in report["findings"]:
            print("  - " + finding)
        print("Known HTTP structural checks: " + ("rejected" if report["findings"] else "passed"))
        print("Not a full protocol compatibility proof; see unverified scope:")
        for boundary in report["unverified"]:
            print("  - " + boundary)
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
