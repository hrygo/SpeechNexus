"""Deployment-owned reviewed publication evidence, for real-assembly tests only.

An enabled profile writer requires a readable reviewed evidence document; the
repository ships no default. Tests that compose the real API with a writer must
therefore declare what the deployment reviewed. ``entries=[]`` means "this
deployment reviewed no revision": drafts work, every publication is a stable 422
rather than a fabricated eligibility or the old 503. Never use this helper to
grant a publication in a test that does not explicitly review the bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from speechnexus_backend.speech.infrastructure.profile_publication_evidence import (
    EVIDENCE_FORMAT,
)

EVIDENCE_NAME = "profile-publication-evidence.json"


def publication_evidence(directory: Path, entries: list[dict[str, Any]] | None = None) -> str:
    """Write one reviewed evidence document and return its path."""
    path = directory / EVIDENCE_NAME
    path.write_text(
        json.dumps({"format": EVIDENCE_FORMAT, "entries": entries or []}) + "\n",
        encoding="utf-8",
    )
    return str(path)
