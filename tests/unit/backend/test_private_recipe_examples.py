"""Pin the committed private recipe examples to the loaders that consume them.

`deploy/private/*.example.json` are the only copyable statement of the private
schemas: the transcription and speech recipes, and the gateway configs. They are
deliberately schema-valid, so an operator can copy one and replace identities. A
sample that drifts from its loader is worse than no sample: it teaches a shape the
deployment rejects, and the failure surfaces only in a container traceback.

These cases make the drift visible in two directions:

- every committed recipe example loads through the production loader, including
  its RFC 8785 `recipe_digest`;
- removing a required field is an explicit rejection, never a defaulted value —
  the loaders forbid extra keys and require every one of them;
- the deployment-side checker reports which fields still carry the example's
  value and which are undecidable, rejects a partial replacement, and never
  claims more than "the files were replaced".

The examples themselves must stay credential-free: a real token, host path or
bucket in git is a leak, not a fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import rfc8785

from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    load_transcription_recipe,
)
from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "deploy" / "private"
COMPOSE = ROOT / "deploy" / "compose.yaml"
CHECKER = ROOT / "scripts" / "dev-env" / "check-tts-assembly.py"
TTS_RECIPE = EXAMPLES / "speech-recipe.example.json"
ASR_RECIPE = EXAMPLES / "transcription-recipe.example.json"
TTS_GATEWAY = EXAMPLES / "gateway-file-tts.example.json"
TTS_ENDPOINT = EXAMPLES / "gateway-file-tts-endpoint.example.json"

# The replacement values a deployment would use. They are deliberately free of the
# example's `-compose-example` marker and of each other's identities, so a fixture
# built from them is "every field really replaced" rather than "most fields".
REPLACED = {
    "model": "tts-approved-model",
    "binding": "binding-revision-approved-tts",
    "manifest": "capacity-manifest-approved-tts",
    "policy": "policy-revision-approved-tts",
    "template": "template-approved-tts",
    "stage": "stage-approved-tts",
    "instance": "instance-tts-approved-1",
    "node": "node-approved-1",
    "gateway": "gateway-approved-1",
    "pool": "tts-batch",
    "digest": "sha256:" + "a" * 64,
    "voice": "voice-approved",
    "voice_revision_id": "voice-revision-approved",
    "voice_policy": "policy-tts-approved",
    "address": "10.44.7.9",
    "port": 18299,
}


def _private_copy(source: Path, directory: Path, name: str) -> Path:
    """Copy one example where the production loader will accept it (mode 0600)."""
    target = directory / name
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return target


def _redigest(document: dict[str, Any]) -> dict[str, Any]:
    """Recompute the recipe digest the way the loader verifies it."""
    body = {key: value for key, value in document.items() if key != "recipe_digest"}
    document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    return document


def write_assembly(directory: Path, *, replace: bool, keep: tuple[str, ...] = ()) -> Path:
    """Write a three-file TTS assembly, fully replaced or (mostly) as committed.

    ``keep`` names recipe fields to leave at the example's value even when
    ``replace`` is true, so a partial replacement is expressible instead of being
    a special-case branch in a test body.
    """
    recipe = json.loads(TTS_RECIPE.read_text(encoding="utf-8"))
    endpoint = json.loads(TTS_ENDPOINT.read_text(encoding="utf-8"))
    gateway = json.loads(TTS_GATEWAY.read_text(encoding="utf-8"))
    if replace:
        recipe.update(
            model_id=REPLACED["model"],
            binding_revision_id=REPLACED["binding"],
            manifest_id=REPLACED["manifest"],
            policy_revision_id=REPLACED["policy"],
        )
        recipe["stage"].update(stage_id=REPLACED["stage"], instance_id=REPLACED["instance"])
        recipe["runtime_plan"].update(
            topology_digest=REPLACED["digest"],
            qualification_digest="sha256:" + "b" * 64,
        )
        recipe["runtime_plan"]["binding"].update(
            target_node_id=REPLACED["node"],
            pool_id=REPLACED["pool"],
            gateway_id=REPLACED["gateway"],
            approved_template_id=REPLACED["template"],
            binding_revision_id=REPLACED["binding"],
            manifest_id=REPLACED["manifest"],
            config_digest="sha256:" + "c" * 64,
        )
        endpoint["target"].update(
            host_node_id=REPLACED["node"],
            gateway_id=REPLACED["gateway"],
            pool_id=REPLACED["pool"],
            instance_id=REPLACED["instance"],
            binding_revision_id=REPLACED["binding"],
        )
        endpoint.update(
            address=REPLACED["address"],
            port=REPLACED["port"],
            served_model=REPLACED["model"],
            bearer_token="0123456789abcdef" * 4,
        )
        gateway["builtin_voice_binding"].update(
            voice_id=REPLACED["voice"],
            voice_revision_id=REPLACED["voice_revision_id"],
        )
        gateway["tts_policy"]["config_revision"] = REPLACED["voice_policy"]
        for field in keep:
            recipe[field] = json.loads(TTS_RECIPE.read_text(encoding="utf-8"))[field]
        _redigest(recipe)
    (directory / "speech-recipe.json").write_text(json.dumps(recipe), encoding="utf-8")
    (directory / "gateway-file-tts.json").write_text(json.dumps(gateway), encoding="utf-8")
    (directory / "gateway-file-tts-endpoint.json").write_text(
        json.dumps(endpoint), encoding="utf-8"
    )
    for path in directory.iterdir():
        path.chmod(0o600)
    return directory


class RecipeExampleLoadsTests(unittest.TestCase):
    def test_the_speech_recipe_example_satisfies_the_production_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _private_copy(TTS_RECIPE, Path(directory), "speech-recipe.json")
            recipe = load_tts_recipe(path)
        self.assertEqual(recipe.approved.stages[0].runtime_plan.binding.gateway_id, "gateway-1")
        self.assertEqual(recipe.approved.quota_dimensions, frozenset({"concurrent_attempts"}))
        self.assertGreater(recipe.approved.admission_expires_at_us, 0)

    def test_the_transcription_recipe_example_still_satisfies_its_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _private_copy(ASR_RECIPE, Path(directory), "transcription-recipe.json")
            recipe = load_transcription_recipe(path)
        # The published and executing binding revisions must agree
        # (transcription_recipe.py:218); the sample carried two different values and
        # therefore could not be loaded at all before #821.
        self.assertEqual(recipe.approved.binding_revision_id, recipe.binding_publication_id)
        self.assertEqual(recipe.stage_id, "stage-asr-compose-example")

    def test_a_removed_required_field_is_rejected_not_defaulted(self) -> None:
        document = json.loads(TTS_RECIPE.read_text(encoding="utf-8"))
        del document["stage"]
        _redigest(document)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "speech-recipe.json"
            target.write_text(json.dumps(document), encoding="utf-8")
            target.chmod(0o600)
            with self.assertRaises(ValueError) as raised:
                load_tts_recipe(target)
        # The refusal names the file and the failure class: "forgot to copy" and
        # "stale digest" need different operator actions.
        message = str(raised.exception)
        self.assertTrue(message.startswith("Invalid private speech admission recipe "), message)
        self.assertIn(str(target), message)
        self.assertIn("ValidationError", message)

    def test_a_stale_digest_is_rejected(self) -> None:
        document = json.loads(TTS_RECIPE.read_text(encoding="utf-8"))
        document["model_id"] = "tts-replaced-without-redigest"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "speech-recipe.json"
            target.write_text(json.dumps(document), encoding="utf-8")
            target.chmod(0o600)
            with self.assertRaises(ValueError):
                load_tts_recipe(target)

    def test_a_recipe_the_api_is_pointed_at_must_exist(self) -> None:
        # Composition-level failure: the loader rejects a missing file instead of
        # returning an empty recipe, so a declared path that does not exist stops
        # the process instead of silently leaving synchronous TTS unassembled.
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(ValueError) as raised,
        ):
            load_tts_recipe(Path(directory) / "absent.json")
        self.assertIn("FileNotFoundError", str(raised.exception))


class ExampleHygieneTests(unittest.TestCase):
    def test_the_examples_give_the_speech_recipe_a_runtime_path(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "SPEECHNEXUS_SPEECH_RECIPE_FILE: /run/speechnexus-private/speech-recipe.json", compose
        )
        self.assertTrue(TTS_RECIPE.is_file(), "the mounted recipe needs a committed example")

    def test_the_examples_hold_no_real_credential_or_host_path(self) -> None:
        for path in (TTS_RECIPE, TTS_ENDPOINT, TTS_GATEWAY):
            document = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", document, f"{path.name} must not name a host path")
            self.assertNotIn("/opt/", document, f"{path.name} must not name a private install path")
            parsed = json.loads(document)
            for value in _strings(parsed):
                if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
                    self.assertEqual(
                        value, "0" * 64, f"{path.name} must not carry a real 64-hex credential"
                    )

    def test_the_speech_recipe_example_carries_no_credential_field(self) -> None:
        document = json.loads(TTS_RECIPE.read_text(encoding="utf-8"))
        forbidden = {"bearer_token", "token", "credential", "password", "secret", "api_key"}
        self.assertEqual(sorted(forbidden & set(_keys(document))), [])


class AssemblyCheckerTests(unittest.TestCase):
    def _run(self, directory: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), "--private-dir", str(directory)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_fully_replaced_assembly_reports_no_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(write_assembly(Path(directory), replace=True))
        message = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, message)
        self.assertIn("0 仍是样例值", message)
        self.assertIn("0 不一致", message)
        # The checker must not promise serviceability, let alone "ready".
        self.assertIn("scope:", message)
        self.assertNotIn("replace-ready", message)
        summary = re.search(r"summary: (\d+) 本地检查通过", message)
        assert summary is not None, message
        self.assertGreater(int(summary.group(1)), 20)

    def test_one_field_left_at_the_example_value_is_red(self) -> None:
        # The review's counter-example: everything replaced except policy_revision_id.
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                write_assembly(Path(directory), replace=True, keep=("policy_revision_id",))
            )
        message = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, message)
        self.assertIn("policy_revision_id", message)
        self.assertIn("1 仍是样例值", message)
        self.assertNotIn("0 仍是样例值", message)

    def test_an_unreplaced_example_is_rejected_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(write_assembly(Path(directory), replace=False))
        message = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, message)
        self.assertIn("binding-revision-tts-compose-example", message)
        self.assertIn("bearer_token", message)
        self.assertIn("policy_revision_id", message)

    def test_an_expired_recipe_is_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assembly = write_assembly(Path(directory), replace=True)
            recipe = json.loads((assembly / "speech-recipe.json").read_text(encoding="utf-8"))
            recipe["admission_expires_at_us"] = 1
            _redigest(recipe)
            (assembly / "speech-recipe.json").write_text(json.dumps(recipe), encoding="utf-8")
            result = self._run(assembly)
        message = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, message)
        self.assertIn("admission_expires_at_us: 1 is already in the past", message)

    def test_disagreeing_files_are_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assembly = write_assembly(Path(directory), replace=True)
            endpoint = json.loads(
                (assembly / "gateway-file-tts-endpoint.json").read_text(encoding="utf-8")
            )
            endpoint["target"]["instance_id"] = "instance-tts-other-9"
            (assembly / "gateway-file-tts-endpoint.json").write_text(
                json.dumps(endpoint), encoding="utf-8"
            )
            result = self._run(assembly)
        message = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, message)
        self.assertIn("agreement: recipe.stage.instance_id == endpoint.target.instance_id", message)

    def test_an_absent_private_directory_is_unreadable_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent"
            result = self._run(missing)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("does not exist", result.stderr)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


if __name__ == "__main__":
    unittest.main()
