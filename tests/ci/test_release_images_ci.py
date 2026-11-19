"""Pin the release-images pipeline to the properties publication depends on.

The workflow itself is YAML: nothing here builds an image or reaches a registry.
What it pins is the set of decisions that would otherwise fail silently after a tag
was already published — the native runner pair other image workflows use, the
commit-pinned actions, the exact permission set, the immutable-asset rule, the
release environment gate, and the two shell paths that used to exit 0 on an empty
or malformed image list.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/release-images.yml"
# `Any` key: the YAML `on:` key parses as the boolean `True`, not the string "on".
WORKFLOW: dict[Any, Any] = yaml.safe_load(WORKFLOW_PATH.read_text())
# `on` is a YAML 1.1 boolean key, so the trigger block is reachable only as True.
TRIGGERS: dict[str, Any] = WORKFLOW[True]
JOBS: dict[str, Any] = WORKFLOW["jobs"]
SIBLINGS = (
    ROOT / ".github/workflows/asset-inspector-image.yml",
    ROOT / ".github/workflows/node-controller-image.yml",
)
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
UPLOAD_ASSETS = (
    "speechnexus-${TAG}-images.txt",
    "speechnexus-${TAG}-SHA256SUMS",
    "speechnexus-${TAG}-compose.yaml",
)


def steps(job: str) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", JOBS[job]["steps"])


def step_named(job: str, name: str) -> dict[str, Any]:
    for step in steps(job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in job {job!r}")


def run_script(job: str, name: str) -> str:
    return str(step_named(job, name)["run"])


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


def runner_labels(workflow: dict[str, Any]) -> set[str]:
    matrix = workflow["jobs"]["image"]["strategy"]["matrix"]
    if "runner" in matrix:
        return cast("set[str]", set(matrix["runner"]))
    return {str(entry["runner"]) for entry in matrix["arch"]}


class WorkflowShapeTests(unittest.TestCase):
    def test_native_runner_pair_matches_the_other_image_workflows(self) -> None:
        """A renamed or mistyped label must fail here, not wait 24 h for a runner."""
        ours = runner_labels(WORKFLOW)
        self.assertEqual(ours, {"ubuntu-24.04", "ubuntu-24.04-arm"})
        for sibling in SIBLINGS:
            with self.subTest(sibling=sibling.name):
                self.assertEqual(runner_labels(yaml.safe_load(sibling.read_text())), ours)

    def test_both_architectures_are_declared_and_built_on_their_own_runner(self) -> None:
        arch = {entry["name"]: entry for entry in JOBS["image"]["strategy"]["matrix"]["arch"]}
        self.assertEqual(arch["amd64"]["platform"], "linux/amd64")
        self.assertEqual(arch["arm64"]["platform"], "linux/arm64")
        self.assertEqual(arch["amd64"]["runner"], "ubuntu-24.04")
        self.assertEqual(arch["arm64"]["runner"], "ubuntu-24.04-arm")
        matrix = JOBS["image"]["strategy"]["matrix"]["image"]
        self.assertIn("fromJSON(needs.metadata.outputs.images)", matrix)

    def test_every_action_is_pinned_to_a_full_commit_sha(self) -> None:
        for job, definition in JOBS.items():
            for step in definition["steps"]:
                if "uses" not in step:
                    continue
                with self.subTest(job=job, uses=step["uses"]):
                    self.assertRegex(step["uses"], PINNED_ACTION)

    def test_permissions_are_exactly_the_two_writes_publication_needs(self) -> None:
        self.assertEqual(WORKFLOW["permissions"], {"contents": "write", "packages": "write"})
        self.assertNotIn("id-token", json.dumps(WORKFLOW["permissions"]))
        for step in steps("image"):
            self.assertNotIn("id-token", json.dumps(step.get("with", {})))

    def test_triggers_are_a_version_tag_push_or_a_manual_dispatch(self) -> None:
        self.assertEqual(set(TRIGGERS), {"workflow_dispatch", "push"})
        self.assertEqual(TRIGGERS["push"], {"tags": ["v*"]})
        inputs = TRIGGERS["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["ref"]["default"], "v0.1.0")
        self.assertIs(inputs["republish"]["default"], False)
        self.assertEqual(inputs["republish"]["type"], "boolean")

    def test_a_run_is_never_cancelled_in_flight(self) -> None:
        self.assertIs(WORKFLOW["concurrency"]["cancel-in-progress"], False)

    def test_no_step_may_fail_without_failing_the_run(self) -> None:
        for job, definition in JOBS.items():
            for step in definition["steps"]:
                with self.subTest(job=job, step=step.get("name")):
                    self.assertNotIn("continue-on-error", step)
                    self.assertNotIn("|| true", str(step.get("run", "")))
                    self.assertNotIn("|| :", str(step.get("run", "")))

    def test_the_manifest_job_is_gated_by_the_release_environment(self) -> None:
        self.assertEqual(JOBS["manifest"]["environment"], "release")
        self.assertEqual(JOBS["manifest"]["needs"], ["metadata", "image"])


class ImmutableAssetTests(unittest.TestCase):
    def test_plain_upload_does_not_clobber_and_the_reupload_does(self) -> None:
        upload = step_named("manifest", "Upload the assets to the release")
        reupload = step_named(
            "manifest", "Re-upload the assets to the release (explicit republish)"
        )
        self.assertNotIn("--clobber", without_comments(str(upload["run"])))
        self.assertIn("--clobber", without_comments(str(reupload["run"])))
        for step in (upload, reupload):
            self.assertIn("needs.metadata.outputs.republish", str(step["if"]))
            for asset in UPLOAD_ASSETS:
                self.assertIn(asset, str(step["run"]))
        self.assertIn("== 'true'", str(reupload["if"]))
        self.assertIn("!= 'true'", str(upload["if"]))

    def test_metadata_refuses_existing_assets_before_building_anything(self) -> None:
        script = run_script("metadata", "Resolve tag, version and image list")
        self.assertIn('assets="$(gh release view', script)
        self.assertIn("republish=true", script)
        # The guard has to run before the image list is emitted and before the build
        # matrix is derived, otherwise images are pushed and only the assets fail.
        self.assertLess(script.index("already carries"), script.index('echo "images='))
        self.assertEqual(
            JOBS["metadata"]["outputs"]["republish"], "${{ steps.resolve.outputs.republish }}"
        )

    def test_version_tag_regex_rejects_docker_illegal_build_metadata(self) -> None:
        script = run_script("metadata", "Resolve tag, version and image list")
        regex = re.search(r'\[\[ ! "\$tag" =~ (\^[^ ]+\$) \]\]', script)
        assert regex is not None, "tag validation regex not found"
        pattern = regex.group(1)
        for accepted in ("v0.1.0", "v1.2.3", "v1.2.3-rc.1"):
            with self.subTest(accepted=accepted):
                self.assertIsNotNone(re.fullmatch(pattern, accepted))
        # `+` is legal SemVer build metadata and illegal in a Docker tag: accepting it
        # would push both architectures and only fail at the manifest merge.
        for rejected in ("v1.2.3+build.5", "1.2.3", "v1.2", "v1.2.3_rc1"):
            with self.subTest(rejected=rejected):
                self.assertIsNone(re.fullmatch(pattern, rejected))


class ReadbackTests(unittest.TestCase):
    def test_an_empty_image_list_fails_the_readback(self) -> None:
        source = embedded_python(
            run_script("manifest", "Write the digest manifest and verify it against the registry")
        )
        result = execute_python(
            source, {"IMAGE_PREFIX": "example.invalid/x", "TAG": "v0.1.0", "IMAGES": "[]"}
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("No image definitions", result.stderr)

    def test_the_readback_reads_the_written_file_and_requires_lines(self) -> None:
        script = run_script(
            "manifest", "Write the digest manifest and verify it against the registry"
        )
        self.assertIn("if not recorded or len(recorded) != len(images)", script)
        self.assertIn("manifest_digest(f\"{match['ref']}:{match['tag']}\")", script)
        # The digest is recomputed from the registry bytes, not trusted from the
        # `--format` value alone.
        self.assertIn("hashlib.sha256(raw).hexdigest()", script)

    def test_the_merge_step_fails_on_a_malformed_image_list(self) -> None:
        script = run_script(
            "manifest", "Merge the platform manifests into one image per repository"
        )
        base = {"IMAGE_PREFIX": "example.invalid/x", "TAG": "v0.1.0", "SHORT_SHA": "deadbeef1234"}
        malformed = execute(script, {**base, "IMAGES": "not-json"})
        self.assertNotEqual(malformed.returncode, 0, malformed.stdout)
        empty = execute(script, {**base, "IMAGES": "[]"})
        self.assertNotEqual(empty.returncode, 0, empty.stdout)
        self.assertIn("image definitions", empty.stderr)

    def test_the_release_assets_are_read_back_from_the_page(self) -> None:
        script = run_script("manifest", "Read the release page back")
        for suffix in ("images.txt", "SHA256SUMS", "compose.yaml"):
            self.assertIn(suffix, script)
        self.assertIn('grep -qx "speechnexus-${TAG}-${suffix}"', script)
        self.assertIn("gh release view", script)


if __name__ == "__main__":
    unittest.main()
