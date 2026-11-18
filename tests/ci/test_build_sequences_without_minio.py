"""Keep every documented and scripted build sequence out of `minio`'s Dockerfile.

`#900` measured the failure this guards: `deploy/images/minio.Dockerfile` fetches a
LICENSE from `raw.githubusercontent.com` while building, so a host without that
egress dies at `target minio: failed to solve`. Compose reaches that target through
the dependency graph — `api` and `gateway` pull in `minio-init`, which pulls in
`minio` — so `up -d --build api gateway` fails as a whole. `#903` fixed
`docs/operations/compose-minimal.md`; `#905` is the same correction for
`docs/operations/async-job-runbook.md` and `scripts/dev-env/reference-acceptance.py`.

What is checked, and where each half gets its evidence from:

* every compose command a reader can copy out of an active document (a fenced block
  in `README.md`, `README.en.md` or the async-job runbook) carries no `--build`; the
  only `build` it offers names its services and none of them is `minio`;
* `reference-acceptance.py` reaches a build through an argv that names the service
  it builds. Two independent checks keep that honest: an AST scan pins which
  functions may mention `--build` or the `build` subcommand at all, and every argv
  those functions produce is planned through a graph derived from
  `deploy/compose.yaml`'s `depends_on` / `network_mode: service:` edges — the
  expected built set is registered per argv, so dropping one is red instead of
  "nothing to check";
* the graph model is anchored by its own fails-if-vacuous cases: `minio` must be
  reachable from `api`, `gateway` and `job-dispatch-worker`, and unreachable from
  `migrate`. A parser that silently returned no dependencies cannot pass;
* where the compose plugin can plan a build in `--dry-run`, the one form this card
  removed (`up -d --build api gateway`) must still plan `minio` as building — the
  negative control that keeps the criterion itself from going vacuous, and the only
  plan check the `guards` job (`timeout-minutes: 5`) can afford;
* on a developer host, every registered argv is additionally measured through the
  `up` forms that report `Image … Building` in both plugin generations (compose v5
  needs no detour, the v2 plugin has no `build --dry-run` plan), and the measured
  set must equal the graph's prediction. That tier costs one plan per argv, which
  is why it does not run in the 5-minute guards job.
* a host that cannot plan a build still collects both classes and reports them as
  `skipped` with a reason, never as absent: `@unittest.skipUnless` keeps the tests in
  the count so "did not run" cannot look like "passed" (issue #992). The merged
  coverage gate accepts exactly those five nodeids, declared in
  `scripts/dev-env/python-coverage.py:EXPECTED_SKIPS`; an undeclared skip is still a
  failure, and a declared one that stops being collected is a failure too.

Dated records are excluded on purpose and listed in `HISTORICAL_RECORDS`: they keep
the command that was run and its output as measured; rewriting a record to satisfy a
current rule would be a false statement about the past.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import unittest
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "compose.yaml"
ENV_EXAMPLE = ROOT / "deploy" / "compose.env.example"
ENTRY = ROOT / "scripts" / "dev-env" / "reference-acceptance.py"

#: Documents a reader is expected to copy commands from.
ACTIVE_DOCUMENTS = (
    "README.md",
    "README.en.md",
    "docs/operations/async-job-runbook.md",
)

#: Dated records that keep the exact commands and outputs measured at that time.
#: They are evidence of what happened, not instructions.
HISTORICAL_RECORDS = {
    "docs/operations/compose-minimal-from-zero.md": (
        "2026-11-16 两次从空库实跑的执行记录，命令与输出按原样保留"
    ),
}

RUNBOOK = "docs/operations/async-job-runbook.md"
RUNBOOK_RECORD_HEADING = "## 14. 本机验证记录与未验证项"

#: The one build sequence every active document points at instead of restating it.
SINGLE_SOURCE = "compose-minimal.md#0-构建前提"

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
COMPOSE_INVOCATION = re.compile(
    r"^(?:\$\s+)?(?:sudo\s+)?(?:docker\s+compose|\"\$\{COMPOSE\[@\]\}\")\s+"
)
BUILDING_IMAGE = re.compile(r"^\s*Image\s+(?P<image>\S+)\s+Building\s*$")

#: Flags whose next token is a value, not a service name or subcommand.
VALUE_FLAGS = frozenset(
    {
        "--ansi",
        "--env",
        "--env-file",
        "--entrypoint",
        "--file",
        "--label",
        "--name",
        "--parallel",
        "--profile",
        "--progress",
        "--project-directory",
        "--project-name",
        "--user",
        "--volume",
        "--workdir",
        "-e",
        "-f",
        "-l",
        "-p",
        "-u",
        "-v",
        "-w",
    }
)


# ---------------------------------------------------------------------------
# deploy/compose.yaml: the dependency graph Compose builds through
# ---------------------------------------------------------------------------

SERVICE_KEY = re.compile(r"^  ([a-z][a-z0-9_-]*):\s*$")
DEPENDENCY_KEY = re.compile(r"^      ([a-z][a-z0-9_-]*):\s*$")
NETWORK_MODE = re.compile(r'^    network_mode:\s*"?service:([a-z][a-z0-9_-]*)"?\s*$')


@dataclass(frozen=True)
class ComposeService:
    """One service block: whether it has an image to build and what it pulls in."""

    has_build: bool
    depends: tuple[str, ...]


def compose_graph(text: str) -> dict[str, ComposeService]:
    """Parse the services block into ``service -> (has_build, dependencies)``.

    Only two edges matter for Compose's build closure: ``depends_on`` and
    ``network_mode: service:<name>`` (the latter also starts that service). The
    parser is deliberately narrow — a line shape it does not recognise is skipped —
    so the anti-vacuity cases below pin that it still reads the edges that matter.
    """
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.rstrip() == "services:")
    except StopIteration as error:  # pragma: no cover - the file always has services
        raise AssertionError("deploy/compose.yaml has no `services:` block") from error
    services: dict[str, ComposeService] = {}
    current: str | None = None
    has_build = False
    depends: list[str] = []
    in_depends_on = False

    def flush() -> None:
        nonlocal current, has_build, depends, in_depends_on
        if current is not None:
            services[current] = ComposeService(has_build, tuple(depends))
        current, has_build, depends, in_depends_on = None, False, [], False

    for line in lines[start + 1 :]:
        if line and not line.startswith(" ") and not line.startswith("#"):
            break  # the next top-level key ends the services block
        service = SERVICE_KEY.match(line)
        if service is not None:
            flush()
            current = service.group(1)
            continue
        if current is None:
            continue
        if in_depends_on and DEPENDENCY_KEY.match(line):
            depends.append(DEPENDENCY_KEY.match(line).group(1))  # type: ignore[union-attr]
            continue
        if line.strip() == "depends_on:":
            in_depends_on = True
            continue
        if line.startswith("    ") and not line.startswith("      "):
            in_depends_on = False  # a new service-level key, not a dependency entry
            if line.strip().startswith("build:"):
                has_build = True
            network = NETWORK_MODE.match(line)
            if network is not None:
                depends.append(network.group(1))
    flush()
    return services


def started_services(services: Sequence[str], graph: Mapping[str, ComposeService]) -> set[str]:
    """Every service Compose starts for ``services``, following both edge kinds."""
    seen: set[str] = set()
    stack = list(services)
    while stack:
        service = stack.pop()
        if service in seen:
            continue
        seen.add(service)
        node = graph.get(service)
        if node is not None:
            stack.extend(node.depends)
    return seen


def built_images(services: Sequence[str], graph: Mapping[str, ComposeService]) -> set[str]:
    """The subset of ``started_services`` that actually has an image to build."""
    return {name for name in started_services(services, graph) if graph[name].has_build}


# ---------------------------------------------------------------------------
# compose argv -> what Compose would build
# ---------------------------------------------------------------------------


def compose_parts(argv: Sequence[str]) -> tuple[list[str], str, list[str]]:
    """Split ``docker compose <flags> <subcommand> ...`` into its three parts."""
    tokens = list(argv)
    if tokens[:2] != ["docker", "compose"]:
        raise AssertionError(f"not a docker compose invocation: {argv}")
    index = 2
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in VALUE_FLAGS else 1
    if index >= len(tokens):
        raise AssertionError(f"compose invocation with no subcommand: {argv}")
    return tokens[2:index], tokens[index], tokens[index + 1 :]


def named_services(tokens: Sequence[str]) -> list[str]:
    """Non-flag tokens, skipping the values of value-taking flags."""
    services: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in VALUE_FLAGS:
            index += 2
            continue
        if not token.startswith("-"):
            services.append(token)
        index += 1
    return services


def built_services(
    argv: Sequence[str], graph: Mapping[str, ComposeService]
) -> tuple[str, tuple[str, ...]]:
    """``(subcommand, service names whose images this argv would build)``.

    Compose semantics, measured against the real plugin in `ComposePlanTest`:

    * ``build A B`` builds exactly A and B;
    * ``up --build`` builds everything it starts, i.e. the closure of its arguments;
    * ``run --build S`` also builds S's dependency closure, unless ``--no-deps`` is
      present, in which case only S;
    * the same commands without ``--build`` build nothing here: they reuse an image,
      which is exactly why an explicit build precedes them.
    """
    _flags, subcommand, rest = compose_parts(argv)
    services = named_services(rest)
    if subcommand == "build":
        return subcommand, tuple(services)
    if subcommand == "up":
        if "--build" not in rest:
            return subcommand, ()
        targets = services or sorted(graph)
        return subcommand, tuple(sorted(built_images(targets, graph)))
    if subcommand == "run":
        if "--build" not in rest or not services:
            return subcommand, ()
        service = services[0]
        if "--no-deps" in rest:
            return subcommand, (service,)
        return subcommand, tuple(sorted(built_images([service], graph)))
    return subcommand, ()


# ---------------------------------------------------------------------------
# Documents: the commands a reader can copy
# ---------------------------------------------------------------------------


def fenced_lines(path: Path) -> list[tuple[int, str]]:
    """Every non-empty line inside a fenced block as ``(line number, stripped line)``."""
    collected: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence and line.strip():
            collected.append((number, line.strip()))
    return collected


def invocation_words(command: str) -> list[str]:
    """Split one command into words, dropping the shell prompt and wrapper."""
    words = command.replace("\\", " ").split()
    if words and words[0] == "$":
        words = words[1:]
    if words and words[0] == "sudo":
        words = words[1:]
    if words and words[0] == '"${COMPOSE[@]}"':
        words = ["docker", "compose", *words[1:]]
    return words


def documented_compose_commands(path: Path) -> Iterator[tuple[int, list[str]]]:
    """Compose invocations in fences, continuations joined, as ``(line, argv)``."""
    buffer: list[str] = []
    start = 0
    for number, line in fenced_lines(path):
        if not buffer:
            if not COMPOSE_INVOCATION.match(line):
                continue
            start = number
        buffer.append(line)
        if not line.endswith("\\"):
            yield start, invocation_words(" ".join(buffer))
            buffer = []
    if buffer:
        yield start, invocation_words(" ".join(buffer))


def scan_document(path: Path) -> list[tuple[int, list[str]]]:
    """Compose commands of one document, skipping the ones this parser cannot read."""
    commands: list[tuple[int, list[str]]] = []
    for number, argv in documented_compose_commands(path):
        try:
            compose_parts(argv)
        except AssertionError:
            continue
        commands.append((number, argv))
    return commands


# ---------------------------------------------------------------------------
# reference-acceptance.py: the argv builders the entry really uses
# ---------------------------------------------------------------------------


def entry_module() -> Any:
    spec = importlib.util.spec_from_file_location("reference_acceptance_build_guard", ENTRY)
    assert spec is not None and spec.loader is not None
    loaded: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


HARNESS: Any = entry_module()


class UnusedRunner:
    """Builders only read ``ctx.compose_argv``; no command may run here."""

    def run(self, *_args: object, **_kwargs: object) -> Any:  # pragma: no cover
        raise AssertionError("the build-sequence guard must not execute a command")


def entry_context() -> Any:
    return HARNESS.Context(
        env={},
        env_file=ENV_EXAMPLE,
        compose_file=COMPOSE_FILE,
        project="sn-build-guard",
        runner=UnusedRunner(),
        dry_run=True,
        gate_mode="compose",
        repo_root=ROOT,
    )


def gate_direct_config() -> dict[str, object]:
    """The compose-config shape `gate_direct_argv` reads, without a Docker run."""
    return {
        "services": {
            "job-dispatch-worker": {
                "environment": {
                    "SPEECHNEXUS_DATABASE_URL": "postgresql://sn_job_dispatch:pw@postgres:5432/db"
                },
                "volumes": [
                    {
                        "source": str(ROOT / "deploy/job-startup-gate.py"),
                        "target": "/opt/speechnexus/job-startup-gate.py",
                    },
                    {
                        "source": str(ROOT / "deploy/private"),
                        "target": "/run/speechnexus-private",
                    },
                ],
            }
        }
    }


def entry_invocations() -> dict[str, tuple[str, ...]]:
    """Every build-bearing argv the entry ships, keyed by its builder."""
    context = entry_context()
    direct, _child = HARNESS.gate_direct_argv(context, gate_direct_config())
    return {
        "migrate_argv": tuple(HARNESS.migrate_argv(context)),
        "stack_build_argv": tuple(HARNESS.stack_build_argv(context)),
        "stack_up_argv": tuple(HARNESS.stack_up_argv(context)),
        "gate_compose_build_argv": tuple(HARNESS.gate_compose_build_argv(context)),
        "gate_compose_argv": tuple(HARNESS.gate_compose_argv(context)),
        "gate_direct_argv": tuple(direct),
    }


#: Which service each builder targets. A renamed service, a dropped builder or a
#: build moved into a different function is red here.
EXPECTED_ENTRY_INVOCATIONS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "migrate_argv": ("run", ("migrate",)),
    "stack_build_argv": ("build", ("api", "gateway")),
    "stack_up_argv": ("up", ("api", "gateway")),
    "gate_compose_build_argv": ("build", ("job-dispatch-worker",)),
    "gate_compose_argv": ("run", ("job-dispatch-worker",)),
    "gate_direct_argv": ("run", ("minio-init",)),
}

#: What each builder is expected to build, pinned. The graph-derived value must
#: agree: giving `migrate` a dependency with a build section would move this.
EXPECTED_ENTRY_BUILDS: Mapping[str, tuple[str, ...]] = {
    "migrate_argv": ("migrate",),
    "stack_build_argv": ("api", "gateway"),
    "stack_up_argv": (),
    "gate_compose_build_argv": ("job-dispatch-worker",),
    "gate_compose_argv": (),
    "gate_direct_argv": ("minio-init",),
}

#: Functions allowed to mention `--build`; every other function must not.
BUILD_FLAG_FUNCTIONS = frozenset({"migrate_argv", "gate_direct_argv"})
#: Functions allowed to run the `build` subcommand.
BUILD_SUBCOMMAND_FUNCTIONS = frozenset({"stack_build_argv", "gate_compose_build_argv"})


def target_of(argv: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """``(subcommand, service(s) the argv acts on)`` for the registry comparison."""
    _flags, subcommand, rest = compose_parts(argv)
    services = named_services(rest)
    if subcommand in {"build", "up"}:
        return subcommand, tuple(services)
    if subcommand == "run":
        return subcommand, tuple(services[:1])
    return subcommand, tuple(services)


def _own_nodes(function: ast.AST) -> list[ast.AST]:
    """Nodes of one function body; nested defs are audited as their own function."""
    collected: list[ast.AST] = []
    stack: list[ast.AST] = list(function.body)  # type: ignore[attr-defined]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        collected.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return collected


def functions_containing(needle: str) -> set[str]:
    """Functions in the entry whose own body contains ``needle`` as a constant."""
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(inner, ast.Constant) and inner.value == needle for inner in _own_nodes(node)
        ):
            found.add(node.name)
    return found


class DocumentedBuildSequenceTest(unittest.TestCase):
    """No active document offers a command that can build `minio`."""

    def setUp(self) -> None:
        self.graph = compose_graph(COMPOSE_FILE.read_text(encoding="utf-8"))

    def test_no_fenced_command_carries_build(self) -> None:
        for relative in ACTIVE_DOCUMENTS:
            path = ROOT / relative
            for number, line in fenced_lines(path):
                with self.subTest(document=relative, line=number):
                    self.assertNotIn(
                        "--build",
                        line,
                        f"{relative}:{number} offers `--build` for copy-paste; build once "
                        "with an explicit `build <services>` and keep `up` bare",
                    )

    def test_every_documented_build_names_its_services_and_skips_minio(self) -> None:
        builds = []
        for relative in ACTIVE_DOCUMENTS:
            for number, argv in scan_document(ROOT / relative):
                if compose_parts(argv)[1] != "build":
                    continue
                services = named_services(compose_parts(argv)[2])
                builds.append((relative, number, services))
        self.assertTrue(builds, "no active document documents an explicit build any more")
        for relative, number, services in builds:
            with self.subTest(document=relative, line=number):
                self.assertTrue(
                    services,
                    f"{relative}:{number} runs a bare `build`, which builds every service "
                    "including minio",
                )
                self.assertNotIn(
                    "minio",
                    services,
                    f"{relative}:{number} builds minio; without raw.githubusercontent.com "
                    "that target fails and takes the command down",
                )

    def test_the_asset_inspector_is_built_before_it_is_started(self) -> None:
        commands = [
            (compose_parts(argv)[1], named_services(compose_parts(argv)[2]))
            for _number, argv in scan_document(ROOT / RUNBOOK)
        ]
        self.assertIn(
            ("build", ["asset-inspector"]),
            commands,
            "§10.2 must build the inspector explicitly instead of `up -d --build`",
        )
        self.assertIn(("up", ["asset-inspector"]), commands)

    def test_the_documents_point_at_the_single_build_sequence(self) -> None:
        for relative in ACTIVE_DOCUMENTS:
            with self.subTest(document=relative):
                self.assertIn(
                    SINGLE_SOURCE,
                    (ROOT / relative).read_text(encoding="utf-8"),
                    f"{relative} restates a build sequence without pointing at "
                    "compose-minimal §0, which is how the two copies drifted apart (#905)",
                )

    def test_runbook_compose_invocations_with_build_live_in_its_dated_records(self) -> None:
        """Prose may explain `--build`; a full compose invocation outside §14 is a call.

        The runbook keeps dated records after `RUNBOOK_RECORD_HEADING` that quote the
        commands actually run, `--build` included. Those stay as measured. What must
        not appear is a *new* `docker compose … --build` anywhere else, because that
        is a command a reader copies rather than a past result.
        """
        text = (ROOT / RUNBOOK).read_text(encoding="utf-8")
        self.assertIn(RUNBOOK_RECORD_HEADING, text)
        record_start = text.index(RUNBOOK_RECORD_HEADING)
        offset = 0
        for number, line in enumerate(text.splitlines(keepends=True), start=1):
            if "docker compose" in line and "--build" in line:
                with self.subTest(line=number):
                    self.assertGreater(
                        offset,
                        record_start,
                        f"{RUNBOOK}:{number} is a compose invocation carrying `--build` "
                        "outside §14's dated records",
                    )
            offset += len(line)

    def test_no_unregistered_document_offers_a_build(self) -> None:
        """A new document with a build command must be registered, not silently exempt."""
        self.graph = compose_graph(COMPOSE_FILE.read_text(encoding="utf-8"))
        offenders: dict[str, str] = {}
        paths = sorted(ROOT.glob("docs/**/*.md")) + sorted(ROOT.glob("README*.md"))
        paths += sorted(ROOT.glob("deploy/**/*.md"))
        for path in paths:
            relative = str(path.relative_to(ROOT))
            if relative in ACTIVE_DOCUMENTS or relative in HISTORICAL_RECORDS:
                continue
            for number, argv in scan_document(path):
                _flags, subcommand, rest = compose_parts(argv)
                services = named_services(rest)
                if "--build" in rest:
                    offenders.setdefault(relative, f"line {number} puts --build on a command")
                if subcommand == "build" and not services:
                    offenders.setdefault(relative, f"line {number} runs a bare build")
                if subcommand == "build" and "minio" in services:
                    offenders.setdefault(relative, f"line {number} builds minio")
                if "minio" in built_services(argv, self.graph)[1]:
                    offenders.setdefault(relative, f"line {number} reaches minio")
        self.assertEqual(
            offenders,
            {},
            "these documents offer a build command without being an active document the "
            "guard scans or a registered dated record: "
            + "; ".join(f"{name} ({reason})" for name, reason in sorted(offenders.items())),
        )


class ComposeGraphTest(unittest.TestCase):
    """The graph and the argv model fail loudly instead of going vacuous."""

    def setUp(self) -> None:
        self.graph = compose_graph(COMPOSE_FILE.read_text(encoding="utf-8"))

    def test_the_graph_reads_the_services_it_must(self) -> None:
        for service in ("api", "gateway", "minio", "minio-init", "migrate", "job-dispatch-worker"):
            with self.subTest(service=service):
                self.assertIn(service, self.graph)

    def test_minio_is_reachable_exactly_where_compose_reaches_it(self) -> None:
        for service in ("api", "gateway", "minio-init", "job-dispatch-worker"):
            with self.subTest(service=service):
                self.assertIn(
                    "minio",
                    started_services([service], self.graph),
                    f"{service} must reach minio through the compose graph; a parser that "
                    "lost that edge would make every later assertion vacuous",
                )
        self.assertNotIn("minio", started_services(["migrate"], self.graph))

    def test_postgres_has_no_image_to_build(self) -> None:
        self.assertFalse(self.graph["postgres"].has_build)
        self.assertIn("postgres", started_services(["migrate"], self.graph))
        self.assertNotIn("postgres", built_images(["migrate"], self.graph))

    def test_build_builds_only_what_it_names(self) -> None:
        build = ["docker", "compose", "--env-file", "env", "build", "api", "gateway"]
        self.assertEqual(built_services(build, self.graph), ("build", ("api", "gateway")))
        up = ["docker", "compose", "--env-file", "env", "up", "-d", "--wait", "api", "gateway"]
        self.assertEqual(built_services(up, self.graph), ("up", ()))

    def test_the_model_reproduces_the_two_removed_forms(self) -> None:
        old_up = ["docker", "compose", "up", "-d", "--build", "--wait", "api", "gateway"]
        self.assertIn("minio", built_services(old_up, self.graph)[1])
        old_run = [
            "docker",
            "compose",
            "--profile",
            "job-dispatch",
            "run",
            "--build",
            "--rm",
            "-T",
            "job-dispatch-worker",
            "true",
        ]
        self.assertIn(
            "minio",
            built_services(old_run, self.graph)[1],
            "`run --build` on a service with network_mode: service:gateway reaches minio",
        )
        index = old_run.index("--build") + 1
        no_deps_run = [*old_run[:index], "--no-deps", *old_run[index:]]
        self.assertEqual(built_services(no_deps_run, self.graph)[1], ("job-dispatch-worker",))


class EntryBuildSequenceTest(unittest.TestCase):
    """Every build the entry runs names its service, and none of them is minio."""

    def setUp(self) -> None:
        self.graph = compose_graph(COMPOSE_FILE.read_text(encoding="utf-8"))
        self.invocations = entry_invocations()

    def test_the_registry_lists_the_argv_the_entry_ships(self) -> None:
        entered = {builder: target_of(argv) for builder, argv in self.invocations.items()}
        self.assertEqual(
            entered,
            dict(EXPECTED_ENTRY_INVOCATIONS),
            "an argv builder changed shape; update the plan table and say why",
        )

    def test_the_graph_agrees_with_the_registered_build_sets(self) -> None:
        for builder, argv in self.invocations.items():
            with self.subTest(builder=builder):
                self.assertEqual(
                    built_services(argv, self.graph)[1], EXPECTED_ENTRY_BUILDS[builder]
                )

    def test_no_entry_build_sequence_reaches_minio(self) -> None:
        for builder, argv in self.invocations.items():
            with self.subTest(builder=builder):
                self.assertNotIn(
                    "minio",
                    built_services(argv, self.graph)[1],
                    f"{builder} builds minio; the reference acceptance must not depend on "
                    "raw.githubusercontent.com at build time",
                )

    def test_only_registered_functions_mention_build(self) -> None:
        self.assertEqual(
            functions_containing("--build"),
            set(BUILD_FLAG_FUNCTIONS),
            "a function outside this registry mentions --build: every `--build` must be "
            "reviewed for the dependency closure it drags in",
        )
        self.assertEqual(
            functions_containing("build"),
            set(BUILD_SUBCOMMAND_FUNCTIONS),
            "a function outside this registry runs the `build` subcommand",
        )

    def test_no_up_anywhere_in_the_entry_carries_build(self) -> None:
        for builder, argv in self.invocations.items():
            _flags, subcommand, rest = compose_parts(argv)
            if subcommand != "up":
                continue
            with self.subTest(builder=builder):
                self.assertNotIn("--build", rest, "up --build also builds minio (#900/#905)")


# ---------------------------------------------------------------------------
# Real plugin: `docker compose --dry-run`, where it can plan a build
# ---------------------------------------------------------------------------
#
# Compose plans a build in `--dry-run` only for some commands and versions: v5
# reports `Image … Building` for `build`, while the v2 plugin on GitHub runners
# reports nothing for it and only for `up --build`. Every build set is therefore
# measured through the `up` forms below, which both report, and a single service
# at a time, so the same measurement exists in every environment.

#: `--dry-run` forms whose plan carries the build set of one service. The first
#: non-empty plan wins; the second is the other plugin generation's shape.
SINGLE_SERVICE_PLANS = (
    ("up", "-d", "--no-deps", "--build"),
    ("build",),
)
CLOSURE_SERVICE_PLANS = (
    ("up", "-d", "--build"),
    ("up", "-d", "--build", "--wait"),
)


def explicit_build(argv: Sequence[str]) -> bool:
    """Whether this argv asks Compose to build (as opposed to reusing an image)."""
    _flags, subcommand, rest = compose_parts(argv)
    return subcommand == "build" or "--build" in rest


def docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - host dependent
        return False
    return probe.returncode == 0


#: Global flags that change the plan; `--env-file`/`-f` are replaced by the
#: repository files so a documented path (`deploy/compose.env`) is not required.
PLAN_FLAGS = frozenset({"--profile", "-p", "--project-name"})


def canonical_flags(flags: Sequence[str]) -> list[str]:
    """Keep only the flags that change which services exist, with their values."""
    kept: list[str] = []
    index = 0
    while index < len(flags):
        if flags[index] in PLAN_FLAGS and index + 1 < len(flags):
            kept.extend(flags[index : index + 2])
            index += 2
            continue
        index += 1
    return kept


def compose_project() -> str:
    """The ``name:`` at the top of `deploy/compose.yaml`; Compose prefixes images with it."""
    for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return ""


def project_prefixes(flags: Sequence[str]) -> list[str]:
    """Project prefixes Compose may put in front of a generated image name."""
    prefixes: list[str] = []
    for index, flag in enumerate(flags):
        if flag in {"-p", "--project-name"} and index + 1 < len(flags):
            prefixes.append(f"{flags[index + 1]}-")
    project = compose_project()
    if project:
        prefixes.append(f"{project}-")
    return prefixes


@functools.cache
def dry_run_images(plan: tuple[str, ...], flags: tuple[str, ...]) -> tuple[str, ...] | None:
    """Services `docker compose --dry-run <plan>` says it would build."""
    command = [
        "docker",
        "compose",
        *canonical_flags(flags),
        "--env-file",
        str(ENV_EXAMPLE),
        "-f",
        str(COMPOSE_FILE),
        "--dry-run",
        *plan,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - host dependent
        return None
    if result.returncode != 0:
        return None
    prefixes = project_prefixes(flags)
    images: set[str] = set()
    for line in (result.stdout + result.stderr).splitlines():
        match = BUILDING_IMAGE.match(line)
        if match is None:
            continue
        repository = match.group("image").split(":", 1)[0]
        for prefix in prefixes:
            if repository.startswith(prefix):
                repository = repository[len(prefix) :]
                break
        images.add(repository)
    return tuple(sorted(images))


@functools.cache
def planned_service_images(
    service: str, *, closure: bool, flags: tuple[str, ...]
) -> tuple[str, ...] | None:
    """Images planned for building ``service`` (and its closure, when asked)."""
    for form in CLOSURE_SERVICE_PLANS if closure else SINGLE_SERVICE_PLANS:
        images = dry_run_images((*form, service), flags)
        if images:
            return images
    return None


def plan_units(argv: Sequence[str]) -> list[tuple[str, bool]] | None:
    """The ``(service, closure)`` builds whose union is this argv's build set.

    ``build A B`` builds each named image and nothing else; ``run --build`` builds
    its dependency closure unless ``--no-deps`` is present. ``up --build`` is left
    to the real argv, since that is the form the guard rejects.
    """
    _flags, subcommand, rest = compose_parts(argv)
    if not explicit_build(argv) or subcommand == "up":
        return None
    services = named_services(rest)
    if subcommand == "build":
        return [(service, False) for service in services]
    if not services:
        return None
    return [(services[0], "--no-deps" not in rest)]


@functools.cache
def planned_build_set(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    """What Compose would build for ``argv``, or ``None`` when it cannot be planned."""
    flags, subcommand, rest = compose_parts(argv)
    if subcommand == "up":
        if "--build" not in rest:
            return None
        return dry_run_images((subcommand, *rest), tuple(canonical_flags(flags)))
    units = plan_units(argv)
    if units is None:
        return None
    images: set[str] = set()
    for service, closure in units:
        planned = planned_service_images(
            service, closure=closure, flags=tuple(canonical_flags(flags))
        )
        if planned is None:
            return None
        images.update(planned)
    return tuple(sorted(images))


@functools.cache
def plan_tier_available() -> bool:
    """Whether this host can plan a build at all, in both directions.

    Probes the three shapes the tier relies on: one service alone, one service with
    its closure, and the `up --build` form the negative control plans. A host where
    any of them reports nothing does not collect the tier at all — the graph,
    registry and AST checks above stay authoritative there.
    """
    if not docker_compose_available():
        return False
    single = planned_service_images("api", closure=False, flags=())
    closure = planned_service_images("api", closure=True, flags=())
    up = dry_run_images(("up", "-d", "--build", "--wait", "api", "gateway"), ())
    return bool(single) and bool(closure) and bool(up)


def running_in_github_actions() -> bool:
    """Whether this is a CI runner, where `guards` has five minutes for all of tests/ci.

    The full tier costs one `docker compose --dry-run` per measured argv: cheap on a
    developer host, roughly a minute of a cold runner's budget. `ci.yml` is out of
    this card's scope, so the tier stays local. On a runner the tier and the
    negative control both report `skipped` with a reason (measured 2026-11-19: no
    `Building` line from `docker compose --dry-run` there), so the criterion is
    carried by the graph, registry and AST checks above, which need no docker at
    all — not by the negative control, which does not run there either.
    """
    return os.environ.get("GITHUB_ACTIONS") == "true"


@functools.cache
def up_build_plan() -> tuple[str, ...] | None:
    """The plan of `up -d --build --wait api gateway`, the form this card removed."""
    return dry_run_images(("up", "-d", "--build", "--wait", "api", "gateway"), ())


@unittest.skipUnless(
    up_build_plan(),
    "this host planned no build for `up --build`, so the negative control cannot run",
)
class ComposeNegativeControlTest(unittest.TestCase):
    """The criterion still sees the defect: `up --build` must plan `minio`."""

    def test_the_removed_up_build_form_still_plans_minio(self) -> None:
        planned = up_build_plan()
        assert planned is not None
        self.assertIn(
            "minio",
            planned,
            "the removed form must still plan minio, otherwise the criterion this "
            "guard rests on cannot see the failure it removes",
        )


@unittest.skipUnless(
    not running_in_github_actions() and plan_tier_available(),
    "this host cannot plan a build with `docker compose --dry-run`, so the plan tier "
    "does not run here",
)
class ComposePlanTest(unittest.TestCase):
    """The graph model and the real plugin agree, and minio stays out of the plan."""

    def setUp(self) -> None:
        self.graph = compose_graph(COMPOSE_FILE.read_text(encoding="utf-8"))

    def plan(self, builder: str, argv: tuple[str, ...]) -> tuple[str, ...]:
        planned = planned_build_set(argv)
        self.assertIsNotNone(
            planned,
            f"{builder}: this host planned no build for {argv}, so the tier's own "
            "probe and the plan disagree",
        )
        assert planned is not None
        return planned

    def test_the_registered_argv_plans_match_the_graph(self) -> None:
        for builder, argv in entry_invocations().items():
            if not explicit_build(argv):
                continue  # a bare up/run has no build plan to judge
            predicted = built_services(argv, self.graph)[1]
            with self.subTest(builder=builder):
                self.assertEqual(
                    self.plan(builder, argv),
                    predicted,
                    f"{builder}: the graph and the plugin must agree before either is trusted",
                )

    def test_no_planned_argv_builds_minio(self) -> None:
        for builder, argv in entry_invocations().items():
            if not explicit_build(argv):
                continue
            with self.subTest(builder=builder):
                self.assertNotIn("minio", self.plan(builder, argv))

    def test_the_removed_forms_still_plan_minio(self) -> None:
        """Negative control: the criterion really detects the defect it removed."""
        old_up = ("docker", "compose", "up", "-d", "--build", "--wait", "api", "gateway")
        old_run = (
            "docker",
            "compose",
            "--profile",
            "job-dispatch",
            "run",
            "--build",
            "--rm",
            "-T",
            "job-dispatch-worker",
            "true",
        )
        for label, argv in (
            ("up --build api gateway", old_up),
            ("run --build worker", old_run),
        ):
            with self.subTest(form=label):
                self.assertIn(
                    "minio",
                    self.plan(label, argv),
                    "the old form must still plan minio, otherwise this guard's "
                    "criterion cannot see the failure it is meant to remove",
                )

    def test_the_documented_build_plans_only_its_services(self) -> None:
        for relative in ACTIVE_DOCUMENTS:
            for number, argv in scan_document(ROOT / relative):
                if compose_parts(argv)[1] != "build":
                    continue
                label = f"{relative}:{number}"
                with self.subTest(document=relative, line=number):
                    self.assertEqual(
                        self.plan(label, tuple(argv)),
                        tuple(sorted(named_services(compose_parts(argv)[2]))),
                        f"{label} builds more than it names",
                    )


if __name__ == "__main__":
    unittest.main()
