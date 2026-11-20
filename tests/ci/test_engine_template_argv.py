"""Hold the reference engine templates to the pinned image's real `serve` argv.

`deploy/engines/{file-tts-customvoice,file-voice-design,file-voice-clone}` each pin
one image digest and start it with an explicit `command`. Both halves drifted once
already: the templates passed `--task-type <Kind>` without `--omni`, so the entry
point forwarded the argv to upstream vLLM's parser, which answers
`error: unrecognized arguments: --task-type CustomVoice` (#1075). Text-only guard:
the recorded fixtures under
`tests/fixtures/engine-cli/vllm-omni-sha256-5a12eac1/` are the pinned image's own
option lists plus the accept/reject output; their README records how they were
captured and what could not be proven without a GPU.

Mutations that must turn this red:

- dropping `--omni` from a template (the exact historical failure), or adding any
  flag the pinned image's omni parser does not declare;
- re-adding `--task-type` (recognised only behind `--omni`, and never part of the
  verified startup shape);
- changing `image_digest` without recapturing the fixtures, or pointing a template
  at another image;
- deleting the recorded rejection, which is the evidence that the old argv fails.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENGINES = ROOT / "deploy/engines"
FIXTURES = ROOT / "tests/fixtures/engine-cli/vllm-omni-sha256-5a12eac1"
PINNED_DIGEST = "sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52"
TEMPLATES = (
    "file-tts-customvoice/template.example.json",
    "file-voice-design/template.example.json",
    "file-voice-clone/template.example.json",
)
# Flags the three variants must keep: they carry the memory/latency limits the
# engine notes in each README were measured under.
REQUIRED_FLAGS = {"--omni", "--enforce-eager", "--max-model-len", "--max-num-seqs"}


def option_set(name: str) -> set[str]:
    """The `--flag` names the recorded `serve --help=all` dump printed."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.startswith("--")}


def template(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((ENGINES / name).read_text(encoding="utf-8"))
    return document


def flags(command: list[str]) -> set[str]:
    return {token for token in command if token.startswith("--")}


class PinnedEngineArgvTest(unittest.TestCase):
    """The templates start the image they pin, with flags that image declares."""

    def test_every_template_pins_the_recorded_image(self) -> None:
        for name in TEMPLATES:
            with self.subTest(template=name):
                self.assertEqual(template(name)["image_digest"], PINNED_DIGEST)

    def test_every_template_flag_is_declared_by_the_omni_parser(self) -> None:
        accepted = option_set("serve-options-omni.txt")
        self.assertIn("--omni", accepted)
        for name in TEMPLATES:
            with self.subTest(template=name):
                command = template(name)["command"]
                self.assertEqual(
                    flags(command) - accepted,
                    set(),
                    f"{name} passes flags the pinned image's omni parser does not declare",
                )

    def test_every_template_switches_the_omni_parser_on(self) -> None:
        # `--omni` is the branch switch: without it the entry point hands the argv
        # to upstream vLLM's parser, which is the historical failure.
        for name in TEMPLATES:
            with self.subTest(template=name):
                command = template(name)["command"]
                missing = REQUIRED_FLAGS - flags(command)
                self.assertFalse(missing, f"{name} dropped {sorted(missing)}")
                self.assertEqual(command[:2], ["vllm-omni", "serve"])

    def test_no_template_passes_the_task_type_override(self) -> None:
        # Recognised only behind `--omni`, and never part of the verified startup
        # shape: the checkpoint selects the task. Re-adding it needs a real GPU run
        # recorded here first, not just an argparse surface.
        for name in TEMPLATES:
            with self.subTest(template=name):
                self.assertNotIn("--task-type", template(name)["command"])

    def test_the_recorded_rejection_is_the_pre_fix_argv(self) -> None:
        # Without `--omni` the same flag is unrecognised: this is what the
        # templates produced before the fix, kept so the guard cannot be satisfied
        # by dropping the fixture.
        rejected = (FIXTURES / "rejected-task-type.stderr").read_text(encoding="utf-8")
        self.assertIn("vllm-omni: error: unrecognized arguments: --task-type CustomVoice", rejected)
        self.assertIn("exit=2", rejected)
        self.assertNotIn("--task-type", option_set("serve-options-plain.txt"))
        # And the corrected argv is accepted: the server starts and only the model
        # path is refused, which is the boundary this fixture can prove.
        accepted = (FIXTURES / "accepted-omni.txt").read_text(encoding="utf-8")
        self.assertIn("vLLM server version", accepted)
        self.assertIn("HFValidationError", accepted)


if __name__ == "__main__":
    unittest.main()
