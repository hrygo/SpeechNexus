#!/usr/bin/env python3
"""Fail a pull request that declares itself a review but carries non-review paths.

The repository's delivery SOP (`docs/operations/agent-delivery-sop.md`, step 4)
describes two author-discipline commands (`git status --porcelain`,
`git diff origin/main...HEAD --stat`) that keep review pull requests to review
artifacts only. Both are self-checks: CI never reacted to them. A reviewer once
branched from the reviewed head, so the review PR carried implementation files
that were byte-identical to the reviewed head (verified by blob hash), and that
squash merge entered `main` under a `review:` title (`f88279dc`). The reviewed
implementation then merged through a different revision (`4ad6cad7`), so the
code that landed was not the code that was reviewed. This script is the machine
gate for that boundary.

Signals (any one of them makes the pull request a review PR; they are
independent so that a rename of the title or of the branch alone does not
un-gate the boundary):

1. the title starts with ``review:`` (the full-width colon spelling counts too);
2. the head branch starts with ``review/`` or ``docs/review``;
3. structural: the change touches ``docs/reviews/`` and at least one path
   outside it, whatever the title and branch say.

Boundary: every changed path must live under ``docs/reviews/``. Measured on
merged history before this gate existed (33 merged review-titled pull requests,
exact base ``d58127c0``):

* 32 touched exactly one file under ``docs/reviews/`` and nothing else;
* the only exception is the incident above (PR #722): the report plus three
  implementation/test paths lifted from the reviewed head;
* no merged review pull request touched ``docs/validation/`` or any other
  documentation directory, so nothing is relaxed here. (The single
  ``review:``-ish search hit, PR #429, is titled ``ci: add opt-in OpenCode PR
  reviews`` and is an implementation PR; it is not a review PR.)
* evaluating the structural signal over the 648 non-empty, non-merge changesets
  in ``main`` history matches exactly one commit: the incident. Widening it to "touches
  ``docs/reviews/`` at all" matches the same single commit, so the added net
  cost nothing in false positives, while the title/branch signals are what a
  narrow ``docs/reviews/`` boundary would otherwise depend on.

A declared review PR also needs at least one path under ``docs/reviews/``: the
boundary alone is vacuously satisfied by an empty change set, and an empty review
PR carries no report to review.

Boundary limits: this is a path prefix check. It does not hash blobs (it cannot
prove the report reviewed the head it cites), it does not read report contents,
and any file type inside ``docs/reviews/`` counts - implementation dropped at
``docs/reviews/impl.py`` would pass. That directory is not imported or executed,
so the third gap is harmless, but it is a gap.

Trade-off (under-report vs. mis-report). A declared review PR (signals 1-2)
fails closed: if the exact diff cannot be computed it is red, because the cost
of a false green is the incident class above and the remediation is entirely in
the author's hands. A fetch of the base commit is retried once with a fixed
backoff, and the failure text names the fetch, so one network blip does not read
like a real violation. An undeclared pull request whose diff cannot be computed
is reported as not applicable instead of red: the structural signal is a
supplementary net, and turning a transient Git/network failure into a blanket
blocker would mis-report ordinary implementation PRs. That leaves one narrow
residual gap (an unreadable diff plus a renamed title and branch); the CI
routing lane separately degrades to full verification when the same diff is
unavailable. Only a ``pull_request`` event is judged; pushed main history is
already merged.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# The measured boundary; see the module docstring for the evidence. Review
# reports cite other documents by link rather than editing them, so no
# ``docs/validation/`` exception is granted here.
REVIEW_ROOT = "docs/reviews/"

TITLE_PREFIXES = ("review:", "review：")
BRANCH_PREFIXES = ("review/", "docs/review")
SOP = "docs/operations/agent-delivery-sop.md"

# One retry with a fixed backoff: a single network blip must not turn every
# declared review PR red, but the decision stays fail-closed when the base really
# cannot be fetched. The lane's own budget is minutes, and the routing step above
# already attempted the same fetch, so the timeout stays inside that budget.
GIT_TIMEOUT_SECONDS = 30
FETCH_ATTEMPTS = 2
FETCH_BACKOFF_SECONDS = 2.0


class DiffUnavailableError(Exception):
    """The exact base-to-HEAD diff could not be computed."""


@dataclass(frozen=True)
class Signals:
    """Independent reasons to treat one pull request as a review PR."""

    title: bool
    branch: bool
    structure: bool

    def labels(self) -> tuple[str, ...]:
        labels = []
        if self.title:
            labels.append("title prefix")
        if self.branch:
            labels.append("branch prefix")
        if self.structure:
            labels.append("docs/reviews mixed with other paths")
        return tuple(labels)

    def declared(self) -> bool:
        """Signals an author cannot lose by accident: title or branch."""
        return self.title or self.branch

    def any(self) -> bool:
        return self.declared() or self.structure


@dataclass(frozen=True)
class Verdict:
    """Outcome for one pull request."""

    applicable: bool
    ok: bool
    signals: tuple[str, ...]
    violations: tuple[str, ...]
    reports: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "applicable": self.applicable,
            "ok": self.ok,
            "signals": list(self.signals),
            "violations": list(self.violations),
            "reports": list(self.reports),
            "reason": self.reason,
        }


def is_review_path(path: str) -> bool:
    return path.startswith(REVIEW_ROOT)


def detect_signals(title: str, head_ref: str, paths: Sequence[str]) -> Signals:
    """Return the independent review-PR signals for one change set."""
    reviews = [path for path in paths if is_review_path(path)]
    outside = [path for path in paths if not is_review_path(path)]
    return Signals(
        title=title.strip().lower().startswith(TITLE_PREFIXES),
        branch=head_ref.strip().startswith(BRANCH_PREFIXES),
        structure=bool(reviews) and bool(outside),
    )


def judge(signals: Signals, paths: Sequence[str] | None, unavailable: str = "") -> Verdict:
    """Apply the boundary to a change set; ``paths is None`` means unreadable."""
    labels = signals.labels()
    detail = f" ({unavailable})" if unavailable else ""
    if paths is None:
        if signals.declared():
            return Verdict(
                applicable=True,
                ok=False,
                signals=labels,
                violations=(),
                reports=(),
                reason=(
                    "The exact base-to-HEAD diff is unavailable; a declared review PR "
                    f"cannot be verified{detail}."
                ),
            )
        return Verdict(
            applicable=False,
            ok=True,
            signals=labels,
            violations=(),
            reports=(),
            reason=(
                "No review signal, and the exact diff is unavailable so the structural "
                f"signal could not be evaluated either{detail}; undeclared changes are not "
                "blocked here and the routing lane already requires full verification for "
                "an unreadable diff."
            ),
        )
    if not signals.any():
        return Verdict(
            applicable=False,
            ok=True,
            signals=(),
            violations=(),
            reports=(),
            reason=(
                "No review signal: the title is not review-prefixed, the branch is not "
                "review/ or docs/review, and no changed path mixes docs/reviews/ with "
                "another path."
            ),
        )
    violations = tuple(path for path in paths if not is_review_path(path))
    reports = tuple(path for path in paths if is_review_path(path))
    if not violations and reports:
        return Verdict(
            applicable=True,
            ok=True,
            signals=labels,
            violations=(),
            reports=reports,
            reason=f"Review PR changes only docs/reviews/ ({len(reports)} report file(s)).",
        )
    if not reports:
        reason = (
            f"Review PR reports no file under docs/reviews/ ({len(violations)} path(s) outside)."
        )
    else:
        reason = f"Review PR changes {len(violations)} path(s) outside docs/reviews/."
    return Verdict(
        applicable=True,
        ok=False,
        signals=labels,
        violations=violations,
        reports=reports,
        reason=reason,
    )


def _display(path: str) -> str:
    """One log line per path, even when Git reports a newline in the name."""
    return path.replace("\r", "\\r").replace("\n", "\\n")


def _git(cwd: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT_SECONDS
    )


def _git_failure(exc: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> str:
    """One line an operator can read: exit code plus Git's first stderr line."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timeout after {GIT_TIMEOUT_SECONDS}s"
    if isinstance(exc.stderr, bytes):
        stderr = exc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        if stderr:
            return f"exit {exc.returncode}: {stderr[0]}"
    return f"exit {exc.returncode}: {exc}"


