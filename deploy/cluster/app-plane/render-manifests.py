#!/usr/bin/env python3
"""Render the application-plane manifests from the single version definition point.

The manifests in this directory name no image tag. Every ``image:`` value is a
``${NAME}`` placeholder whose value comes from ``deploy/cluster/versions.env`` --
the same file ``deploy/cluster/init-env.py`` reads for the compute plane. One file
therefore decides the version of the whole assembly, instead of one hand edit per
manifest (issue #1238).

Two modes:

* ``--check`` validates the manifests and writes nothing. It fails when an
  ``image:`` value is not a placeholder, which is what a hardcoded ``:<sha>`` tag
  looks like. Nothing else in the file is interpreted, so the shell variables in
  the PostgreSQL probes and the nginx regex stay untouched.
* default rendering writes the substituted manifests to a working directory and
  prints the apply commands; see ``deploy/cluster/README.md`` for the order.
  ``--select name[,name]`` narrows the output to those documents, which is how a
  single level of the rollout is applied without dragging the rest in.

Substitution is a literal token replacement over a whitelist read from the
manifests themselves. A general ``envsubst`` over these files would also rewrite
``"$POSTGRES_USER"`` and the ``^WEB_(...)$`` regex in ``web.yaml``, producing
YAML that still parses but no longer means anything.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
VERSIONS = ROOT / "deploy/cluster/versions.env"
OUTPUT = ROOT / ".runtime/cluster/app-plane"

# Placeholders are read from the manifests, so this module holds no second list
# of the images the app plane runs.
PLACEHOLDER = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
IMAGE_LINE = re.compile(r"^(\s*image:\s*)(\S.*?)(\s*)$")


def manifests() -> list[Path]:
    return sorted(path for path in HERE.glob("*.yaml"))


def check(path: Path) -> list[str]:
    """Return one message per ``image:`` value that is not a placeholder."""
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = IMAGE_LINE.match(line)
        if match is None:
            continue
        value = match.group(2)
        if PLACEHOLDER.match(value):
            continue
        problems.append(
            f"{path.name}:{number}: image {value!r} is not a ${{NAME}} placeholder; "
            "the tag belongs in deploy/cluster/versions.env"
        )
    return problems


def load_versions() -> dict[str, str]:
    if not VERSIONS.exists():
        raise SystemExit(
            f"missing {VERSIONS}: the app-plane image tags live there "
            "(see deploy/cluster/README.md)"
        )
    values: dict[str, str] = {}
    for raw in VERSIONS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def placeholders(text: str) -> list[str]:
    names: set[str] = set()
    for line in text.splitlines():
        match = IMAGE_LINE.match(line)
        if match is not None:
            token = PLACEHOLDER.match(match.group(2))
            if token is not None:
                names.add(token.group(1))
    return sorted(names)


def render(path: Path, versions: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    missing = [name for name in placeholders(text) if not versions.get(name)]
    if missing:
        raise SystemExit(f"{path.name} needs {', '.join(missing)}; define them in {VERSIONS}")
    for name in placeholders(text):
        text = text.replace("${" + name + "}", versions[name])
    return text


DOCUMENT_SEPARATOR = "\n---\n"
TOP_LEVEL_NAME = re.compile(r"^  name: (\S+)\s*$", re.MULTILINE)


def documents(text: str) -> list[tuple[str, str]]:
    """Split a multi-document manifest into (name, text) pairs."""
    split: list[tuple[str, str]] = []
    for chunk in text.split(DOCUMENT_SEPARATOR):
        if not chunk.strip():
            continue
        names = TOP_LEVEL_NAME.findall(chunk)
        if len(names) != 1:
            raise SystemExit(
                f"cannot identify the object in a document with {len(names)} "
                f"top-level names; keep one object per document"
            )
        split.append((names[0], chunk))
    return split


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    select: list[str] = []
    rest: list[str] = []
    skip = False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--select":
            if index + 1 >= len(argv):
                raise SystemExit("--select needs a comma-separated list of object names")
            select = [name.strip() for name in argv[index + 1].split(",") if name.strip()]
            skip = True
        elif arg.startswith("--select="):
            select = [name.strip() for name in arg.split("=", 1)[1].split(",") if name.strip()]
        else:
            rest.append(arg)
    unknown = [arg for arg in rest if arg != "--check"]
    if unknown:
        raise SystemExit(f"unknown arguments: {' '.join(unknown)}")

    problems: list[str] = []
    for path in manifests():
        problems.extend(check(path))
    if problems:
        for problem in problems:
            sys.stderr.write(problem + "\n")
        raise SystemExit(
            "application-plane manifests must not carry image tags; "
            "put every tag in deploy/cluster/versions.env"
        )
    if check_only:
        print(f"checked {len(manifests())} manifests: every image is a ${{NAME}} placeholder")
        return 0

    versions = load_versions()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, str] = {}
    for path in manifests():
        body = render(path, versions)
        rendered[path.name] = body
        target = OUTPUT / path.name
        target.write_text(body, encoding="utf-8")
        print(f"rendered {target}")

    if select:
        chosen = [
            chunk for body in rendered.values() for name, chunk in documents(body) if name in select
        ]
        found = {
            name for body in rendered.values() for name, _ in documents(body) if name in select
        }
        missing = sorted(set(select) - found)
        if missing:
            raise SystemExit(f"--select names objects the manifests do not declare: {missing}")
        target = OUTPUT / "selected.yaml"
        target.write_text(DOCUMENT_SEPARATOR.join(chosen), encoding="utf-8")
        print(f"\nselected {', '.join(select)} -> {target}")
    print("\napply order (one-shots are re-created, not re-applied):")
    for name in (
        "namespace.yaml",
        "postgres.yaml",
        "minio.yaml",
        "minio-init.yaml",
        "migrate-job.yaml",
        "api.yaml",
        "workers.yaml",
        "engine-service.yaml",
    ):
        if (OUTPUT / name).exists():
            print(f"  kubectl apply -f {OUTPUT / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
