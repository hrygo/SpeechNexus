"""The unreleased-notes gate must be red for a missing entry and green for an empty set.

`scripts/dev-env/check-unreleased-notes.py` is what stops
`docs/releases/unreleased.md` from rotting: an operator who follows the `v0.1.0`
notes and swaps the image hits a read face that answers `42501` unless the page
names the widened grant function. A guard nobody has seen fail is not evidence, so
every case here builds its own throw-away git repository and pins the exit code:

* a migration added after the tag with no entry -> red, and the file is named;
* the entry row deleted while a passing sentence keeps the name -> red (an entry is
  a line of its own, not any occurrence);
* a changed `*_grants` file whose new function is unnamed -> red;
* the same repository with nothing added since the tag -> green, because a guard
  that fails when there is nothing to check gets disabled;
* a checkout with no reachable tag -> exit 2, never green: CI must fetch full
  history and tags rather than let a shallow clone pass silently.

The cases use temporary repositories instead of this checkout: the real page lists
whatever the last release left behind, so asserting on it here would rot at the next
tag. The real checkout is judged by the `docs_refs` CI job, which fetches the tag.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-unreleased-notes.py"
MIGRATIONS = "backend/src/speechnexus_backend/speech/infrastructure/migrations/versions"
GRANTS = "backend/src/speechnexus_backend/speech/infrastructure/grants.py"
NOTES = "docs/releases/unreleased.md"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {completed.stderr}")
    return completed.stdout


def _write(repository: Path, relative: str, text: str) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repository: Path, message: str) -> None:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", message)


def _run(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


class UnreleasedNotesGateTest(unittest.TestCase):
    """One released state, then one change on top of it."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.repo = Path(self._temporary.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "gate@example.invalid")
        _git(self.repo, "config", "user.name", "gate")
        _write(self.repo, f"{MIGRATIONS}/0057_seed.py", '"""seed revision"""\n')
        _write(self.repo, GRANTS, "def alpha_grants(schema, role):\n    return ()\n")
        _write(
            self.repo,
            NOTES,
            "# Unreleased\n\n| change | what |\n| --- | --- |\n"
            "| `0057_seed` | seed revision |\n"
            "| `alpha_grants` | seed grants |\n",
        )
        _commit(self.repo, "seed the released state")
        _git(self.repo, "tag", "v0.1.0")

    def test_nothing_after_the_tag_is_green(self) -> None:
        completed = _run(self.repo)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("empty set passes", completed.stdout)
        self.assertIn("RESULT OK", completed.stdout)

    def test_a_new_migration_must_have_an_entry_and_is_named_when_missing(self) -> None:
        _write(self.repo, f"{MIGRATIONS}/0058_lease_debt_index.py", '"""new revision"""\n')
        completed = _run(self.repo)
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("migration 0058_lease_debt_index", completed.stdout)
        self.assertIn("RESULT FAIL 1 problem(s) since v0.1.0", completed.stdout)

    def test_deleting_the_entry_row_is_red_even_if_a_sentence_keeps_the_name(self) -> None:
        _write(self.repo, f"{MIGRATIONS}/0058_lease_debt_index.py", '"""new revision"""\n')
        _write(
            self.repo,
            NOTES,
            "# Unreleased\n\n| change | what |\n| --- | --- |\n"
            "| `0057_seed` | seed revision |\n"
            "| `alpha_grants` | seed grants |\n"
            "| `0058_lease_debt_index` | new revision |\n",
        )
        _commit(self.repo, "list the new revision")
        self.assertEqual(0, _run(self.repo).returncode)
        # The row an operator reads goes away; a passing sentence still names it. An
        # occurrence search would stay green here, which is the failure this pins.
        _write(
            self.repo,
            NOTES,
            "# Unreleased\n\nThe revision `0058_lease_debt_index` still exists.\n\n"
            "| change | what |\n| --- | --- |\n"
            "| `0057_seed` | seed revision |\n"
            "| `alpha_grants` | seed grants |\n",
        )
        completed = _run(self.repo)
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("migration 0058_lease_debt_index", completed.stdout)

    def test_a_changed_grants_file_requires_every_function_name(self) -> None:
        _write(
            self.repo,
            GRANTS,
            "def alpha_grants(schema, role):\n    return ()\n\n\n"
            "def beta_grants(schema, role):\n    return ()\n",
        )
        completed = _run(self.repo)
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("grant function beta_grants", completed.stdout)
        self.assertNotIn("grant function alpha_grants", completed.stdout)
        _write(
            self.repo,
            NOTES,
            "# Unreleased\n\n| change | what |\n| --- | --- |\n"
            "| `0057_seed` | seed revision |\n"
            "| `alpha_grants` | seed grants |\n"
            "| `beta_grants` | second group |\n",
        )
        self.assertEqual(0, _run(self.repo).returncode)

    def test_a_missing_notes_page_is_red(self) -> None:
        _write(self.repo, f"{MIGRATIONS}/0058_lease_debt_index.py", '"""new revision"""\n')
        (self.repo / NOTES).unlink()
        completed = _run(self.repo)
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("is missing", completed.stdout)

    def test_a_checkout_without_a_release_tag_is_an_environment_failure(self) -> None:
        _git(self.repo, "tag", "-d", "v0.1.0")
        completed = _run(self.repo)
        self.assertEqual(2, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("no release tag is reachable", completed.stdout)

    def test_a_deleted_migration_is_not_judged(self) -> None:
        # A revision removed after the tag is not a change an operator must apply;
        # asking the notes to name it would be noise with no action behind it.
        (self.repo / f"{MIGRATIONS}/0057_seed.py").unlink()
        _commit(self.repo, "remove a revision")
        completed = _run(self.repo)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