def _fetch_base(base: str, cwd: Path) -> None:
    """Fetch the exact base commit, retrying once with a fixed backoff."""
    detail = ""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            _git(cwd, "fetch", "--no-tags", "--depth=1", "origin", base)
            return
        except FileNotFoundError as exc:  # pragma: no cover - git missing from the runner
            raise DiffUnavailableError(f"git is not available: {exc}") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = _git_failure(exc)
            if attempt < FETCH_ATTEMPTS:
                time.sleep(FETCH_BACKOFF_SECONDS)
    raise DiffUnavailableError(
        f"fetching base {base} from origin failed after {FETCH_ATTEMPTS} attempts: {detail}"
    )


def changed_paths(base: str, cwd: Path) -> list[str]:
    """Changed paths between the exact base commit and HEAD.

    ``--no-renames`` keeps a source file renamed into ``docs/reviews/`` visible as
    the deleted original plus the new path, so the original cannot hide inside
    the review directory.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        raise DiffUnavailableError("no exact 40-character base commit SHA was provided")
    try:
        _git(cwd, "cat-file", "-e", f"{base}^{{commit}}")
    except FileNotFoundError as exc:  # pragma: no cover - git missing from the runner
        raise DiffUnavailableError(f"git is not available: {exc}") from exc
    except subprocess.CalledProcessError:
        _fetch_base(base, cwd)
    try:
        raw = _git(cwd, "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--")
    except FileNotFoundError as exc:  # pragma: no cover - git missing from the runner
        raise DiffUnavailableError(f"git is not available: {exc}") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DiffUnavailableError(
            f"computing git diff {base}..HEAD failed: {_git_failure(exc)}"
        ) from exc
    return [name.decode("utf-8", errors="surrogateescape") for name in raw.split(b"\0") if name]


def _write_summary(summary: Path | None, verdict: Verdict) -> None:
    if summary is None:
        return
    status = "not applicable" if not verdict.applicable else ("pass" if verdict.ok else "fail")
    with summary.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"### Review PR boundary: {status}\n\n")
        if verdict.signals:
            handle.write(f"- Signals: {', '.join(verdict.signals)}\n")
        if verdict.reports:
            handle.write(f"- Under `docs/reviews/`: {len(verdict.reports)}\n")
        for path in verdict.violations:
            handle.write(f"- Outside `docs/reviews/`: `{_display(path)}`\n")
        handle.write(f"\n{verdict.reason}\n")


def _report(verdict: Verdict, paths: Sequence[str] | None) -> None:
    """Emit GitHub annotations and the remediation that keeps the check fixable."""
    print(json.dumps(verdict.as_dict(), ensure_ascii=True))
    if verdict.ok:
        print(f"review-pr-boundary: {verdict.reason}")
        return
    reason = verdict.reason
    if paths is None:
        print(f"::error title=Review PR boundary unverifiable::{reason}", file=sys.stderr)
    elif not verdict.reports:
        print(f"::error title=Review PR has no report::{reason}", file=sys.stderr)
        print(
            "Review artifacts live in docs/reviews/; add the report there and keep every "
            "other path out of this pull request.",
            file=sys.stderr,
        )
    if verdict.violations:
        for path in verdict.violations:
            print(f"::error title=Path outside docs/reviews/::{_display(path)}", file=sys.stderr)
    if paths is not None:
        print(
            "Review pull requests carry review artifacts only (see step 4 of "
            f"{SOP}). Read the reviewed code out of the repository instead of branching "
            "from the reviewed head:",
            file=sys.stderr,
        )
        print(
            "  git show <reviewed-head>:<path>                     # print or pipe",
            file=sys.stderr,
        )
        print(
            "  git show <reviewed-head>:<path> > /tmp/<name>       # write outside the repository",
            file=sys.stderr,
        )
        print(
            "Do not `git checkout <reviewed-head>` on this branch and do not copy its tree back "
            "into the repository.",
            file=sys.stderr,
        )
        print(
            "If this pull request is not a review, drop the `review:` title prefix and rename the "
            "branch off `review/`, or move the non-review paths into their own pull request.",
            file=sys.stderr,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="github.event_name")
    parser.add_argument("--base", default="", help="github.event.pull_request.base.sha")
    parser.add_argument("--title", default="", help="github.event.pull_request.title")
    parser.add_argument("--head-ref", default="", help="github.event.pull_request.head.ref")
    parser.add_argument("--summary", type=Path, help="append a Markdown section here")
    args = parser.parse_args(argv)

    if args.event != "pull_request":
        verdict = Verdict(
            applicable=False,
            ok=True,
            signals=(),
            violations=(),
            reports=(),
            reason=(
                f"Event {args.event!r} carries no pull-request title, branch and base diff; "
                "the review boundary is a pull-request-time constraint."
            ),
        )
        _write_summary(args.summary, verdict)
        print(json.dumps(verdict.as_dict(), ensure_ascii=True))
        return 0

    paths: list[str] | None
    unavailable = ""
    try:
        paths = changed_paths(args.base, Path.cwd())
    except DiffUnavailableError as exc:
        paths = None
        unavailable = str(exc)
    signals = detect_signals(args.title, args.head_ref, paths or ())
    verdict = judge(signals, paths, unavailable)
    _write_summary(args.summary, verdict)
    _report(verdict, paths)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
