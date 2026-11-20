"""Hold the builtin-voice publication path to the only evidence shape it accepts.

`speechnexus-publish-builtin-voice` verifies a binding's release evidence, which
needs the digest of the capacity manifest and of the qualification report
(`binding_release_evidence.py`). The HTTP lifecycle cannot supply them: its request
carries two evidence IDs and nothing else, the server has no raw bytes to digest,
and the evidence row it writes is insert-only, so a `NULL` digest can never be
repaired afterwards (#1075). The operator-facing closure is therefore the host CLI,
and this guard keeps the documentation and the contract telling the same story:

- the minimum-deployment manual publishes a builtin voice's binding with
  `speechnexus-publish-binding --capacity-manifest-digest ...
  --qualification-report-digest ...`, and says why the HTTP path cannot;
- `ModelBindingPublicationRequest` still has no digest field -- adding one is a
  contract change that must revisit the manual, not a quiet divergence;
- the HTTP publisher still writes evidence without digests, and the release check
  still refuses a `NULL` digest. Either edit means the "two independent paths"
  doc statement is stale.

Text-only guard, like the rest of `tests/ci`: it runs under the system interpreter
and never imports the backend or renders Compose.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "docs/operations/compose-minimal.md"
RUNBOOK = ROOT / "docs/operations/async-job-runbook.md"
CONTRACT = ROOT / "contracts/admin-openapi.yaml"
LIFECYCLE = (
    ROOT / "backend/src/speechnexus_backend/speech/infrastructure/binding_lifecycle_postgres.py"
)
RELEASE = ROOT / "backend/src/speechnexus_backend/speech/application/binding_release_evidence.py"
REQUEST_SCHEMA = "ModelBindingPublicationRequest"
CLI_FLAGS = ("--capacity-manifest-digest", "--qualification-report-digest")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def schema_block(name: str) -> str:
    """One component schema, bounded by the next two-space component name."""
    body = text(CONTRACT)
    marker = f"\n    {name}:\n"
    start = body.index(marker)
    rest = body[start + len(marker) :]
    following = re.search(r"^    [A-Za-z][A-Za-z0-9]*:\n", rest, re.MULTILINE)
    end = start + len(marker) + (following.start() if following else 0)
    return body[start:end]


class PublicationContractTest(unittest.TestCase):
    """The HTTP request cannot carry what the release check reads."""

    def test_the_http_request_carries_evidence_ids_only(self) -> None:
        block = schema_block(REQUEST_SCHEMA)
        for field in ("capacity_manifest_id", "qualification_report_id", "expected_revision"):
            self.assertIn(field, block)
        self.assertNotIn("digest", block.lower())

    def test_the_http_publisher_records_no_digest(self) -> None:
        source = text(LIFECYCLE)
        start = source.index('evidence_source="publish_http"') - 600
        publish = source[start:]
        publish = publish[: publish.index(")") + 1]
        self.assertNotIn("digest", publish.lower())

    def test_the_release_check_still_refuses_a_missing_digest(self) -> None:
        source = text(RELEASE)
        self.assertIn("capacity_manifest_digest is None", source)
        self.assertIn("qualification_report_digest is None", source)


class ManualClosureTest(unittest.TestCase):
    """The manual sends operators to the host CLI before publishing a voice."""

    def test_the_manual_names_the_host_command_before_the_voice_publish(self) -> None:
        manual = text(MANUAL)
        anchor = manual.index("speechnexus-publish-builtin-voice")
        section = manual[max(0, anchor - 400) : anchor + 4000]
        self.assertIn("speechnexus-publish-binding", section)
        for flag in CLI_FLAGS:
            self.assertIn(flag, section)
        self.assertIn("NULL", section)
        self.assertIn("insert-only", section)

    def test_the_manual_and_runbook_name_the_host_command(self) -> None:
        self.assertIn("Invalid binding release evidence", text(MANUAL))
        for path in (MANUAL, RUNBOOK):
            with self.subTest(document=path.name):
                document = text(path)
                self.assertIn("speechnexus-publish-binding", document)
                for flag in CLI_FLAGS:
                    self.assertIn(flag, document)


if __name__ == "__main__":
    unittest.main()
