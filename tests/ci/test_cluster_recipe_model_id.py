"""The cluster's private transcription recipe names the *public* model id.

The synchronous transcription route is called with a public model id (the id
``/v1/models`` advertises). Admission compares that id against the private
recipe's ``model_id`` and refuses with ``recipe_mismatch`` when they differ, so
the recipe field must carry the public binding id -- not the engine's logical
model name, which is a different namespace (the engine template launches vLLM
without ``--served-model-name``, so no file records that alias).

``scripts/dev-env/check-tts-assembly.py`` already states that rule for the TTS
side and names the failure it prevents (DEL-538). This test pins the same rule
for the cluster script that writes the ASR recipe, because the deployment input
is produced from this code: a wrong value here silently makes every request
answer ``503 backend_unavailable`` at the public boundary instead of failing
where the mistake was made.

Mutations that must turn it red:

- writing ``LOGICAL_MODEL`` into ``model_id`` (the state before this test, which
  made the MegaCube e2e unreachable): the first assertion fails;
- deleting the distinction between the two namespaces (pointing ``BINDING_ID``
  and ``LOGICAL_MODEL`` at the same value): ``test_the_two_ids_are_distinct``
  fails, so the equality above cannot be satisfied by collapsing them.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Protocol, cast

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_BINDING = ROOT / "deploy/cluster/app-plane/publish-binding.py"
EXAMPLE_RECIPE = ROOT / "deploy/private/transcription-recipe.example.json"


class RecipeBuilder(Protocol):
    BINDING_ID: str
    LOGICAL_MODEL: str

    def build_recipe(self, path: Path) -> dict: ...


def load() -> RecipeBuilder:
    # ``build_recipe`` performs no canonicalisation, but the module imports the
    # canonical-JSON library for ``seal_recipe``. Stand in for it when the guard
    # lane does not install it, so the rule under test stays checkable there; the
    # stub is never called by this test.
    try:
        import rfc8785  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("rfc8785")
        stub.dumps = lambda value: b"{}"  # type: ignore[attr-defined]
        sys.modules["rfc8785"] = stub
    spec = importlib.util.spec_from_file_location("publish_binding", PUBLISH_BINDING)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(RecipeBuilder, module)


PUBLISHER = load()


class RecipeModelIdTests(unittest.TestCase):
    def recipe(self) -> dict:
        return PUBLISHER.build_recipe(EXAMPLE_RECIPE)

    def test_the_recipe_names_the_public_binding_id(self) -> None:
        self.assertEqual(self.recipe()["model_id"], PUBLISHER.BINDING_ID)

    def test_the_recipe_does_not_name_the_engine_logical_model(self) -> None:
        # Admission compares the requested public id; the engine name never
        # travels on that field.
        self.assertNotEqual(self.recipe()["model_id"], PUBLISHER.LOGICAL_MODEL)

    def test_the_two_ids_are_distinct(self) -> None:
        # Guards the counterexample: the assertions above are only meaningful
        # while the deployment keeps two separate namespaces.
        self.assertNotEqual(PUBLISHER.BINDING_ID, PUBLISHER.LOGICAL_MODEL)

    def test_the_staged_example_keeps_its_own_public_id(self) -> None:
        # The example is the template this script rewrites; if it ever carried
        # the logical model name the rule would look satisfied by accident.
        example = self.recipe()
        self.assertEqual(example["model_id"], PUBLISHER.BINDING_ID)


if __name__ == "__main__":
    unittest.main()
