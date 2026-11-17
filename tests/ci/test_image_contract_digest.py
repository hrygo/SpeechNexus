"""Pin how a built image's baked contract digest is read out of its bundle.

`scripts/dev-env/check-image-contract-digest.py` is the assertion that makes "same
version" reviewable for an image that was built elsewhere: it must find the one digest
the bundle compiled in, ignore the repeated-character placeholders the committed
contract fixtures contain, and report an image that carries no digest at all (one built
before the same-version guard existed) instead of passing it silently.

These tests exercise that reading logic alone, on a temporary directory standing in for
a published bundle; they do not start Docker, because the value being pinned is which
strings count as the digest.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-image-contract-digest.py"

REAL = "sha256:" + "ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7863"
FIXTURE = "sha256:" + "a" * 64


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("image_contract_digest_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _load()


class ScanTests(unittest.TestCase):
    def scan(self, files: dict[str, bytes]) -> tuple[dict[str, list[str]], list[str], bool]:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            for name, content in files.items():
                path = bundle / "assets" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            return cast(tuple[dict[str, list[str]], list[str], bool], CHECK.scan(bundle))

    def test_the_baked_digest_is_found_and_the_fixture_is_not_taken_for_it(self) -> None:
        candidates, fixtures, guard = self.scan(
            {
                "index.js": (
                    f'var a="{FIXTURE}",b="{FIXTURE}",c="{REAL}",d="data-nexus-contract-check"'
                ).encode()
            }
        )
        self.assertEqual(list(candidates), [REAL])
        self.assertEqual(candidates[REAL], ["index.js"])
        self.assertEqual(fixtures, [FIXTURE])
        self.assertTrue(guard)

    def test_a_repeated_character_placeholder_is_never_a_candidate(self) -> None:
        candidates, fixtures, guard = self.scan({"index.js": f'"{FIXTURE}"'.encode()})
        self.assertEqual(candidates, {})
        self.assertEqual(fixtures, [FIXTURE])
        self.assertFalse(guard)

    def test_two_distinct_digests_stay_ambiguous_for_the_caller_to_refuse(self) -> None:
        other = "sha256:" + "0" * 63 + "1"
        candidates, _, _ = self.scan({"a.js": f'"{REAL}"'.encode(), "b.js": f'"{other}"'.encode()})
        self.assertEqual(sorted(candidates), sorted([REAL, other]))

    def test_only_scripts_are_read(self) -> None:
        candidates, _, _ = self.scan(
            {"index.js": f'"{REAL}"'.encode(), "index.css": f'"{FIXTURE}"'.encode()}
        )
        self.assertEqual(list(candidates), [REAL])


if __name__ == "__main__":
    unittest.main()
