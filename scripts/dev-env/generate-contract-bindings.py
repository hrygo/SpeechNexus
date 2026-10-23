"""Generate the offline file-workflow bundle and Python/TS boundary types.

All references must resolve inside contracts/. No remote fetch is permitted.
--check regenerates in a temporary directory and compares exact tracked bytes.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
PACKAGE = ROOT / "packages/contracts"


def flatten_typed_dicts(path: Path) -> None:
    """Flatten allOf field refinement for mutable TypedDicts (template revision 1).

    TypedDict inheritance cannot narrow an inherited field or make it required.
    The generator emits these refinements for OpenAPI allOf. Copy parent fields
    into each concrete dictionary before applying its generated child fields;
    runtime constraints remain exclusively in the untouched schema bundle.
    """
    source = path.read_text()
    tree = ast.parse(source)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def fields(node: ast.ClassDef) -> dict[str, ast.AnnAssign]:
        result: dict[str, ast.AnnAssign] = {}
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                result.update(fields(classes[base.id]))
        for field in node.body:
            if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                result[field.target.id] = field
        return result

    replacements: dict[str, list[ast.AnnAssign]] = {
        name: list(fields(node).values()) for name, node in classes.items()
    }
    for name, node in classes.items():
        node.bases = [ast.Name(id="TypedDict", ctx=ast.Load())]
        node.body = list(replacements[name]) if replacements[name] else [ast.Pass()]
    header = "\n".join(line for line in source.splitlines() if line.startswith("#"))
    path.write_text(
        header + "\n#   TypedDict flattening template: 1\n\n" + ast.unparse(tree) + "\n"
    )


def bundle_contracts(manifest: dict[str, Any]) -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    documents: dict[Path, Any] = {}
    identities: dict[tuple[Path, str], str] = {}
    primary = (CONTRACTS / manifest["source"]).resolve()

    def include(path: Path, fragment: str) -> str:
        path = path.resolve()
        path.relative_to(CONTRACTS)
        identity = (path, fragment)
        if identity in identities:
            return identities[identity]
        if path not in documents:
            documents[path] = yaml.safe_load(path.read_text())
        node = documents[path]
        if fragment:
            if not fragment.startswith("/"):
                raise ValueError(f"Unsupported reference fragment: {fragment}")
            for token in fragment[1:].split("/"):
                node = node[unquote(token).replace("~1", "/").replace("~0", "~")]
        name = (
            fragment.rsplit("/", 1)[-1]
            if path == primary
            else (
                "External_"
                + str(path.relative_to(CONTRACTS))
                .replace("/", "_")
                .replace(".", "_")
                .replace("-", "_")
                + "_"
                + fragment.rsplit("/", 1)[-1]
            )
        )
        if name in definitions:
            raise ValueError(f"Duplicate generated definition: {name}")
        identities[identity] = name
        definitions[name] = {}  # Reserve before walking recursive references.
        definitions[name] = rewrite(copy.deepcopy(node), path)
        return name

    def rewrite(node: Any, path: Path) -> Any:
        if isinstance(node, dict):
            # Rebase refs into one closed document; original $id scopes no longer apply.
            node.pop("$id", None)
            ref = node.get("$ref")
            if isinstance(ref, str):
                target, _, fragment = ref.partition("#")
                if ":" in target:
                    raise ValueError("Remote and absolute URI references are forbidden")
                name = include(path.parent / target if target else path, fragment)
                node["$ref"] = f"#/$defs/{name}"
            # OpenAPI discriminator mappings are annotations, not JSON Schema refs.
            node.pop("discriminator", None)
            return {key: rewrite(value, path) for key, value in node.items()}
        if isinstance(node, list):
            return [rewrite(value, path) for value in node]
        return node

    for name in manifest["schemas"]:
        include(primary, f"/components/schemas/{name}")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "FileContract",
        "x-nexus-bindings-version": manifest["version"],
        "x-nexus-entrypoints": manifest["schemas"],
        "$defs": definitions,
        "anyOf": [{"$ref": f"#/$defs/{name}"} for name in manifest["schemas"]],
    }


def generate(destination: Path) -> list[Path]:
    manifest = json.loads((CONTRACTS / "bindings.json").read_text())
    python = destination / "src/speechnexus_contracts"
    python.mkdir(parents=True, exist_ok=True)
    schema = python / "file.schema.json"
    schema.write_text(json.dumps(bundle_contracts(manifest), ensure_ascii=False, indent=2) + "\n")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(schema),
            "--input-file-type",
            "jsonschema",
            "--output",
            str(python / "models.py"),
            "--output-model-type",
            "typing.TypedDict",
            "--target-python-version",
            "3.14",
            "--disable-timestamp",
            "--enable-version-header",
            "--no-allow-remote-refs",
            "--allof-class-hierarchy",
            "if-no-conflict",
            "--allof-merge-mode",
            "all",
            "--no-use-closed-typed-dict",
            "--import-overrides",
            '{"TypedDict":"typing"}',
            "--use-standard-collections",
            "--use-union-operator",
            "--formatters",
            "ruff-check",
            "ruff-format",
        ],
        cwd=ROOT,
        check=True,
    )
    names = manifest["schemas"]
    flatten_typed_dicts(python / "models.py")
    parsers = (
        '"""Generated from contracts/bindings.json; do not edit."""\n\n'
        "from typing import Any, Literal, overload\n\n"
        "from .models import " + ", ".join(names) + "\n\n"
    )
    for name in names:
        parsers += f"@overload\ndef parse(name: Literal[{name!r}], value: Any) -> {name}: ...\n\n"
    parsers += (
        "def parse(name: str, value: Any) -> Any:\n"
        "    from .validation import validate\n\n"
        "    return validate(name, value)\n"
    )
    (python / "parsers.py").write_text(parsers)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(ROOT / "pyproject.toml"),
            str(python),
        ],
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--fix",
            "--config",
            str(ROOT / "pyproject.toml"),
            str(python),
        ],
        check=True,
        cwd=ROOT,
        capture_output=False,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(ROOT / "pyproject.toml"),
            str(python),
        ],
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    subprocess.run(
        [
            "pnpm",
            "exec",
            "openapi-typescript",
            str(CONTRACTS / manifest["source"]),
            "--output",
            str(destination / "src/speech.generated.ts"),
        ],
        cwd=ROOT,
        check=True,
    )
    (destination / "src/file.generated.ts").write_text(
        "// Generated from contracts/bindings.json; do not edit.\n"
        'import type { components } from "./speech.generated";\n'
        'export type FileContracts = Pick<components["schemas"],\n'
        + " | ".join(json.dumps(name) for name in names)
        + ">;\n"
    )
    outputs = [
        Path("src/speechnexus_contracts/file.schema.json"),
        Path("src/speechnexus_contracts/models.py"),
        Path("src/speechnexus_contracts/parsers.py"),
        Path("src/speech.generated.ts"),
        Path("src/file.generated.ts"),
    ]
    inventory: dict[str, Any] = {}
    for path in sorted(CONTRACTS.rglob("*")):
        if path.name.endswith((".schema.json", ".yaml")):
            source = yaml.safe_load(path.read_text())
            inventory[str(path.relative_to(CONTRACTS))] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "version": source.get("info", {}).get("version")
                or source.get("x-nexus-version")
                or source.get("version"),
            }
    (destination / "source-inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n"
    )
    outputs.append(Path("source-inventory.json"))
    subprocess.run(
        [
            "pnpm",
            "exec",
            "prettier",
            "--ignore-path",
            "/dev/null",
            "--config",
            str(ROOT / ".prettierrc.json"),
            "--write",
            *[str(destination / path) for path in outputs if path.suffix in {".json", ".ts"}],
        ],
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="speechnexus-bindings-") as temporary:
        destination = Path(temporary)
        for path in generate(destination):
            generated = (destination / path).read_bytes()
            target = PACKAGE / path
            if args.check:
                if not target.exists() or target.read_bytes() != generated:
                    raise SystemExit(
                        f"Generated contract drift: {path}; run make contracts-generate"
                    )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(generated)
    print("Contract bindings: " + ("up to date" if args.check else "generated"))


if __name__ == "__main__":
    main()
