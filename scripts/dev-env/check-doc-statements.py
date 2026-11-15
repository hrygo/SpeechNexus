#!/usr/bin/env python3
"""Fail when a documented capability statement stops matching the code it cites.

Why this exists: three stale statements had readers treating an existing writer, an
assembled route set and an assembled planner as missing (#823). A reader cannot tell
a stale sentence from a current one, so the sentences that already drifted once are
pinned here as executable assertions:

  * the public Job route set documented in `docs/prd.md` must equal the set the
    router really registers in `jobs.py`, and the router must be mounted by `app.py`;
  * the synchronous-ASR statement in `deploy/README.md` and
    `docs/operations/compose-minimal.md` must cite the terminal-state writer line,
    name the step this deployment does not verify, and never call it missing again;
  * the production planner is assembled in the dispatch runtime, so no current
    document or the delivery matrix may claim it is not.

Each claim also pins its citation: a doc that cites `path:line` must cite a line
that still contains the expected symbol. Reverting the doc or the code therefore
fails here, which is the point - a comment would not.

The guard is read-only and uses only the standard library. Usage:

  scripts/dev-env/check-doc-statements.py            # check this checkout
  scripts/dev-env/check-doc-statements.py --root DIR # check a copied tree

Exit codes: 0 every claim matches, 1 at least one claim drifted, 2 a file is
missing or unreadable.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

JOBS_ROUTER = "backend/src/speechnexus_backend/speech/interfaces/http/jobs.py"
APP = "backend/src/speechnexus_backend/app.py"
DISPATCH_RUNTIME = "backend/src/speechnexus_backend/job_dispatch_runtime.py"
FILE_WORKER = "execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py"
COMPLETION = "execution-gateway/src/speechnexus_gateway/application/execution_completion.py"
PRD = "docs/prd.md"
SPEECH_API = "docs/architecture/speech-api.md"
MANIFEST = "docs/plans/delivery-issues.json"
MATRIX = "docs/plans/delivery-tracking.md"

DOCUMENTED_ROUTES_ANCHOR = "公开 Job 路由已装配"
ROUTE_PATTERN = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1/nexus/jobs[^`\s]*)`")
# The phrase that was wrong in both directions before #823: a planner that is
# assembled must never be described as missing, in a document or in the matrix.
STALE_PLANNER_PHRASES = (
    "生产 planner 未装配",
    "生产 planner 尚未装配",
    "生产派发 planner 尚未装配",
    "planner 未装配",
    "planner 尚未装配",
)


@dataclass(frozen=True)
class Claim:
    """One documented statement plus the code line it must keep citing."""

    name: str
    doc: str
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    anchor: tuple[str, str] | None = None

    def check(self, root: Path) -> list[str]:
        """Return every reason this claim no longer holds."""
        text = _read(root, self.doc)
        problems = [
            f"{self.name}: {self.doc} no longer says {phrase!r}"
            for phrase in self.required
            if phrase not in text
        ]
        problems.extend(
            f"{self.name}: {self.doc} still says {phrase!r}"
            for phrase in self.forbidden
            if phrase in text
        )
        if self.anchor is not None:
            problems.extend(self._check_anchor(root, text))
        return problems

    def _check_anchor(self, root: Path, text: str) -> list[str]:
        """Require a citation of the anchor path whose line still holds the marker."""
        assert self.anchor is not None
        path, marker = self.anchor
        cited = _cited_lines(text, path)
        if not cited:
            return [f"{self.name}: {self.doc} must cite {path}:<line> near {marker!r}"]
        lines = _read(root, path).splitlines()
        if any(1 <= number <= len(lines) and marker in lines[number - 1] for number in cited):
            return []
        return [
            f"{self.name}: {self.doc} cites {path}:{number} but none of those lines "
            f"contains {marker!r}; update the citation or restore the code"
            for number in cited[:1]
        ]


CLAIMS = (
    Claim(
        name="sync-asr-readme",
        doc="deploy/README.md",
        required=("assembled but not exercised in this deployment", "no public `200` has been run"),
        forbidden=("missing production terminal-state", "still blocked by the missing"),
        anchor=(FILE_WORKER, "_completion.complete("),
    ),
    Claim(
        name="sync-asr-readme-completion",
        doc="deploy/README.md",
        required=("record_stop(SUCCEEDED)",),
        anchor=(COMPLETION, "record_stop("),
    ),
    Claim(
        name="sync-asr-compose",
        doc="docs/operations/compose-minimal.md",
        required=(
            "已装配、已实现；本部署无公开 `200` 证据",
            "本部署未验证的只有最后一步",
            "POST /v1/audio/transcriptions",
        ),
        forbidden=("**本部署未验证**", "本部署已验证"),
        anchor=(FILE_WORKER, "_completion.complete("),
    ),
    Claim(
        name="sync-asr-compose-completion",
        doc="docs/operations/compose-minimal.md",
        required=("record_stop(SUCCEEDED)",),
        anchor=(COMPLETION, "record_stop("),
    ),
    Claim(
        name="public-job-routes-prd",
        doc=PRD,
        required=(DOCUMENTED_ROUTES_ANCHOR, "尚未在参考部署上实际调用"),
        forbidden=("仅装配创建", "取消、重试和 SSE 仍须接线验收"),
        anchor=(JOBS_ROUTER, 'operation_id="createJob"'),
    ),
    Claim(
        name="public-job-routes-prd-assembly",
        doc=PRD,
        required=(APP,),
        anchor=(APP, "create_job_router("),
    ),
    Claim(
        name="public-job-routes-speech-api",
        doc=SPEECH_API,
        required=("公开 Job 路由已装配创建、列表、读取、取消、重试与 SSE 事件流",),
        forbidden=("仅装配创建",),
        anchor=(JOBS_ROUTER, 'operation_id="createJob"'),
    ),
    Claim(
        name="planner-readme",
        doc="docs/README.md",
        required=("生产 planner 已随该运行时装配",),
        forbidden=STALE_PLANNER_PHRASES,
        anchor=(DISPATCH_RUNTIME, "StoredCommandPlanner("),
    ),
    Claim(
        name="planner-speech-api",
        doc=SPEECH_API,
        required=("生产派发 planner 已在派发运行时装配",),
        forbidden=STALE_PLANNER_PHRASES,
        anchor=(DISPATCH_RUNTIME, "StoredCommandPlanner("),
    ),
    Claim(
        name="planner-runtime-governance",
        doc="docs/architecture/runtime-governance.md",
        required=("生产 Job planner 已在派发运行时装配",),
        forbidden=STALE_PLANNER_PHRASES,
        anchor=(DISPATCH_RUNTIME, "StoredCommandPlanner("),
    ),
    Claim(
        name="planner-product-architecture",
        doc="docs/speechnexus-product-architecture.md",
        required=("生产 planner 均已装配",),
        forbidden=STALE_PLANNER_PHRASES,
        anchor=(DISPATCH_RUNTIME, "StoredCommandPlanner("),
    ),
)

PREFIX_PATTERN = re.compile(r'^PREFIX = "([^"]+)"$', re.MULTILINE)
DECORATOR_PATTERN = re.compile(r"@router\.(get|post|put|patch|delete)\(([^)]*)\)")
MOUNT_PATTERN = re.compile(r"app\.include_router\(\s*create_job_router\(")


def _read(root: Path, path: str) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"cannot read {path}: {error}") from error


def _cited_lines(text: str, path: str) -> list[int]:
    """Line numbers this document cites for one repository path."""
    return [int(match.group(1)) for match in re.finditer(re.escape(path) + r":(\d+)", text)]


def _route_path(expression: str, prefix: str) -> str:
    """Evaluate one registered path expression without importing the module."""
    argument = expression.split(",", 1)[0].strip()
    pieces: list[str] = []
    for part in argument.split("+"):
        token = part.strip()
        if token == "PREFIX":
            pieces.append(prefix)
        elif len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            pieces.append(token[1:-1])
        else:
            raise ValueError(f"jobs.py registers a route path this guard cannot read: {argument!r}")
    return "".join(pieces)


def job_routes(root: Path) -> set[tuple[str, str]]:
    """Every (method, path) the public Job router registers."""
    text = _read(root, JOBS_ROUTER)
    match = PREFIX_PATTERN.search(text)
    if match is None:
        raise ValueError("jobs.py no longer defines a literal PREFIX")
    prefix = match.group(1)
    routes = {
        (method.upper(), _route_path(expression, prefix))
        for method, expression in DECORATOR_PATTERN.findall(text)
    }
    if not routes:
        raise ValueError("jobs.py no longer registers routes through `router`")
    return routes


def documented_job_routes(root: Path) -> set[tuple[str, str]]:
    """Every (method, path) docs/prd.md lists for the public Job surface."""
    routes: set[tuple[str, str]] = set()
    for line in _read(root, PRD).splitlines():
        if DOCUMENTED_ROUTES_ANCHOR not in line:
            continue
        routes.update(ROUTE_PATTERN.findall(line))
    return routes


def job_router_mounted(root: Path) -> bool:
    """Whether app.py mounts the public Job router itself."""
    return MOUNT_PATTERN.search(_read(root, APP)) is not None


def _route_set_problems(root: Path) -> list[str]:
    code = job_routes(root)
    documented = documented_job_routes(root)
    problems: list[str] = []
    for method, path in sorted(code - documented):
        problems.append(
            f"public-job-routes: {JOBS_ROUTER} registers {method} {path} but {PRD} does not list it"
        )
    for method, path in sorted(documented - code):
        problems.append(
            f"public-job-routes: {PRD} lists {method} {path} but {JOBS_ROUTER} does not register it"
        )
    if not job_router_mounted(root):
        problems.append(f"public-job-routes: {APP} no longer mounts create_job_router")
    return problems


def _matrix_problems(root: Path) -> list[str]:
    """The matrix (and its rendered view) must not resurrect the stale planner claim."""
    problems: list[str] = []
    for path in (MANIFEST, MATRIX):
        text = _read(root, path)
        for phrase in STALE_PLANNER_PHRASES:
            if phrase in text:
                problems.append(f"planner-matrix: {path} still says {phrase!r}")
    if "生产 planner 与结果发布器已装配" not in _read(root, MANIFEST):
        problems.append(
            f"planner-matrix: {MANIFEST} must state that the production planner "
            "and result publisher are assembled"
        )
    return problems


def check(root: Path) -> list[str]:
    """Every problem found under one checkout; an empty list means the guard is green."""
    problems: list[str] = []
    for claim in CLAIMS:
        problems.extend(claim.check(root))
    problems.extend(_route_set_problems(root))
    problems.extend(_matrix_problems(root))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="checkout to check")
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args()
    try:
        problems = check(args.root)
    except (SyntaxError, ValueError) as error:
        print(f"FAIL doc-statements: {error}")
        return 1
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        print(f"RESULT FAIL {len(problems)} problem(s)")
        return 1
    if not args.quiet:
        routes = len(job_routes(args.root))
        print(f"ok   {routes} public Job routes documented as registered")
        print(f"ok   {len(CLAIMS)} capability statements match the code they cite")
        print("RESULT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
