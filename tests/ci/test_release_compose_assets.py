"""Pin the release attachments to what makes them self-contained.

`v0.1.0` published `deploy/compose.yaml` verbatim. Every SpeechNexus service kept
`build:` and had no `image:`, `minio` named `speechnexus-minio:RELEASE.…` — a tag that
only existed on the building host — and `minio-init` bind-mounted
`./init-object-storage.py` from a checkout the downloading host does not have. The
attachment therefore resolved to `<project>-api` instead of
`ghcr.io/hrygo/speechnexus-python` and failed with
`resolve : lstat .../deploy: no such file or directory`.

These cases generate the attachment from the real `deploy/compose.yaml` and a synthetic
digest list, then assert the properties a reader relies on:

* every service runs an anonymously pullable reference — a published `ghcr.io` digest
  or the one public database image — and no service carries `build:` or a build
  context, so no checkout is needed to resolve the file;
* the scripts the compose file mounts are attachments of the same release, copied
  beside it and mounted by their published names, so the set is closed under what the
  file needs;
* the manifest states tag, commit, every digest, the migration head read out of the
  image, and the checksums of the rest of the set, without opening the documentation;
* the workflow publishes that generated set, publishes `minio` (the upstream image is
  not anonymously pullable any more), and reads the migration head from the image
  instead of transcribing it.

Nothing here builds an image or reaches a registry: the release path is pinned as text
and as a generator run over the repository's own compose file.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "deploy/release-assets.py"
COMPOSE = ROOT / "deploy/compose.yaml"
WORKFLOW = (ROOT / ".github/workflows/release-images.yml").read_text()
README = ROOT / "deploy/README.md"

TAG = "v9.9.9"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
HEAD = "0057_asr_settlement"
HEAD_IMAGE = f"ghcr.io/hrygo/speechnexus-python:{TAG}@sha256:{'a' * 64}"

#: Every repository `images.txt` carries for this compose file, plus the image that is
#: published for the operator without being a service here.
REPOSITORIES = (
    "speechnexus-python",
    "speechnexus-web-admin",
    "speechnexus-web-workbench",
    "speechnexus-node-controller",
    "speechnexus-asset-inspector",
    "speechnexus-minio",
)

#: The scripts `deploy/compose.yaml` bind-mounts from its own directory.
SCRIPTS = (
    "init-object-storage.py",
    "job-startup-gate.py",
    "workbench-startup-gate.py",
)

PUBLIC_IMAGE = "postgres:16.15-alpine"
PINNED = re.compile(r"ghcr\.io/hrygo/speechnexus-[a-z-]+@sha256:[0-9a-f]{64}")


def digest_of(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def images_text() -> str:
    return (
        "\n".join(
            f"ghcr.io/hrygo/{repository}:{TAG}@sha256:{digest_of(repository)}"
            for repository in REPOSITORIES
        )
        + "\n"
    )


def compose_services(text: str) -> dict[str, list[str]]:
    """Service name -> its `image:` lines, from a compose file's own text."""
    lines = text.splitlines()
    start = lines.index("services:")
    services: dict[str, list[str]] = {}
    name: str | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        header = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):$", line)
        if header:
            name = header.group(1)
            services[name] = []
            continue
        if name and line.startswith("    image: "):
            services[name].append(line.split("image: ", 1)[1].strip())
    return services


