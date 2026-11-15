#!/usr/bin/env python3
"""Verify that the Web image build context is a complete build input set.

`deploy/images/web.Dockerfile` copies a fixed subset of the repository into its
builder stage and then runs `pnpm install --frozen-lockfile` and
`pnpm --filter "@speechnexus/<APP_NAME>" build` inside it. The CI Web job runs the
same commands with the whole repository present, so a `COPY` set that is narrower
than the build's real inputs stays green there and fails only when someone builds
the image. #834 was exactly that: `web/admin/src/profiles/profile-test-fixtures.ts`
and the dev scenario pages imported `tests/fixtures/contracts`, which the image
never copied, so `origin/main` could not build either Web image.

This guard reproduces the builder step from the `COPY` set alone. It materialises a
directory containing exactly the files the Dockerfile copies, installs the locked
dependencies there and builds every Web application the image can build. Anything
the build reads from outside that set — a test fixture, a document, a root config
file — fails here, before an image is built, and without the rest of the repository
to fall back on.

Properties a copy alone cannot show are checked statically:

* every copied path survives `.dockerignore`: copying an ignored path copies
  nothing and fails later, inside the image;
* the repository-level configuration `pnpm install` and `tsc` read is part of the
  `COPY` set;
* the test material the image must not contain is not part of the `COPY` set, and
  no file of an application's production TypeScript program (`tsconfig.build.json`,
  which is what the image compiles) imports it. The dev scenario pages and the
  test-only fixture helpers are outside that program, so their fixture imports stay
  outside the image build graph.

The static checks need neither Node nor Docker, which is what lets the `changes`
job run them; `--build` additionally needs `pnpm` on `PATH` and a populated store.

Usage::

    python3 scripts/dev-env/verify-web-image-context.py
    python3 scripts/dev-env/verify-web-image-context.py --build
    python3 scripts/dev-env/verify-web-image-context.py --build --keep-context /tmp/web-context

Exit status is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DOCKERFILE = "deploy/images/web.Dockerfile"
DOCKERIGNORE = ".dockerignore"
WORKSPACE = "pnpm-workspace.yaml"
BUILD_TSCONFIG = "tsconfig.build.json"
# Files `pnpm install` and `tsc` read from the repository root. No source file
# imports them, so nothing else here would notice them missing from the `COPY` set;
# without them the image builds under a different package-manager and compiler
# policy than CI.
ROOT_CONFIG_INPUTS = (
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.base.json",
    ".npmrc",
)
# Test material. The image must not ship it: the criterion is that the product build
# graph does not reach `tests/**`, not that the copy is small.
# Root inputs whose absence changes policy rather than breaking the build: `pnpm install`
# and `tsc` both succeed without them, so a green build says nothing about them. They are
# still required, because shipping an image that resolves dependencies under different
# engine/save policy than CI is exactly the divergence this check exists to catch.
POLICY_ONLY_INPUTS = frozenset({".npmrc"})
TEST_DATA_ROOTS = ("tests/",)
SKIP_DIRECTORIES = {"node_modules", "dist", "coverage", "test-results", "playwright-report"}
# Import-like constructs that put a module into a build graph. `import.meta.glob` is
# deliberately absent: Vite resolves it, TypeScript does not, so it never pulls a
# module into the program the image compiles. Glob targets are checked separately
# below because Vite does resolve them in a dev build.
IMPORT_PATTERNS = (
    re.compile(r"""\bfrom\s*(["'])"""),
    re.compile(r"""\bimport\s*(["'])"""),
    re.compile(r"""\bimport\s*\(\s*(["'])"""),
    re.compile(r"""\brequire\s*\(\s*(["'])"""),
)
GLOB_PATTERN = re.compile(r"""\bimport\.meta\.glob\s*(?:<[^>()]*>)?\s*\(\s*(["'])""")
ARG_PATTERN = re.compile(r"(?m)^\s*ARG\s+APP_NAME=(\S+)\s*$")


