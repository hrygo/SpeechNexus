"""The endpoint's instance generation follows the ledger, and drift fails a check.

``gateway-file-asr-endpoint.json`` pins which ``(instance_id, instance_generation)``
the execution gateway submits to. The generation is a ledger fact
(``runtime_instance_heads.current_generation``); a hand-typed number either lags
(then every submit is refused with ``stale_instance_generation`` while readiness
still reports the instance valid) or leads (the gateway submits to a generation the
coordinator never accepted). Both are silent, so #1291 asks for a guard rather than
a convention.

This test imports ``deploy/cluster/render-endpoint.py`` and drives its pure
functions plus ``main`` against an injected ledger reader. It never opens a
database and never writes into the repository: the endpoint document lives in a
temporary directory.

Mutations that must turn it red:

- ignoring the ledger and keeping the file's number (``with_generation`` returning
  the document unchanged) -- ``test_rendering_follows_the_ledger`` fails;
- making ``--check`` exit 0 while the generations disagree -- the drift case fails;
- treating a missing head row as generation 1 instead of refusing -- the
  ``test_a_missing_head_row_is_refused`` case fails;
- rewriting fields other than ``instance_generation`` -- the untouched-fields
  assertion fails, which is what keeps a re-render from re-pointing the gateway.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Protocol, cast

ROOT = Path(__file__).resolve().parents[2]
RENDER_ENDPOINT = ROOT / "deploy/cluster/render-endpoint.py"

DOCUMENT = {
    "protocol": "vllm_api_key_v1",
    "target": {
        "host_node_id": "node-test",
        "gateway_id": "gateway-test",
        "pool_id": "asr-batch",
        "instance_id": "instance-test",
        "instance_generation": 1,
        "binding_revision_id": "mbrev-test",
    },
    "address": "127.0.0.1",
    "port": 18190,
    "served_model": "engine-model-1",
}


class Renderer(Protocol):
    def generation_from_head(self, head: dict[str, object] | None) -> int: ...

    def with_generation(self, document: dict, generation: int) -> dict: ...

    def disagreements(self, document: dict, generation: int) -> list[str]: ...

    def main(self, argv: list[str] | None = None) -> int: ...


def load() -> Renderer:
    spec = importlib.util.spec_from_file_location("render_endpoint", RENDER_ENDPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Renderer, module)


RENDER = load()


class GenerationDerivationTests(unittest.TestCase):
    def test_generation_comes_from_the_head_row(self) -> None:
        self.assertEqual(RENDER.generation_from_head({"current_generation": 11}), 11)

    def test_a_missing_head_row_is_refused(self) -> None:
        # "no row" is not generation 1: it means the instance was never registered
        # against this database.
        with self.assertRaises(Exception):
            RENDER.generation_from_head(None)

    def test_rendering_follows_the_ledger(self) -> None:
        rewritten = RENDER.with_generation(DOCUMENT, 11)
        self.assertEqual(rewritten["target"]["instance_generation"], 11)

    def test_rendering_touches_only_the_generation(self) -> None:
        rewritten = RENDER.with_generation(DOCUMENT, 11)
        self.assertEqual(
            {key for key in rewritten if key != "target"},
            {key for key in DOCUMENT if key != "target"},
        )
        other = {
            name: value
            for name, value in rewritten["target"].items()
            if name != "instance_generation"
        }
        self.assertEqual(
            other,
            {
                name: value
                for name, value in DOCUMENT["target"].items()
                if name != "instance_generation"
            },
        )

    def test_agreement_and_disagreement_are_explicit(self) -> None:
        self.assertEqual(RENDER.disagreements(DOCUMENT, 1), [])
        problems = RENDER.disagreements(DOCUMENT, 11)
        self.assertEqual(len(problems), 1)
        self.assertIn("instance_generation is 1", problems[0])
        self.assertIn("current_generation", problems[0])


class CheckModeTests(unittest.TestCase):
    """``--check`` is the drift detector the CI lane and the operator both read."""

    def run_check(self, ledger_generation: int | None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endpoint.json"
            path.write_text(json.dumps(DOCUMENT), encoding="utf-8")
            return self.invoke(
                ["--file", str(path), "--check", "--database-url", "postgresql://unused/test"],
                ledger_generation,
            )

    def invoke(self, argv: list[str], ledger_generation: int | None) -> tuple[int, str]:
        errors = io.StringIO()
        stdout = io.StringIO()
        original = RENDER.read_ledger_generation  # type: ignore[attr-defined]

        def fake(**_: object) -> int:
            if ledger_generation is None:
                raise RENDER.LedgerError("the ledger has no head row for this instance")  # type: ignore[attr-defined]
            return ledger_generation

        RENDER.read_ledger_generation = fake  # type: ignore[attr-defined]
        try:
            with redirect_stdout(stdout), redirect_stderr(errors):
                code = RENDER.main(argv)
        finally:
            RENDER.read_ledger_generation = original  # type: ignore[attr-defined]
        return code, errors.getvalue()

    def test_a_matching_file_passes(self) -> None:
        code, _ = self.run_check(1)
        self.assertEqual(code, 0)

    def test_a_lagging_file_fails_the_check(self) -> None:
        code, message = self.run_check(11)
        self.assertEqual(code, 1)
        self.assertIn("instance_generation is 1", message)

    def test_an_unreadable_ledger_fails_the_check(self) -> None:
        code, message = self.run_check(None)
        self.assertEqual(code, 1)
        self.assertIn("the ledger has no head row", message)


if __name__ == "__main__":
    unittest.main()
