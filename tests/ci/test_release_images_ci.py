"""Pin the release-images pipeline to the properties publication depends on.

`tests/ci` runs under `python -m unittest discover` with no project dependencies,
so this file parses the workflow as text (like the sibling image tests) instead of
importing YAML. Nothing here builds an image or reaches a registry: what it pins is
the set of decisions that would otherwise fail silently after a tag was already
published — the native runner pair the other image workflows use, the commit-pinned
actions, the exact permission set, the immutable-asset rule, the release
environment gate, and the shell paths that used to exit 0 on an empty list.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/release-images.yml").read_text()
SIBLINGS = (
    (ROOT / ".github/workflows/asset-inspector-image.yml").read_text(),
    (ROOT / ".github/workflows/node-controller-image.yml").read_text(),
)
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
ASSET_SUFFIXES = ("images.txt", "SHA256SUMS", "compose.yaml", "release-manifest.json")
UPLOAD_STEP = "Upload the assets to the release"
REUPLOAD_STEP = "Re-upload the assets to the release (explicit republish)"


def job_region(job: str) -> str:
    """The text of one job, from its key to the next job key or comment block."""
    match = re.search(rf"\n  {re.escape(job)}:\n", WORKFLOW)
    if match is None:
        raise AssertionError(f"no job {job!r}")
    rest = WORKFLOW[match.end() :]
    boundary = re.search(r"\n  [#a-z][a-z0-9-]*", rest)
    return rest[: boundary.start()] if boundary else rest


def step_block(name: str) -> str:
    """One `- name:` step, from its name to the next step."""
    for chunk in WORKFLOW.split("\n      - name: "):
        if chunk.startswith(name + "\n"):
            return chunk
    raise AssertionError(f"no step named {name!r}")


def step_script(name: str) -> str:
    block = step_block(name)
    body = block.split("        run: |\n", 1)[1]
    lines = []
    for line in body.splitlines(keepends=True):
        if line.strip() and len(line) - len(line.lstrip()) < 10:
            break
        lines.append(line)
    return textwrap.dedent("".join(lines))


def step_if(name: str) -> str:
    match = re.search(r"\n        if: \${{ (.*?) }}\n", step_block(name))
    if match is None:
        raise AssertionError(f"step {name!r} has no if:")
    return match.group(1)


def without_comments(script: str) -> str:
    """Drop shell comment lines so a check cannot be satisfied by prose."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def embedded_python(script: str) -> str:
    """Extract `python3 - <<'PY' … PY` exactly as the runner would see it."""
    return script.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def execute(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def execute_python(source: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def runner_labels(workflow: str) -> set[str]:
    """Every native runner label the `image` job selects."""
    labels = {
        label
        for label in re.findall(r"\n\s+runner:\s*([^\s\]]+)\s*$", workflow, re.M)
        if not label.startswith("[")
    }
    for inline in re.findall(r"\n\s+runner:\s*\[([^\]]+)\]", workflow):
        labels |= {item.strip() for item in inline.split(",") if item.strip()}
    return labels


class WorkflowShapeTests(unittest.TestCase):
    def test_native_runner_pair_matches_the_other_image_workflows(self) -> None:
        """A renamed or mistyped label must fail here, not wait 24 h for a runner."""
        ours = runner_labels(WORKFLOW)
        self.assertEqual(ours, {"ubuntu-24.04", "ubuntu-24.04-arm"})
        for sibling in SIBLINGS:
            self.assertEqual(runner_labels(sibling), ours)

    def test_each_architecture_builds_on_its_own_native_runner(self) -> None:
        arch = re.findall(r"- name: (amd64|arm64)\n\s+runner: (\S+)\n\s+platform: (\S+)", WORKFLOW)
        self.assertEqual(
            arch,
            [
                ("amd64", "ubuntu-24.04", "linux/amd64"),
                ("arm64", "ubuntu-24.04-arm", "linux/arm64"),
            ],
        )
        self.assertIn("image: ${{ fromJSON(needs.metadata.outputs.images) }}", WORKFLOW)

    def test_every_action_is_pinned_to_a_full_commit_sha(self) -> None:
        uses = re.findall(r"uses: (\S+)", WORKFLOW)
        self.assertGreaterEqual(len(uses), 6)
        for action in uses:
            self.assertRegex(action, PINNED_ACTION)

    def test_permissions_are_exactly_the_two_writes_publication_needs(self) -> None:
        block = WORKFLOW.split("\npermissions:\n", 1)[1]
        entries = []
        for line in block.splitlines():
            if not line.strip():
                continue
            if not line.startswith("  "):
                break
            entries.append(line.strip())
        self.assertEqual(entries, ["contents: write", "packages: write"])
        # `id-token` is mentioned in comments only; the granted set must not carry it.
        self.assertNotIn("id-token", "\n".join(entries))

    def test_triggers_are_a_version_tag_push_or_a_manual_dispatch(self) -> None:
        self.assertIn("\n  workflow_dispatch:\n", WORKFLOW)
        self.assertIn('  push:\n    tags: ["v*"]\n', WORKFLOW)
        self.assertIn("        default: v0.1.0\n", WORKFLOW)
        self.assertIn("      republish:\n", WORKFLOW)
        self.assertIn("        type: boolean\n", WORKFLOW)
        # The republish input must default to the non-destructive value.
        republish = WORKFLOW.split("      republish:\n", 1)[1].split("\n  push:", 1)[0]
        self.assertIn("        default: false\n", republish)

    def test_a_run_is_never_cancelled_in_flight(self) -> None:
        self.assertIn("\n  cancel-in-progress: false\n", WORKFLOW)

    def test_no_step_may_fail_without_failing_the_run(self) -> None:
        for tolerated in ("continue-on-error", "|| true", "|| :"):
            self.assertNotIn(tolerated, without_comments(WORKFLOW))

    def test_the_manifest_job_is_gated_by_the_release_environment(self) -> None:
        manifest = job_region("manifest")
        self.assertIn("    needs: [metadata, image]\n", manifest)
        self.assertIn("    environment: release\n", manifest)
        # The gate has to cover the job that writes the durable record.
        self.assertLess(
            manifest.index("    environment: release\n"), manifest.index("    steps:\n")
        )


class ImmutableAssetTests(unittest.TestCase):
    def test_plain_upload_does_not_clobber_and_the_reupload_does(self) -> None:
        plain = without_comments(step_script(UPLOAD_STEP))
        reupload = without_comments(step_script(REUPLOAD_STEP))
        self.assertNotIn("--clobber", plain)
        self.assertIn("--clobber", reupload)
        for script in (plain, reupload):
            for suffix in ASSET_SUFFIXES:
                self.assertIn(f'"speechnexus-${{TAG}}-{suffix}"', script)
        self.assertIn("needs.metadata.outputs.republish", step_if(UPLOAD_STEP))
        self.assertIn("!= 'true'", step_if(UPLOAD_STEP))
        self.assertIn("needs.metadata.outputs.republish", step_if(REUPLOAD_STEP))
        self.assertIn("== 'true'", step_if(REUPLOAD_STEP))

    def test_metadata_refuses_existing_assets_before_building_anything(self) -> None:
        script = step_script("Resolve tag, version and image list")
        self.assertIn('assets="$(gh release view', script)
        self.assertIn("republish=true", script)
        # Refusing after the build matrix starts would push images and then lose the
        # assets; the guard must precede the image list that derives that matrix.
        self.assertLess(script.index("already carries"), script.index('echo "images='))
        self.assertIn(
            "republish: ${{ steps.resolve.outputs.republish }}",
            job_region("metadata"),
        )

    def test_version_tag_regex_rejects_docker_illegal_build_metadata(self) -> None:
        script = step_script("Resolve tag, version and image list")
        match = re.search(r'\[\[ ! "\$tag" =~ (\S+) \]\]', script)
        if match is None:
            raise AssertionError("tag validation regex not found")
        pattern = match.group(1)
        for accepted in ("v0.1.0", "v1.2.3", "v1.2.3-rc.1"):
            self.assertIsNotNone(re.fullmatch(pattern, accepted), accepted)
        # `+` is legal SemVer build metadata and illegal in a Docker tag: accepting it
        # would push both architectures and only fail at the manifest merge.
        for rejected in ("v1.2.3+build.5", "1.2.3", "v1.2", "v1.2.3_rc1"):
            self.assertIsNone(re.fullmatch(pattern, rejected), rejected)


class ReadbackTests(unittest.TestCase):
    def test_an_empty_image_list_fails_the_readback(self) -> None:
        source = embedded_python(
            step_script("Write the digest manifest and verify it against the registry")
        )
        result = execute_python(
            source, {"IMAGE_PREFIX": "example.invalid/x", "TAG": "v0.1.0", "IMAGES": "[]"}
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("No image definitions", result.stderr)

    def test_the_readback_reads_the_written_file_and_requires_lines(self) -> None:
        script = step_script("Write the digest manifest and verify it against the registry")
        self.assertIn("if not recorded or len(recorded) != len(images)", script)
        self.assertIn("hashlib.sha256(raw).hexdigest()", script)
        # The registry is asked again for the digest of the tag the file names.
        self.assertIn('manifest_digest(f"{match', script)
        self.assertIn("registry now reports", script)

    def test_the_merge_step_fails_on_an_unusable_image_list(self) -> None:
        script = step_script("Merge the platform manifests into one image per repository")
        base = {
            "IMAGE_PREFIX": "example.invalid/x",
            "TAG": "v0.1.0",
            "SHORT_SHA": "deadbeef1234",
        }
        for images in ("[]", "not-json"):
            with self.subTest(images=images):
                result = execute(script, {**base, "IMAGES": images})
                self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('<<< "$repositories"', script)
        self.assertNotIn("< <(jq", script)
        # `jq -e` makes the list resolution fail loudly instead of in a subshell.
        self.assertIn("jq -er", script)

    def test_the_release_assets_are_read_back_from_the_page(self) -> None:
        script = step_script("Read the release page back")
        for suffix in ASSET_SUFFIXES:
            self.assertIn(suffix, script)
        self.assertIn('grep -qx "speechnexus-${TAG}-${suffix}"', script)
        self.assertIn("gh release view", script)


if __name__ == "__main__":
    unittest.main()