class CheckError(Exception):
    """A named failure; the message is the whole report for that check."""


@dataclass(frozen=True)
class Stage:
    name: str
    copy_sources: tuple[str, ...]


def dockerfile_text(root: Path) -> str:
    path = root / DOCKERFILE
    if not path.is_file():
        raise CheckError(f"{DOCKERFILE} is missing")
    return path.read_text(encoding="utf-8")


def _dockerfile_stages(text: str) -> list[Stage]:
    """Parse `COPY` sources per build stage, in file order.

    Only the literal form is parsed. `COPY --from=...` reads another stage rather
    than the build context and is skipped. A `COPY` with several sources names a
    directory destination (trailing `/` or `.`); everything before it is a source.
    """
    stages: list[Stage] = []
    name = ""
    sources: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        keyword = tokens[0].upper()
        if keyword == "FROM":
            if name or sources:
                stages.append(Stage(name=name, copy_sources=tuple(sources)))
            name = ""
            sources = []
            upper = [token.upper() for token in tokens]
            if "AS" in upper:
                name = tokens[upper.index("AS") + 1]
            continue
        if keyword != "COPY":
            continue
        flags = [token for token in tokens[1:] if token.startswith("--")]
        if any(flag.startswith("--from=") for flag in flags):
            continue
        arguments = [token for token in tokens[1:] if not token.startswith("--")]
        if len(arguments) < 2:
            raise CheckError(
                f"{DOCKERFILE}: cannot parse `{line}`: expected a source and a destination"
            )
        destination = arguments[-1]
        if len(arguments) > 2 and not (destination.endswith("/") or destination == "."):
            raise CheckError(
                f"{DOCKERFILE}: `{line}` has several sources but a destination that is not a "
                "directory; the copy set cannot be derived from it"
            )
        sources.extend(arguments[:-1])
    if name or sources:
        stages.append(Stage(name=name, copy_sources=tuple(sources)))
    return stages


def builder_copy_sources(root: Path) -> tuple[str, ...]:
    """The `COPY` sources of the stage the image builds in."""
    stages = _dockerfile_stages(dockerfile_text(root))
    if not stages:
        raise CheckError(f"{DOCKERFILE}: no build stage found")
    builder = next((stage for stage in stages if stage.name == "build"), stages[0])
    if not builder.copy_sources:
        raise CheckError(f"{DOCKERFILE}: the builder stage copies nothing")
    return builder.copy_sources


