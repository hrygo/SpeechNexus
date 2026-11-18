"""The ``python -m`` module guard must be the last thing a worker module does.

``if __name__ == "__main__":`` runs while the module body is still executing, so
any top-level ``def``/``async def``/``class`` below it does not exist yet. In
``asr_settlement_worker`` the guard sat above ``check``, and
``python -m speechnexus_backend.asr_settlement_worker --check`` died with
``NameError: name 'check' is not defined`` while the imported-``main`` test kept
passing. This guard is structural: it reads the sources, it does not import
them, so it needs no project dependency and no service.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "backend/src/speechnexus_backend"

# Top level only: an indented guard is inside some other block, not the module one.
GUARD = re.compile(r"""^if __name__ == ["']__main__["']:\s*(?:#.*)?$""", re.MULTILINE)
# Top level only: indented definitions are part of the guard body or a class.
DEFINITION = re.compile(r"^(?:async def |def |class )")


def late_definitions(source: str) -> list[tuple[int, str]]:
    """Return ``(line_number, text)`` for top-level definitions after the guard."""
    lines = source.splitlines()
    guard_index = next(
        (index for index, line in enumerate(lines) if GUARD.match(line)),
        None,
    )
    if guard_index is None:
        return []
    return [
        (index + 1, line.strip())
        for index, line in enumerate(lines[guard_index + 1 :], start=guard_index + 1)
        if DEFINITION.match(line)
    ]


def offending_files() -> list[str]:
    """Every backend module whose guard precedes a top-level definition."""
    offenders: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        for line_number, text in late_definitions(path.read_text(encoding="utf-8")):
            relative = path.relative_to(ROOT)
            offenders.append(f"{relative}:{line_number}: after the module guard: {text}")
    return offenders


class GuardOrderTests(unittest.TestCase):
    def test_the_repository_has_no_definition_after_a_module_guard(self) -> None:
        offenders = offending_files()
        self.assertEqual(
            offenders,
            [],
            "move `if __name__ == '__main__':` to the end of the file; `python -m` "
            "runs it before anything below it is defined:\n" + "\n".join(offenders),
        )

    def test_the_scan_reaches_the_backend_package(self) -> None:
        """A moved or renamed source root must fail, not silently pass."""
        self.assertTrue(SOURCE.is_dir(), f"{SOURCE} no longer holds the backend package")
        guarded = [
            path for path in SOURCE.rglob("*.py") if GUARD.search(path.read_text(encoding="utf-8"))
        ]
        self.assertGreater(len(guarded), 10, [str(path) for path in guarded])

    def test_a_definition_after_the_guard_is_reported(self) -> None:
        source = (
            "def main() -> int:\n"
            "    return 0\n"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
            "\n"
            "\n"
            "async def check() -> None:\n"
            "    return None\n"
        )
        self.assertEqual(late_definitions(source), [(9, "async def check() -> None:")])

    def test_a_guard_at_the_end_is_clean(self) -> None:
        source = (
            "async def check() -> None:\n"
            "    return None\n"
            "\n"
            "\n"
            "class Runtime:\n"
            "    pass\n"
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(0)\n"
        )
        self.assertEqual(late_definitions(source), [])

    def test_an_indented_definition_is_not_a_top_level_one(self) -> None:
        source = (
            'if __name__ == "__main__":\n'
            "    def main() -> int:\n"
            "        return 0\n"
            "\n"
            "    raise SystemExit(main())\n"
        )
        self.assertEqual(late_definitions(source), [])

    def test_the_settlement_worker_guard_is_last(self) -> None:
        """The regression this guard exists for, pinned by name."""
        worker = SOURCE / "asr_settlement_worker.py"
        self.assertEqual(late_definitions(worker.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
