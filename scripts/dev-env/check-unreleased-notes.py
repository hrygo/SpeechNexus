#!/usr/bin/env python3
"""Fail when an unreleased migration or grant change is missing from the upgrade notes.

Why this exists: `v0.1.0` shipped, six migrations and one widened grant landed on
`main`, and every document said only "re-run the grant functions". A deployment
that followed the `v0.1.0` notes and swapped the image therefore hit a read face
that failed with `42501` and a `/readyz` detail degraded to
`unavailable (permission)`, with nothing anywhere listing what an operator owed.
`docs/releases/unreleased.md` is that list; this guard is what keeps it from
rotting one merge later.

Judged against the most recent release tag:

  * every migration file added or modified since that tag must have its file name
    (without the `versions/` prefix and `.py`) start a line of the notes — a table
    row, a list item or a bullet;
  * every file that defines a `*_grants` function and was added or modified since
    that tag must have each of those function names start a line of the notes.

"Start a line" instead of "appear somewhere": a name that survives only in a
passing sentence is not the entry an operator reads, and searching for any
occurrence would leave the check green after the row itself was deleted. Prose that
merely mentions the name does not count; the failure message says which shape is
required.

The grant rule is deliberately file-scoped rather than diff-scoped: a `*_grants`
file whose only change is a docstring still asks for the name, because telling a
real grant change from a comment change needs a second parser, and a false red here
costs one line in the notes while a missed one costs an operator a silent
permission regression. It also means one changed file can name several functions;
the notes should say which of them actually changed.

Empty set passes: a tag with no migration and no grant change since it owes
nothing, and a guard that fails when there is nothing to check is a guard that gets
disabled.

The change set is `git diff <tag>` plus untracked files, not `<tag>..HEAD`, so a
migration created but not committed in a working copy is caught too; in CI the
checkout is clean and both agree. The tag comes from
`git describe --tags --abbrev=0`, which needs full history: `actions/checkout` with
`fetch-depth: 0` (a shallow clone resolves no tag and this exits 2, never green).

Residual gap, stated rather than implied: the judged names are the ones defined in
the *changed* file. A grant function in an unchanged file can still widen because a
column tuple it imports moved in a module that defines no grants function (for
example `deletion_ledger_schema.py` feeding `deletion_execution_grants`). That
change is invisible here, and the notes' evidence section says so.

Usage:

  scripts/dev-env/check-unreleased-notes.py
  scripts/dev-env/check-unreleased-notes.py --root /tmp/copied-tree
  scripts/dev-env/check-unreleased-notes.py --doc /tmp/notes-copy.md  # tests only
  scripts/dev-env/check-unreleased-notes.py --quiet

Read-only and standard-library only: no database, no pnpm, no project
dependencies. `--root` must be the repository root.

Exit codes: 0 every unreleased migration and grant function is named; 1 at least
one is missing (or the notes file is missing); 2 the question cannot be asked (no
git, no reachable tag, not a repository root).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTES = "docs/releases/unreleased.md"
MIGRATIONS = "backend/src/speechnexus_backend/speech/infrastructure/migrations/versions"
# The same definition shape `tests/ci/test_compose_minimal_role_provisioning.py`
# reads: a module-level `def <name>_grants(`, one name per definition.
GRANT_DEFINITION = re.compile(r"^def ([a-z][a-z0-9_]*_grants)\(", re.MULTILINE)
# Grant definitions live in product source; a test helper named `test_..._grants`
# is not one, and widening the scan to tests would ask the notes to name it.
PRODUCT_SOURCE = re.compile(r"(?:^|/)src/.*\.py$")
# An entry is a line of its own: a table row, a list item or a bullet, possibly
# wrapped in backticks or emphasis. A name mentioned mid-sentence does not count —
# otherwise deleting the row an operator reads would leave the check green on a
# passing reference somewhere else.
ENTRY_PREFIX = re.compile(r"^\s*(?:[|>*+-]+\s*)*[`*_]*")


class GateUnavailableError(Exception):
    """The checkout cannot answer the question; this is an environment failure."""


@dataclass(frozen=True)
class Changed:
    """One changed file and, when it defines any, its grant function names."""

    path: str
    grants: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    """Everything one invocation decided, so callers and tests read one object."""

    tag: str
    migrations: tuple[str, ...]
    changed: tuple[Changed, ...]
    problems: tuple[str, ...]

    @property
    def grant_names(self) -> tuple[str, ...]:
        return tuple(name for change in self.changed for name in change.grants)


def _git(root: Path, *arguments: str) -> str:
    """Run one read-only git command in ``root``; raise when it cannot answer."""
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GateUnavailableError(
            f"git {' '.join(arguments)} failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def base_tag(root: Path) -> str:
    """The most recent release tag reachable from HEAD."""
    try:
        tag = _git(root, "describe", "--tags", "--abbrev=0").strip()
    except GateUnavailableError as error:
        raise GateUnavailableError(
            f"no release tag is reachable: {error}; a shallow checkout must fetch "
            "full history and tags (actions/checkout: fetch-depth: 0)"
        ) from error
    if not tag:
        raise GateUnavailableError("git describe --tags --abbrev=0 named no tag")
    return tag


def changed_paths(root: Path, tag: str, pathspec: str) -> tuple[str, ...]:
    """Paths added or modified since ``tag``, including untracked files.

    `--no-renames` turns a move into an add plus a delete, so the add is judged at
    its new path; `--diff-filter=ACMR` drops the deleted side.
    """
    tracked = _git(
        root,
        "diff",
        "--name-only",
        "--no-renames",
        "--diff-filter=ACMR",
        tag,
        "--",
        pathspec,
    )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", pathspec)
    lines = (*tracked.splitlines(), *untracked.splitlines())
    return tuple(sorted({line for line in lines if line}))


def migration_names(paths: Sequence[str]) -> tuple[str, ...]:
    """Migration revisions since the tag, as the notes must spell them."""
    return tuple(
        sorted(
            {
                Path(path).stem
                for path in paths
                if path.startswith(MIGRATIONS + "/") and path.endswith(".py")
            }
        )
    )


def grant_definitions(root: Path, paths: Sequence[str]) -> tuple[Changed, ...]:
    """Changed product files that define grant functions, with those names."""
    found: list[Changed] = []
    for path in paths:
        if not PRODUCT_SOURCE.search(path) or not path.endswith(".py"):
            continue
        text = (root / path).read_text(encoding="utf-8")
        names = tuple(sorted(set(GRANT_DEFINITION.findall(text))))
        if names:
            found.append(Changed(path=path, grants=names))
    return tuple(found)


def has_entry(text: str, name: str) -> bool:
    """Whether ``name`` starts a table row or list item, not just a sentence."""
    for line in text.splitlines():
        entry = ENTRY_PREFIX.sub("", line, count=1)
        if not entry.startswith(name):
            continue
        rest = entry[len(name) : len(name) + 1]
        if not rest or not (rest.isalnum() or rest == "_"):
            return True
    return False


def check(root: Path, notes: Path) -> Report:
    """Judge one checkout; ``notes`` is the page the names must appear in."""
    prefix = _git(root, "rev-parse", "--show-prefix").strip()
    if prefix:
        raise GateUnavailableError(
            f"--root {root} is inside the repository (prefix {prefix!r}); pass the root"
        )
    tag = base_tag(root)
    paths = changed_paths(root, tag, ".")
    migrations = migration_names(paths)
    changed = grant_definitions(root, paths)
    label = _label(root, notes)
    problems: list[str] = []
    if notes.is_file():
        text = notes.read_text(encoding="utf-8")
    else:
        text = ""
        problems.append(f"{label} is missing: every unreleased change must be listed there")
    hint = "needs an entry line (a table row or list item) that starts with the name"
    for name in migrations:
        if not has_entry(text, name):
            problems.append(f"migration {name} (since {tag}) {hint} in {label}")
    for change in changed:
        for name in change.grants:
            if not has_entry(text, name):
                problems.append(
                    f"grant function {name} ({change.path}, since {tag}) {hint} in {label}"
                )
    return Report(tag=tag, migrations=migrations, changed=changed, problems=tuple(problems))


def _label(root: Path, notes: Path) -> str:
    """Name the notes page the way the command line named it, for the report."""
    try:
        return str(notes.relative_to(root))
    except ValueError:
        return str(notes)


def _notes_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root to check")
    parser.add_argument(
        "--doc",
        default=NOTES,
        help="notes page to read (relative to --root); tests use a copy",
    )
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args(argv)
    notes = _notes_path(args.root, args.doc)
    label = _label(args.root, notes)
    try:
        report = check(args.root, notes)
    except GateUnavailableError as error:
        print(f"FAIL unreleased-notes: {error}")
        return 2
    except OSError as error:
        print(f"FAIL unreleased-notes: cannot read the change set: {error}")
        return 2
    if report.problems:
        for problem in report.problems:
            print(f"FAIL {problem}")
        print(f"RESULT FAIL {len(report.problems)} problem(s) since {report.tag}")
        return 1
    if not args.quiet:
        print(f"ok   base tag    {report.tag}")
        if report.migrations:
            print(
                f"ok   migrations  {len(report.migrations)} changed since {report.tag}, "
                f"all named in {label}"
            )
        else:
            print(f"ok   migrations  none changed since {report.tag} (empty set passes)")
        if report.grant_names:
            print(
                f"ok   grants      {len(report.grant_names)} function(s) in "
                f"{len(report.changed)} changed definition file(s), all named in {label}"
            )
        else:
            print(f"ok   grants      none changed since {report.tag} (empty set passes)")
        print("RESULT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