def _glob_to_regex(pattern: str) -> str:
    """Translate one tsconfig/`.dockerignore` glob into a regular expression."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return "".join(out)


def _pattern_matches(pattern: str, path: str) -> bool:
    """Docker/tsconfig style match: a pattern also covers everything below it."""
    anchored = pattern.strip()
    while anchored.startswith("./"):
        anchored = anchored[2:]
    anchored = anchored.rstrip("/")
    if not anchored:
        return False
    regex = _glob_to_regex(anchored)
    if re.fullmatch(regex, path) is not None:
        return True
    if not any(char in anchored for char in "*?"):
        # A wildcard-free pattern names a file or a directory; a directory covers
        # its whole subtree, which is how `exclude: ["src/test"]` and `secrets` behave.
        return path.startswith(anchored + "/")
    return re.fullmatch(regex + r"/.*", path) is not None


def dockerignore_rules(root: Path) -> tuple[tuple[bool, str], ...]:
    """`.dockerignore` rules as `(negated, pattern)`, in file order."""
    path = root / DOCKERIGNORE
    if not path.is_file():
        return ()
    rules: list[tuple[bool, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append((negated, line[1:] if negated else line))
    return tuple(rules)


def dockerignore_excluded(path: str, rules: tuple[tuple[bool, str], ...]) -> bool:
    """Whether the build context excludes `path`; the last matching rule wins."""
    excluded = False
    for negated, pattern in rules:
        if _pattern_matches(pattern, path):
            excluded = not negated
    return excluded


def read_json(path: Path) -> dict[str, object]:
    """Read a JSON file that may carry comments or trailing commas, like a tsconfig.

    A plain `json.loads` runs first so an ordinary manifest is parsed exactly. The
    JSONC fallback blanks comments with the same string-aware masker the import scan
    uses: a `/*` inside a glob such as `src/**/*.test.ts` is not a comment, and
    blanking it would silently drop the exclusion the check depends on.
    """
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        stripped = re.sub(r",(\s*[}\]])", r"\1", _mask_comments(text))
        value = json.loads(stripped)
    if not isinstance(value, dict):
        raise CheckError(f"{path}: expected a JSON object")
    return value


def workspace_packages(root: Path) -> list[tuple[str, str]]:
    """`(package name, directory)` for every workspace package with a build script."""
    text = (root / WORKSPACE).read_text(encoding="utf-8")
    patterns: list[str] = []
    collecting = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("packages:"):
            collecting = True
            continue
        if not collecting:
            continue
        if stripped.startswith("- "):
            patterns.append(stripped[2:].strip().strip("'\""))
        elif stripped and not stripped.startswith("#"):
            break
    directories: list[str] = []
    for pattern in patterns:
        if any(char in pattern for char in "*?"):
            directories.extend(
                str(candidate.relative_to(root))
                for candidate in sorted(root.glob(pattern))
                if (candidate / "package.json").is_file()
            )
        elif (root / pattern / "package.json").is_file():
            directories.append(pattern)
    packages: list[tuple[str, str]] = []
    for directory in directories:
        manifest = read_json(root / directory / "package.json")
        name = manifest.get("name")
        scripts = manifest.get("scripts")
        if isinstance(name, str) and isinstance(scripts, dict) and "build" in scripts:
            packages.append((name, directory))
    return packages


def _resolved_tsconfig(root: Path, path: Path) -> dict[str, object]:
    """`include`/`exclude`/`files` of a tsconfig, resolved through its `extends` chain."""
    merged: dict[str, object] = {}
    chain: list[Path] = []
    current: Path | None = path.resolve()
    while current is not None:
        if current in chain:
            raise CheckError(f"{path}: cyclic `extends` chain")
        chain.append(current)
        config = read_json(current)
        for key in ("include", "exclude", "files"):
            if key in config and key not in merged:
                merged[key] = config[key]
        extends = config.get("extends")
        current = None if not isinstance(extends, str) else (current.parent / extends).resolve()
    return merged


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CheckError(f"{label}: expected a list of strings")
    return [item for item in value if isinstance(item, str)]


def production_program(root: Path, directory: str) -> list[str]:
    """Repository-relative files of the application's production TypeScript program.

    The program is the `include`/`exclude` root set of `tsconfig.build.json`. Imports
    TypeScript would follow from those roots are not expanded here; `--build` follows
    them for real, by compiling the same program the image compiles.
    """
    config_path = root / directory / BUILD_TSCONFIG
    if not config_path.is_file():
        raise CheckError(
            f"{directory}/{BUILD_TSCONFIG} is missing: without it `tsc` typechecks the test and "
            "dev sources too, and the image build needs `tests/fixtures/contracts`"
        )
    config = _resolved_tsconfig(root, config_path)
    label = f"{directory}/{BUILD_TSCONFIG}"
    includes = _string_list(config.get("include"), f"{label} include")
    excludes = _string_list(config.get("exclude"), f"{label} exclude")
    if not includes:
        raise CheckError(f"{label} declares no `include`")

    program: list[str] = []
    app_root = root / directory
    for candidate in sorted(app_root.rglob("*")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        relative = candidate.relative_to(root)
        if any(part in SKIP_DIRECTORIES or part.startswith(".") for part in relative.parts):
            continue
        inside = candidate.relative_to(app_root).as_posix()
        if not any(_pattern_matches(pattern, inside) for pattern in includes):
            continue
        if any(_pattern_matches(pattern, inside) for pattern in excludes):
            continue
        program.append(relative.as_posix())
    return program


def _mask_comments(source: str) -> str:
    """Blank comments while keeping string literals and every offset intact.

    Import specifiers are string contents, so only comments are blanked; the
    specifier is then read from the original text at the matched offset.
    """
    out = list(source)
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end < 0 else end
            for offset in range(index, end):
                out[offset] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end < 0 else end + 2
            for offset in range(index, end):
                if out[offset] != "\n":
                    out[offset] = " "
            index = end
            continue
        index += 1
    return "".join(out)


def import_references(source: str) -> list[tuple[int, str]]:
    """`(line, specifier)` for every import-like construct in a source file."""
    masked = _mask_comments(source)
    references: list[tuple[int, str]] = []
    for pattern in (*IMPORT_PATTERNS, GLOB_PATTERN):
        for match in pattern.finditer(masked):
            quote = match.group(1)
            start = match.end()
            end = masked.find(quote, start)
            if end < 0:
                continue
            specifier = source[start:end]
            if specifier:
                references.append((source.count("\n", 0, match.start()) + 1, specifier))
    return references


def _inside(path: str, source: str) -> bool:
    return path == source or path.startswith(source.rstrip("/") + "/")


def _is_test_data(path: str) -> bool:
    normalised = os.path.normpath(path).replace(os.sep, "/")
    return any(
        normalised == entry.rstrip("/") or normalised.startswith(entry) for entry in TEST_DATA_ROOTS
    )


def _default_app_name(text: str) -> str | None:
    match = ARG_PATTERN.search(text)
    return None if match is None else match.group(1).strip("\"'")


def static_checks(root: Path) -> list[str]:
    """Every check that needs neither Node nor Docker. Returns human-readable notes."""
    text = dockerfile_text(root)
    sources = builder_copy_sources(root)

    missing = [source for source in sources if not (root / source).exists()]
    if missing:
        raise CheckError(
            f"{DOCKERFILE}: COPY source(s) do not exist in the repository: {', '.join(missing)}"
        )

    rules = dockerignore_rules(root)
    ignored = [source for source in sources if dockerignore_excluded(source, rules)]
    if ignored:
        raise CheckError(
            f"{DOCKERFILE}: COPY source(s) are excluded by {DOCKERIGNORE} and would copy nothing: "
            f"{', '.join(ignored)}"
        )

    shipped_test_data = [source for source in sources if _is_test_data(source)]
    if shipped_test_data:
        raise CheckError(
            f"{DOCKERFILE}: test material is part of the image build context: "
            f"{', '.join(shipped_test_data)}. The Web image must not contain test data; cut the "
            "product dependency instead of copying it"
        )

    uncopied = [
        entry
        for entry in ROOT_CONFIG_INPUTS
        if (root / entry).exists() and not any(_inside(entry, source) for source in sources)
    ]
    if uncopied:
        required = [entry for entry in uncopied if entry not in POLICY_ONLY_INPUTS]
        policy = [entry for entry in uncopied if entry in POLICY_ONLY_INPUTS]
        if required:
            raise CheckError(
                f"{DOCKERFILE}: the install and the typecheck read these root files, but the "
                f"COPY set omits them: {', '.join(required)}. The image would build "
                "differently from CI or fail to install"
            )
        raise CheckError(
            f"{DOCKERFILE}: the COPY set omits {', '.join(policy)}. The build still succeeds "
            "without it, but the image would install and compile under a different policy "
            "than CI "
            "(engine-strict, save-exact): a green build is not evidence the policy matches"
        )

    packages = workspace_packages(root)
    if not packages:
        raise CheckError(f"{WORKSPACE}: no workspace package declares a `build` script")
    names = {name for name, _ in packages}
    default_app = _default_app_name(text)
    if default_app is not None and f"@speechnexus/{default_app}" not in names:
        raise CheckError(
            f"{DOCKERFILE}: ARG APP_NAME defaults to `{default_app}`, which is not a workspace "
            f"package with a `build` script ({', '.join(sorted(names))})"
        )

    notes: list[str] = []
    for name, directory in packages:
        manifest = read_json(root / directory / "package.json")
        scripts = manifest.get("scripts")
        script = scripts.get("build") if isinstance(scripts, dict) else None
        if not isinstance(script, str) or BUILD_TSCONFIG not in script:
            raise CheckError(
                f"{directory}/package.json: `build` does not typecheck {BUILD_TSCONFIG} "
                f"({script!r}). The image build must compile the production program only, or it "
                "needs the test fixtures again"
            )
        if not any(_inside(directory, source) for source in sources):
            raise CheckError(f"{DOCKERFILE}: no COPY source contains {directory}")
        program = production_program(root, directory)
        if not program:
            raise CheckError(f"{directory}/{BUILD_TSCONFIG}: the production program is empty")
        offenders: list[str] = []
        for relative in program:
            source_text = (root / relative).read_text(encoding="utf-8", errors="surrogateescape")
            for line, specifier in import_references(source_text):
                resolved = os.path.normpath(str(Path(relative).parent / specifier)).replace(
                    os.sep, "/"
                )
                if _is_test_data(specifier) or _is_test_data(resolved):
                    offenders.append(f"{relative}:{line} -> {specifier}")
        if offenders:
            raise CheckError(
                f"{directory}: production sources import test data, which the image does not "
                "contain:\n  " + "\n  ".join(offenders)
            )
        notes.append(f"{name}: {len(program)} production files")
    notes.append(f"{len(sources)} COPY sources, no test data, no ignored path")
    return notes


def materialise_context(root: Path, destination: Path) -> list[str]:
    """Copy exactly the `COPY` set into `destination`, honouring `.dockerignore`."""
    sources = builder_copy_sources(root)
    rules = dockerignore_rules(root)
    copied: list[str] = []
    for source in sources:
        origin = root / source
        if origin.is_dir():
            for candidate in sorted(origin.rglob("*")):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                relative = candidate.relative_to(root).as_posix()
                if any(part in SKIP_DIRECTORIES for part in PurePosixPath(relative).parts):
                    continue
                if dockerignore_excluded(relative, rules):
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target)
                copied.append(relative)
        else:
            target = destination / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
            copied.append(source)
    return copied


def build_context(destination: Path, packages: list[tuple[str, str]]) -> None:
    """Run the image's builder commands inside the materialised context."""
    install = subprocess.run(
        ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"],
        cwd=destination,
        text=True,
        check=False,
    )
    if install.returncode != 0:
        raise CheckError(
            f"`pnpm install --frozen-lockfile` failed inside the {DOCKERFILE} copy set "
            f"(exit {install.returncode}); the copy set is missing an install input"
        )
    for name, _directory in packages:
        build = subprocess.run(
            ["pnpm", "--filter", name, "build"],
            cwd=destination,
            text=True,
            check=False,
        )
        if build.returncode != 0:
            raise CheckError(
                f"`pnpm --filter {name} build` failed inside the {DOCKERFILE} copy set "
                f"(exit {build.returncode}); the copy set is missing a build input"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--build",
        action="store_true",
        help="also install and build every Web application inside the copy set (needs pnpm)",
    )
    parser.add_argument(
        "--keep-context",
        type=Path,
        default=None,
        help="materialise the copy set here instead of a temporary directory",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    try:
        notes = static_checks(root)
        print(f"{DOCKERFILE}:")
        for note in notes:
            print(f"  {note}")
        if args.build:
            packages = workspace_packages(root)
            destination = args.keep_context
            temporary = destination is None
            if destination is None:
                destination = Path(tempfile.mkdtemp(prefix="web-image-context-"))
            else:
                destination.mkdir(parents=True, exist_ok=True)
            try:
                copied = materialise_context(root, destination)
                print(f"materialised {len(copied)} files into {destination}")
                build_context(destination, packages)
                print(f"built {', '.join(name for name, _ in packages)} from the copy set alone")
            finally:
                if temporary:
                    shutil.rmtree(destination, ignore_errors=True)
    except CheckError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
