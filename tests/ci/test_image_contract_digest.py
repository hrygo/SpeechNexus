"""Pin how a built image's baked contract digest is read out of its bundle.

`scripts/dev-env/check-image-contract-digest.py` is the assertion that makes "same
version" reviewable for an image that was built elsewhere: it must find the one digest
the bundle compiled in, ignore the repeated-character placeholders the committed
contract fixtures contain, and report an image that carries no digest at all (one built
before the same-version guard existed) instead of passing it silently.

They also pin the exit-code decision `main()` makes from that reading: an image that
bakes in no digest, an ambiguous bundle, a digest that disagrees and a digest that
matches must not be free to collapse into one another. That decision is the product of
the script, so it is tested through `main()` rather than through the helper alone —
deleting any one verdict has to turn one of these cases red.

Both halves run on a temporary directory standing in for a published bundle; Docker is
replaced by a fake bundle writer, because what is being pinned is which strings count as
the digest and which verdict follows from them.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-image-contract-digest.py"

REAL = "sha256:" + "ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7863"
FIXTURE = "sha256:" + "a" * 64
OTHER = "sha256:" + "0" * 63 + "1"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("image_contract_digest_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _load()


class _Recomputed:
    """Stand-in for the generator module: the value a checkout would recompute."""

    def contract_digest(self, _root: Path) -> str:
        return REAL


def _bundle_writer(files: dict[str, bytes]) -> Callable[[str, str, Path], int]:
    """Replace the Docker copy with writing the given files into the bundle root."""

    def write(_image: str, _bundle_root: str, destination: Path) -> int:
        for name, content in files.items():
            path = destination / "assets" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return len(files)

    return write


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
        candidates, _, _ = self.scan({"a.js": f'"{REAL}"'.encode(), "b.js": f'"{OTHER}"'.encode()})
        self.assertEqual(sorted(candidates), sorted([REAL, OTHER]))

    def test_only_scripts_are_read(self) -> None:
        candidates, _, _ = self.scan(
            {"index.js": f'"{REAL}"'.encode(), "index.css": f'"{FIXTURE}"'.encode()}
        )
        self.assertEqual(list(candidates), [REAL])


class MainExitCodeTests(unittest.TestCase):
    """The verdicts `main()` returns for each reading, with Docker faked out."""

    IMAGE = "speechnexus/web-workbench:under-test"

    def call_main(
        self,
        files: dict[str, bytes],
        *,
        expect: str | None = None,
    ) -> int:
        argv = [SCRIPT.name, self.IMAGE]
        if expect is not None:
            argv += ["--expect", expect]
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(CHECK, "copy_bundle", side_effect=_bundle_writer(files))
            )
            stack.enter_context(mock.patch.object(CHECK, "image_labels", return_value={}))
            stack.enter_context(
                mock.patch.object(CHECK, "load_generator", return_value=_Recomputed())
            )
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            return cast(int, CHECK.main())

    def test_every_verdict_is_reachable(self) -> None:
        cases = {
            "the bundle carries the expected digest": ({"index.js": f'"{REAL}"'.encode()}, 0),
            "the bundle carries no digest": ({"index.js": f'"{FIXTURE}"'.encode()}, 1),
            "the bundle carries two candidates": (
                {"a.js": f'"{REAL}"'.encode(), "b.js": f'"{OTHER}"'.encode()},
                1,
            ),
            "the bundle carries a different digest": ({"index.js": f'"{OTHER}"'.encode()}, 1),
        }
        for name, (files, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.call_main(files), expected)

    def test_expect_compares_against_the_value_it_is_given(self) -> None:
        self.assertEqual(self.call_main({"index.js": f'"{REAL}"'.encode()}, expect=REAL), 0)
        self.assertEqual(self.call_main({"index.js": f'"{OTHER}"'.encode()}, expect=REAL), 1)

    def test_a_check_that_cannot_run_exits_two_rather_than_failing_the_image(self) -> None:
        argv = [SCRIPT.name, self.IMAGE]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            # Intercept below `run()` so its own OSError handling produces the verdict.
            stack.enter_context(
                mock.patch.object(CHECK.subprocess, "run", side_effect=OSError("docker missing"))
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            with self.assertRaises(SystemExit) as raised:
                CHECK.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
