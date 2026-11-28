"""The gateway endpoint's generation comes from the ledger, not from an operator (#1291).

``gateway-file-asr-endpoint.json`` names the ``(instance_id, instance_generation)``
the execution gateway submits to. That generation is a coordination fact:
``runtime_instance_heads.current_generation`` advances when the controller prepares a
new generation, so a number typed into the file is wrong on the next generation and
wrong silently -- submits are refused with ``stale_instance_generation`` while the
read side still reports a valid receipt (#1253/#1286 spent a round on exactly that).

These tests pin the derivation and, as the counterexample, the drift check: when the
ledger's generation changes the rendered file must follow, and a file that kept the
old literal must fail ``--check``. The ledger read is faked, so no database is needed.

Mutations that must turn this red:

- hard-coding the generation in ``with_generation`` (the counterexample test then
  renders 1 instead of the ledger's 7);
- deleting the ``--check`` comparison, or making it always agree;
- falling back to a default generation when the ledger has no head row, which would
  point the gateway at a generation nobody prepared;
- deriving anything beyond ``instance_generation`` -- the address, port and binding
  revision are operator inputs, and a re-render that moved them would re-point the
  gateway as a side effect.

The lane runs ``unittest`` with no third-party test dependency, so the temporary
directory and the environment overrides are explicit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Protocol, cast

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/cluster/render-endpoint.py"

DOCUMENT: dict[str, Any] = {
    "protocol": "vllm_api_key_v1",
    "target": {
        "host_node_id": "node-local-1",
        "gateway_id": "gateway-1",
        "pool_id": "asr-batch",
        "instance_id": "instance-asr-1",
        "instance_generation": 1,
        "binding_revision_id": "binding-revision-compose-example",
    },
    "address": "10.0.0.10",
    "port": 18100,
    "served_model": "asr-compose-example",
    "bearer_token": "0" * 64,
}


class Renderer(Protocol):
    class LedgerError(RuntimeError): ...

    def generation_from_head(self, head: dict[str, Any] | None) -> int: ...

    def with_generation(self, document: dict[str, Any], generation: int) -> dict[str, Any]: ...

    def disagreements(self, document: dict[str, Any], generation: int) -> list[str]: ...


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENDERER = cast(Renderer, _load("render_endpoint"))


class DerivationTests(unittest.TestCase):
    def test_the_rendered_generation_is_the_ledger_s(self) -> None:
        rendered = RENDERER.with_generation(DOCUMENT, 7)
        self.assertEqual(rendered["target"]["instance_generation"], 7)
        # The counterexample: a constant writer would have produced 1 here, and the
        # disagreement is exactly what ``--check`` reports.
        self.assertTrue(RENDERER.disagreements(DOCUMENT, 7), "a stale literal must disagree")

    def test_only_the_generation_is_derived(self) -> None:
        rendered = RENDERER.with_generation(DOCUMENT, 7)
        untouched = {key: value for key, value in DOCUMENT.items() if key != "target"}
        self.assertEqual(
            {key: value for key, value in rendered.items() if key != "target"}, untouched
        )
        for key, value in DOCUMENT["target"].items():
            if key == "instance_generation":
                continue
            self.assertEqual(rendered["target"][key], value, key)
        self.assertEqual(DOCUMENT["target"]["instance_generation"], 1)

    def test_agreement_is_quiet(self) -> None:
        self.assertEqual(RENDERER.disagreements(RENDERER.with_generation(DOCUMENT, 3), 3), [])

    def test_an_absent_head_row_refuses_instead_of_defaulting(self) -> None:
        for head in (None, {"current_generation": None}, {"current_generation": 0}):
            with self.subTest(head=head), self.assertRaises(RENDERER.LedgerError):
                RENDERER.generation_from_head(head)

    def test_the_document_without_a_target_is_refused(self) -> None:
        with self.assertRaises(RENDERER.LedgerError):
            RENDERER.with_generation({"protocol": "vllm_api_key_v1"}, 2)
        self.assertTrue(RENDERER.disagreements({"protocol": "vllm_api_key_v1"}, 2))


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.endpoint = Path(self._directory.name) / "gateway-file-asr-endpoint.json"
        self.endpoint.write_text(json.dumps(DOCUMENT, indent=2) + "\n", encoding="utf-8")
        self._environment = dict(os.environ)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.environ.clear()
        os.environ.update(self._environment)

    def test_check_mode_reads_the_ledger_and_never_writes(self) -> None:
        cli = _load("render_endpoint_cli")
        calls: list[dict[str, Any]] = []

        def fake_ledger(**kwargs: Any) -> int:
            calls.append(kwargs)
            return 9

        cli.read_ledger_generation = fake_ledger
        os.environ["SPEECHNEXUS_DATABASE_URL"] = "postgresql://sn_readiness:pw@postgres:5432/db"
        os.environ["SPEECHNEXUS_DATABASE_SCHEMA"] = "speech"
        # The file still says 1 while the ledger says 9: the check fails and the file
        # is byte-identical afterwards, because answering is not writing.
        original = self.endpoint.read_bytes()
        self.assertEqual(cli.main(["--file", str(self.endpoint), "--check"]), 1)
        self.assertEqual(self.endpoint.read_bytes(), original)
        self.assertTrue(calls)
        self.assertEqual(calls[0]["instance_id"], "instance-asr-1")
        self.assertEqual(calls[0]["schema"], "speech")

        # Without --check the same ledger rewrites exactly that field.
        self.assertEqual(cli.main(["--file", str(self.endpoint)]), 0)
        rewritten = json.loads(self.endpoint.read_text(encoding="utf-8"))
        self.assertEqual(rewritten["target"]["instance_generation"], 9)
        self.assertEqual(
            {key: value for key, value in rewritten.items() if key != "target"},
            {key: value for key, value in DOCUMENT.items() if key != "target"},
        )
        self.assertEqual(cli.main(["--file", str(self.endpoint), "--check"]), 0)

    def test_a_missing_dsn_is_a_usage_error(self) -> None:
        cli = _load("render_endpoint_cli_dsn")
        os.environ.pop("SPEECHNEXUS_DATABASE_URL", None)
        self.assertEqual(cli.main(["--file", str(self.endpoint)]), 2)


if __name__ == "__main__":
    unittest.main()