def generate(
    out: Path,
    images: Path,
    head: str = HEAD,
    compose: Path = COMPOSE,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--compose",
            str(compose),
            "--images",
            str(images),
            "--tag",
            TAG,
            "--version",
            TAG.lstrip("v"),
            "--commit",
            COMMIT,
            "--migration-head",
            head,
            "--migration-head-image",
            HEAD_IMAGE,
            "--out-dir",
            str(out),
            "--created-at",
            "2026-01-01T00:00:00+00:00",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class GeneratedAssets:
    """One generator run over the repository's compose file and a fixture digest list."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.images = self.dir / f"speechnexus-{TAG}-images.txt"
        self.images.write_text(images_text(), encoding="utf-8")
        self.result = generate(self.dir, self.images)
        self.compose_path = self.dir / f"speechnexus-{TAG}-compose.yaml"
        self.manifest_path = self.dir / f"speechnexus-{TAG}-release-manifest.json"
        self.compose = self.compose_path.read_text() if self.compose_path.exists() else ""
        self.manifest = (
            json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
        )

    def close(self) -> None:
        self._tmp.cleanup()

    def script(self, name: str) -> Path:
        return self.dir / f"speechnexus-{TAG}-{name}"


class PinnedServiceTests(unittest.TestCase):
    assets: GeneratedAssets
    generated: dict[str, list[str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.assets = GeneratedAssets()
        cls.generated = compose_services(cls.assets.compose)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.assets.close()

    def test_generation_succeeds(self) -> None:
        self.assertEqual(
            self.assets.result.returncode, 0, self.assets.result.stdout + self.assets.result.stderr
        )

    def test_every_service_in_the_development_file_survives_the_rewrite(self) -> None:
        """A mapping that forgets a service must fail here, not publish a local name."""
        self.assertEqual(set(self.generated), set(compose_services(COMPOSE.read_text())))

    def test_each_service_has_exactly_one_anonymously_pullable_image(self) -> None:
        for service, images in self.generated.items():
            with self.subTest(service=service):
                self.assertEqual(len(images), 1, images)
                reference = images[0]
                if service == "postgres":
                    self.assertEqual(reference, PUBLIC_IMAGE)
                else:
                    # `<project>-api` (the old attachment's resolved name) cannot match
                    # this: a pinned reference names the registry, the repository and
                    # a digest, and nothing here is inferred from the project name.
                    self.assertRegex(reference, PINNED)

    def test_no_service_carries_a_build_or_a_context(self) -> None:
        service = "top level"
        offenders: list[tuple[str, str]] = []
        for line in self.assets.compose.splitlines():
            header = re.match(r"^  ([a-z0-9][a-z0-9-]*):$", line)
            if header is not None:
                service = header[1]
            if line.lstrip().startswith("#"):
                continue
            if re.match(r"^ {2,6}(build|context|dockerfile):", line):
                offenders.append((service, line.strip()))
        for service, line in offenders:
            with self.subTest(service=service):
                self.fail(f"{service} still carries {line!r}")
        self.assertNotIn("*python-build", self.assets.compose)

    def test_comments_point_every_command_at_the_attachment_directory(self) -> None:
        # Comments are preserved from the source file, but a command copied out of
        # them has to name a path the reader actually has: `deploy/compose.env`
        # does not exist beside a downloaded attachment.
        self.assertNotIn("deploy/compose.env", self.assets.compose)
        self.assertIn("--env-file compose.env", self.assets.compose)

    def test_the_number_of_pinned_services_matches_the_manifest(self) -> None:
        pinned = [
            service for service, images in self.generated.items() if PINNED.fullmatch(images[0])
        ]
        self.assertEqual(self.assets.manifest["compose"]["digest_pinned_services"], len(pinned))
        self.assertEqual(self.assets.manifest["compose"]["build_sections"], 0)

    def test_every_published_repository_is_used_by_the_compose_file(self) -> None:
        for repository in REPOSITORIES:
            if repository == "speechnexus-node-controller":
                continue  # published for the operator, not a service here
            with self.subTest(repository=repository):
                self.assertIn(
                    f"ghcr.io/hrygo/{repository}@sha256:{digest_of(repository)}",
                    self.assets.compose,
                )

    def test_a_service_without_an_image_decision_fails_the_generation(self) -> None:
        text = COMPOSE.read_text()
        marker = "\nvolumes:\n"
        self.assertIn(marker, text)
        hostile = text.replace(
            marker,
            '\n  mystery:\n    build: *python-build\n    command: ["true"]\n' + marker,
            1,
        )
        source = self.assets.dir / "compose-with-a-new-service.yaml"
        source.write_text(hostile, encoding="utf-8")
        result = generate(self.assets.dir, self.assets.images, compose=source)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("services without an image decision: mystery", result.stderr)

    def test_an_image_missing_from_the_list_fails_the_generation(self) -> None:
        partial = self.assets.dir / "images-without-minio.txt"
        partial.write_text(
            "\n".join(
                line for line in images_text().splitlines() if "speechnexus-minio" not in line
            )
            + "\n",
            encoding="utf-8",
        )
        result = generate(self.assets.dir / "partial", partial)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("images.txt does not name speechnexus-minio", result.stderr)

    def test_a_migration_head_that_was_not_read_out_of_an_image_is_refused(self) -> None:
        result = generate(self.assets.dir / "bad-head", self.assets.images, head="latest main")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not a migration revision id", result.stderr)


class MountedScriptTests(unittest.TestCase):
    assets: GeneratedAssets
    compose: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.assets = GeneratedAssets()
        cls.compose = cls.assets.compose

    @classmethod
    def tearDownClass(cls) -> None:
        cls.assets.close()

    def test_the_scripts_are_published_next_to_the_compose_file(self) -> None:
        for script in SCRIPTS:
            with self.subTest(script=script):
                attachment = self.assets.script(script)
                self.assertTrue(attachment.is_file(), attachment)
                self.assertEqual(attachment.read_text(), (COMPOSE.parent / script).read_text())

    def test_each_mount_names_the_attachment_instead_of_the_checkout(self) -> None:
        source = COMPOSE.read_text()
        for script in SCRIPTS:
            with self.subTest(script=script):
                self.assertIn(f"- ./{script}:", source)
                self.assertNotIn(f"- ./{script}:", self.compose)
                target = re.search(
                    rf"^      - \./{re.escape(script)}:(?P<target>/\S+):ro$", source, re.M
                )
                if target is None:
                    self.fail(f"the development compose does not mount {script}")
                self.assertIn(
                    f"      - ./speechnexus-{TAG}-{script}:{target['target']}:ro\n", self.compose
                )

    def test_the_manifest_checksums_the_scripts(self) -> None:
        entries = {
            entry["name"]: entry for entry in self.assets.manifest["assets"] if entry["sha256"]
        }
        for script in SCRIPTS:
            with self.subTest(script=script):
                name = f"speechnexus-{TAG}-{script}"
                self.assertIn(name, entries)
                self.assertEqual(
                    entries[name]["sha256"],
                    hashlib.sha256(self.assets.script(script).read_bytes()).hexdigest(),
                )
                self.assertTrue(entries[name]["mounted_as"].startswith("/opt/speechnexus/"))


class ManifestTests(unittest.TestCase):
    assets: GeneratedAssets
    manifest: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.assets = GeneratedAssets()
        cls.manifest = cls.assets.manifest

    @classmethod
    def tearDownClass(cls) -> None:
        cls.assets.close()

    def test_the_manifest_identifies_the_release(self) -> None:
        self.assertEqual(self.manifest["release"]["tag"], TAG)
        self.assertEqual(self.manifest["release"]["commit"], COMMIT)
        self.assertEqual(self.manifest["release"]["short_commit"], COMMIT[:12])

    def test_the_migration_head_names_the_image_it_was_read_from(self) -> None:
        head = self.manifest["migration_head"]
        self.assertEqual(head["value"], HEAD)
        self.assertEqual(head["read_from"], HEAD_IMAGE)
        self.assertIn("executed inside that image", head["read_by"])
        # The comparison a reader has to be able to make without the docs.
        self.assertIn("predate", self.manifest["how_to_read"])

    def test_every_published_image_is_recorded_with_its_digest(self) -> None:
        images = {entry["repository"]: entry for entry in self.manifest["images"]}
        self.assertEqual(set(images), set(REPOSITORIES))
        for repository, entry in images.items():
            with self.subTest(repository=repository):
                self.assertEqual(entry["digest"], f"sha256:{digest_of(repository)}")
                self.assertEqual(
                    entry["reference"],
                    f"ghcr.io/hrygo/{repository}:{TAG}@{entry['digest']}",
                )
        self.assertFalse(images["speechnexus-node-controller"]["in_compose"])
        self.assertIn("api", images["speechnexus-python"]["services"])
        self.assertEqual(
            images["speechnexus-python"]["services"],
            sorted(images["speechnexus-python"]["services"]),
        )

    def test_the_public_database_image_is_declared_not_implied(self) -> None:
        third_party = {entry["service"]: entry for entry in self.manifest["third_party_images"]}
        self.assertEqual(third_party["postgres"]["image"], PUBLIC_IMAGE)
        self.assertFalse(third_party["postgres"]["digest_pinned"])

    def test_the_asset_list_covers_the_whole_attachment_set(self) -> None:
        names = [entry["name"] for entry in self.manifest["assets"]]
        expected = {
            f"speechnexus-{TAG}-images.txt",
            f"speechnexus-{TAG}-compose.yaml",
            f"speechnexus-{TAG}-release-manifest.json",
            f"speechnexus-{TAG}-SHA256SUMS",
            *(f"speechnexus-{TAG}-{script}" for script in SCRIPTS),
        }
        self.assertEqual(set(names), expected)
        hashed = {entry["name"]: entry["sha256"] for entry in self.manifest["assets"]}
        for name in (f"speechnexus-{TAG}-images.txt", f"speechnexus-{TAG}-compose.yaml"):
            with self.subTest(name=name):
                self.assertEqual(
                    hashed[name], hashlib.sha256((self.assets.dir / name).read_bytes()).hexdigest()
                )


class WorkflowTests(unittest.TestCase):
    def test_the_attachment_is_generated_instead_of_copied(self) -> None:
        self.assertNotIn("cp deploy/compose.yaml", WORKFLOW)
        self.assertIn("python3 deploy/release-assets.py", WORKFLOW)
        self.assertIn('--commit "$COMMIT"', WORKFLOW)

    def test_the_migration_head_is_read_from_the_published_image(self) -> None:
        self.assertIn("docker run --rm --entrypoint python", WORKFLOW)
        self.assertIn("get_current_head()", WORKFLOW)
        # The generation step must consume the step output, not a literal.
        self.assertIn("MIGRATION_HEAD: ${{ steps.head.outputs.value }}", WORKFLOW)
        self.assertNotIn('--migration-head "0057', WORKFLOW)

    def test_minio_is_published(self) -> None:
        self.assertIn('"repository": "speechnexus-minio"', WORKFLOW)
        self.assertIn('"dockerfile": "deploy/images/minio.Dockerfile"', WORKFLOW)
        self.assertIn("!= 6", WORKFLOW)

    def test_every_generated_asset_is_uploaded_and_read_back(self) -> None:
        for name in (
            '"speechnexus-${TAG}-release-manifest.json"',
            'speechnexus-"${TAG}"-*.py',
        ):
            with self.subTest(name=name):
                self.assertIn(name, WORKFLOW)
        # The read-back expands the same glob the upload uses: a fourth mounted
        # script cannot be uploaded and silently skipped by the check.
        self.assertIn('scripts=(speechnexus-"${TAG}"-*.py)', WORKFLOW)
        self.assertIn('for script in "${scripts[@]}"; do', WORKFLOW)
        self.assertNotIn("for script in init-object-storage.py", WORKFLOW)
        # The read-back is the guard that the upload actually landed.
        self.assertGreaterEqual(WORKFLOW.count("release-manifest.json"), 5)

    def test_the_read_back_refuses_an_empty_script_glob(self) -> None:
        # `speechnexus-<tag>-*.py` matching nothing must fail loudly instead of
        # looping over a literal pattern that `grep -qx` would never match.
        self.assertIn("if [[ ${#scripts[@]} -eq 0 ]]; then", WORKFLOW)


class DocumentedCommandTests(unittest.TestCase):
    """The README's deployment commands must run on a host with no checkout."""

    def test_the_download_names_the_repository(self) -> None:
        # `gh release download` resolves the repository from the working tree when
        # `--repo` is absent; the section it sits in promises "No checkout is
        # involved", where that resolution fails with `not a git repository`.
        self.assertIn(
            'gh release download "$tag" --repo hrygo/SpeechNexus --pattern "speechnexus-${tag}-*"',
            README.read_text(),
            "deploy/README.md must name the repository, because this section skips the checkout",
        )

    def test_every_documented_compose_command_passes_the_env_file(self) -> None:
        for line in README.read_text().splitlines():
            if "docker compose" not in line or '-f "speechnexus-' not in line:
                continue
            with self.subTest(line=line):
                self.assertIn("--env-file compose.env", line)


if __name__ == "__main__":
    unittest.main()
