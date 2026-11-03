"""Versioned deterministic digests of the installed controller and approved template."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from speechnexus_node_controller.domain.templates import ApprovedRuntimeTemplate


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_normalize(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def template_digest(template: ApprovedRuntimeTemplate) -> str:
    return _digest({"format": "speechnexus-template-v1", "template": _normalize(asdict(template))})


def controller_artifact_digest() -> str:
    """Hash sorted package-relative .py paths and bytes, excluding caches and configuration.

    This v1 inventory identifies installed package code, not the wheel archive or
    third-party dependencies. Checkout and wheel produce the same value for same code.
    """
    root = Path(__file__).resolve().parents[1]
    files: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = [name for name in names if name != "__pycache__"]
        current = Path(directory)
        if any((current / name).is_symlink() for name in names):
            raise ValueError("controller package contains a symlink directory")
        files.extend(current / name for name in filenames if name.endswith(".py"))
    files.sort()
    if not files:
        raise ValueError("controller package has no source files")
    entries = []
    for path in files:
        if (
            path.is_symlink()
            or any(
                parent.is_symlink()
                for parent in path.parents
                if parent != root and root in parent.parents
            )
            or not path.is_file()
        ):
            raise ValueError("controller package source is not a regular file")
        entries.append(
            [path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()]
        )
    return _digest({"format": "speechnexus-controller-python-v1", "files": entries})
