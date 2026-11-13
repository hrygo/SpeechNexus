"""Guard the reference deployment's profile publication authority against drift.

A writer-enabled API used to start without any publication authority and answer
``503 profile_result_unknown`` only when an operator published. The reference
deployment now states the deployment-owned reviewed evidence document as an
explicit prerequisite, and the API refuses to start without a readable one.

This module parses text only, like the other ``tests/ci`` guards. It runs under
the system interpreter and never imports the backend, renders Compose, connects
to PostgreSQL or starts a container. Removing one prerequisite step turns it red:
the Compose forward, the env example key, the runtime wiring, the doc's role and
grant step, the evidence-authoring step, the path/restart step, or the publish
verification step.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
DEPLOY_README = ROOT / "deploy/README.md"
MINIMAL_DOC = ROOT / "docs/operations/compose-minimal.md"
PROFILE_DRAFTS_DOC = ROOT / "docs/components/profile-drafts.md"
CONTRACT_DOC = ROOT / "docs/components/profile-lifecycle-contract.md"
RUNTIME = ROOT / "backend/src/speechnexus_backend/profile_runtime.py"
APP = ROOT / "backend/src/speechnexus_backend/app.py"
SETTINGS = ROOT / "backend/src/speechnexus_backend/settings.py"
EVIDENCE = (
    ROOT / "backend/src/speechnexus_backend/speech/infrastructure/profile_publication_evidence.py"
)

VARIABLE = "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE"
ANCHOR = "#预设发布资格输入与-sn_profile_writer-角色"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def service_block(document: str, name: str) -> str:
    """Return one Compose service block without its siblings."""
    header = re.search(rf"(?m)^  {re.escape(name)}:\n", document)
    if header is None:
        raise AssertionError(f"deploy/compose.yaml has no {name} service")
    rest = document[header.end() :]
    sibling = re.search(r"(?m)^  [a-z][a-z0-9-]*:\n", rest)
    return rest[: sibling.start()] if sibling is not None else rest


class ProfilePublicationAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = read(COMPOSE)
        self.api = service_block(self.compose, "api")

    def test_the_base_compose_forwards_the_evidence_path_without_a_required_guard(self) -> None:
        # A `:?` guard would fail `docker compose config` and `up` for every
        # deployment that never enables the profile writer.
        forward = f"{VARIABLE}: ${{{VARIABLE}:-}}"
        self.assertTrue(
            forward in self.api,
            "deploy/compose.yaml must forward the reviewed evidence path with an empty default",
        )
        self.assertTrue(
            f"${{{VARIABLE}:?" not in self.api,
            "the evidence path must not use a required-variable guard",
        )

    def test_the_env_example_names_the_evidence_key(self) -> None:
        example = read(ENV_EXAMPLE)
        self.assertIn(f"{VARIABLE}=", example)
        self.assertIn(VARIABLE, read(DEPLOY_README))

    def test_the_runtime_has_no_fail_closed_fallback_verifier(self) -> None:
        runtime = read(RUNTIME)
        for marker, source in (
            ("EVIDENCE_FILE_VARIABLE", "runtime"),
            ("load_reviewed_publications", "runtime"),
            ("verifier=self._verifier", "runtime"),
            ("profile_publication_evidence_file", "settings"),
            ("profile_publication_evidence_file is not None", "app"),
            (VARIABLE, "evidence module"),
        ):
            document = {
                "runtime": runtime,
                "settings": read(SETTINGS),
                "app": read(APP),
                "evidence module": read(EVIDENCE),
            }[source]
            self.assertTrue(marker in document, f"{source} lost {marker}")
        # The previous behaviour — start anyway, answer 503 later — must not be
        # reachable again through an unavailable-verifier fallback.
        self.assertTrue(
            "UnavailableProfilePublicationVerifier" not in runtime,
            "profile_runtime must not fall back to the unavailable verifier",
        )

    def test_the_document_keeps_every_prerequisite_step(self) -> None:
        document = read(MINIMAL_DOC)
        self.assertIn("### 预设发布资格输入与 sn_profile_writer 角色", document)
        self.assertIn(ANCHOR, document, "the capability table must link the new section")
        markers = {
            "role password step": "SPEECHNEXUS_PROFILE_WRITER_ROLE_PASSWORD",
            "writer DSN write-back": "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL=",
            "draft grants": "profile_draft_grants(schema, role)",
            "publication grants": "profile_publication_grants(schema, role)",
            "evidence format": '"format": "speechnexus.profile-publication-evidence/1"',
            "definition digest": '"definition_digest": document_digest(',
            "validation digest": '"validation_digest": document_digest(',
            "private evidence path": (
                f"{VARIABLE}=/run/speechnexus-private/profile-publication-evidence.json"
            ),
            "api restart": "up -d --build api",
            "publish verification": "/publications",
            "named startup refusal": f"ValueError: {VARIABLE} is required",
            "unreviewed revision refusal": "422 profile_not_eligible",
            "read at startup": "启动时读取一次",
            "no reviewed revision": '"entries": []',
        }
        missing = [label for label, marker in markers.items() if marker not in document]
        self.assertEqual(missing, [], f"runbook lost prerequisite steps: {missing}")

    def test_the_component_documents_name_the_same_authority(self) -> None:
        drafts = read(PROFILE_DRAFTS_DOC)
        self.assertIn(VARIABLE, drafts)
        self.assertIn("reviewed evidence", drafts)
        self.assertNotIn("production verifier stays", drafts)
        self.assertIn(VARIABLE, read(CONTRACT_DOC))


if __name__ == "__main__":
    unittest.main()
