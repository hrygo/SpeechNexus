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

    references = manifest.get("schema_refs", {})
    if references.keys() - set(manifest["schemas"]):
        raise ValueError("An entrypoint reference must name a declared schema")
    for name in manifest["schemas"]:
        if name not in references:
            include(primary, manifest.get("definitions_prefix", "/components/schemas/") + name)
            continue
        target, separator, fragment = references[name].partition("#")
        if not separator or ":" in target or not fragment.startswith("/"):
            raise ValueError("An entrypoint reference must be a local JSON pointer")
        resolved = include(primary.parent / target if target else primary, fragment)
        if name != resolved:
            if name in definitions:
                raise ValueError(f"Duplicate generated entrypoint: {name}")
            definitions[name] = {"$ref": f"#/$defs/{resolved}"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": manifest.get("title", "FileContract"),
        "x-nexus-bindings-version": manifest["version"],
        "x-nexus-entrypoints": manifest["schemas"],
        "$defs": definitions,
        "anyOf": [{"$ref": f"#/$defs/{name}"} for name in manifest["schemas"]],
    }


def bundle_http_surface(config: dict[str, Any]) -> dict[str, Any]:
    """Close selected reviewed paths into a portable OpenAPI document.

    Keep parameters/responses/headers in their actual component namespaces;
    external JSON Schemas become named schema components. Never flatten away
    runtime conditionals or load references outside the reviewed contract tree.
    """
    primary = (CONTRACTS / config["source"]).resolve()
    documents: dict[Path, Any] = {}
    identities: dict[tuple[Path, str], tuple[str, str]] = {}
    components: dict[str, dict[str, Any]] = {}

    def document(path: Path) -> Any:
        path.relative_to(CONTRACTS.resolve())
        if path not in documents:
            documents[path] = yaml.safe_load(path.read_text())
        return documents[path]

    def include(path: Path, fragment: str) -> str:
        path = path.resolve()
        identity = (path, fragment)
        if identity in identities:
            category, name = identities[identity]
            return f"#/components/{category}/{name}"
        tokens = fragment.removeprefix("/").split("/") if fragment else []
        node = document(path)
        for token in tokens:
            node = node[unquote(token).replace("~1", "/").replace("~0", "~")]
        if path == primary and len(tokens) == 3 and tokens[0] == "components":
            category, name = tokens[1:]
        else:
            category = "schemas"
            external = str(path.relative_to(CONTRACTS)).replace("/", "_").replace(".", "_")
            name = (
                "External_" + external.replace("-", "_") + "_" + (tokens[-1] if tokens else "Root")
            )
        namespace = components.setdefault(category, {})
        if name in namespace:
            raise ValueError("Conflicting HTTP surface component")
        identities[identity] = category, name
        namespace[name] = {}
        namespace[name] = rewrite(copy.deepcopy(node), path)
        return f"#/components/{category}/{name}"

    def rewrite(node: Any, path: Path) -> Any:
        if isinstance(node, dict):
            node.pop("$id", None)
            ref = node.get("$ref")
            if isinstance(ref, str):
                target, _, fragment = ref.partition("#")
                if ":" in target or (fragment and not fragment.startswith("/")):
                    raise ValueError("Unsupported HTTP surface reference")
                node["$ref"] = include(path.parent / target if target else path, fragment)
            return {key: rewrite(value, path) for key, value in node.items()}
        if isinstance(node, list):
            return [rewrite(value, path) for value in node]
        return node

    source = document(primary)
    paths = {
        path: rewrite(copy.deepcopy(source["paths"][path]), primary) for path in config["paths"]
    }
    for name in source.get("components", {}).get("securitySchemes", {}):
        include(primary, "/components/securitySchemes/" + name)
    return {
        "openapi": source["openapi"],
        "info": source["info"],
        "security": source.get("security", []),
        "paths": paths,
        "components": components,
    }


def bundle_http_validation(document: dict[str, Any]) -> dict[str, Any]:
    """Derive transport envelopes; never maintain a second set of wire rules."""

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node:
            reference = node["$ref"]
            if not reference.startswith("#/components/"):
                raise ValueError("HTTP metadata must contain closed component references")
            node = document
            for token in reference[2:].split("/"):
                node = node[token.replace("~1", "/").replace("~0", "~")]
        return node

    def rebase(node: Any) -> Any:
        if isinstance(node, dict):
            result = {key: rebase(value) for key, value in node.items()}
            if "$ref" in result:
                prefix = "#/components/schemas/"
                if not result["$ref"].startswith(prefix):
                    raise ValueError("A transport schema must reference a schema component")
                result["$ref"] = "#/$defs/" + result["$ref"][len(prefix) :]
            return result
        if isinstance(node, list):
            return [rebase(value) for value in node]
        return node

    def scalar_type(schema: dict[str, Any]) -> str:
        kind = resolve(schema).get("type")
        if kind not in ("string", "integer"):
            raise ValueError("HTTP validation currently supports scalar string/integer parameters")
        return str(kind)

    def body(container: dict[str, Any], envelope: dict[str, Any], *, required: bool) -> None:
        content = container.get("content", {})
        if content:
            if set(content) != {"application/json"}:
                raise ValueError("Validated transport profiles currently support JSON bodies")
            envelope["properties"]["body"] = rebase(content["application/json"]["schema"])
            if container.get("required", required):
                envelope["required"].append("body")

    definitions = rebase(document["components"].get("schemas", {}))
    operations: dict[str, Any] = {}
    entrypoints: list[str] = []
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            name = operation["operationId"]
            if name in operations:
                raise ValueError("Duplicate HTTP operation identity")
            groups: dict[str, Any] = {
                location: {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": location == "headers",
                }
                for location in ("path", "query", "headers")
            }
            types: dict[str, dict[str, str]] = {key: {} for key in groups}
            for parameter in item.get("parameters", []) + operation.get("parameters", []):
                parameter = resolve(parameter)
                location = "headers" if parameter["in"] == "header" else parameter["in"]
                if location not in groups:
                    raise ValueError("Unsupported HTTP parameter location")
                key = parameter["name"].lower() if location == "headers" else parameter["name"]
                if key in groups[location]["properties"]:
                    raise ValueError("Duplicate HTTP parameter identity")
                schema = parameter["schema"]
                types[location][key] = scalar_type(schema)
                groups[location]["properties"][key] = rebase(schema)
                if parameter.get("required"):
                    groups[location]["required"].append(key)
            request = {
                "type": "object",
                "additionalProperties": False,
                "properties": groups,
                "required": [key for key, value in groups.items() if value["required"]],
            }
            if "requestBody" in operation:
                body(resolve(operation["requestBody"]), request, required=False)
            request_name = "HttpRequest_" + name
            if request_name in definitions:
                raise ValueError("HTTP envelope collides with a component")
            definitions[request_name] = request
            entrypoints.append(request_name)
            metadata = {
                "path": path,
                "method": method,
                "request": request_name,
                "parameters": types,
                "responses": {},
            }
            for status, raw in operation["responses"].items():
                if not status.isdecimal() or len(status) != 3:
                    raise ValueError("Validated HTTP profiles require explicit response statuses")
                response = resolve(raw)
                headers: dict[str, Any] = {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": True,
                }
                header_types: dict[str, str] = {}
                for key, value in response.get("headers", {}).items():
                    value = resolve(value)
                    normalized = key.lower()
                    header_types[normalized] = scalar_type(value["schema"])
                    headers["properties"][normalized] = rebase(value["schema"])
                    if value.get("required"):
                        headers["required"].append(normalized)
                envelope = {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"headers": headers},
                    "required": ["headers"] if headers["required"] else [],
                }
                body(response, envelope, required=True)
                response_name = "HttpResponse_" + name + "_" + status
                if response_name in definitions:
                    raise ValueError("HTTP envelope collides with a component")
                definitions[response_name] = envelope
                entrypoints.append(response_name)
                metadata["responses"][status] = {
                    "schema": response_name,
                    "headers": header_types,
                    "json": bool(response.get("content")),
                }
            operations[name] = metadata
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "x-nexus-http-operations": operations,
        "x-nexus-entrypoints": entrypoints,
        "$defs": definitions,
    }


def generate_http_profile(name: str, config: dict[str, Any], destination: Path) -> list[Path]:
    document = bundle_http_surface(config)
    bundle = bundle_http_validation(document)
    python = destination / "src/speechnexus_contracts"
    schema = python / f"{name}-http.schema.json"
    schema.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    wrapper = python / f"{name}_http.py"
    wrapper.write_text(
        '"""Generated HTTP validation entrypoints; do not edit."""\n'
        "from typing import Any\n"
        "from .http_validation import "
        "contract_for, validate_http_request, validate_http_response\n\n"
        "def contract() -> dict[str, Any]:\n"
        f"    return contract_for({name!r})\n\n"
        "def validate_request(operation: str, value: Any) -> Any:\n"
        f"    return validate_http_request({name!r}, operation, value)\n\n"
        "def validate_response(operation: str, status: int, value: Any) -> Any:\n"
        f"    return validate_http_response({name!r}, operation, status, value)\n"
    )
    types = destination / f"src/{name}-http.ts"
    types.write_text(
        "// Generated HTTP validation entrypoints; do not edit.\n"
        'import openapiFetch from "openapi-fetch";\n'
        'import { createHttpValidator } from "./http-runtime.mjs";\n'
        f'import bundle from "./speechnexus_contracts/{name}-http.schema.json" '
        'with { type: "json" };\n'
        f'import type {{ paths }} from "./{name}.generated";\n'
        'export { ContractValidationError } from "./errors.mjs";\n'
        "export type HttpOperation = "
        + " | ".join(json.dumps(n) for n in bundle["x-nexus-http-operations"])
        + ";\n"
        "type HttpPaths = Pick<paths, "
        + " | ".join(json.dumps(p) for p in document["paths"])
        + ">;\n"
        "const validator = createHttpValidator(bundle);\n"
        "export function validateRequest<T>(operation: HttpOperation, value: T): T {\n"
        "  return validator.validateRequest(operation, value);\n}\n"
        "export function validateResponse<T>("
        "operation: HttpOperation, status: number, value: T): T {\n"
        "  return validator.validateResponse(operation, status, value);\n}\n"
        "export function createValidatedClient("
        "baseUrl: string, maxJsonBytes: number, fetch?: typeof globalThis.fetch) {\n"
        '  const client = openapiFetch<HttpPaths>({ baseUrl, redirect: "error", '
        "...(fetch ? { fetch } : {}) });\n"
        "  client.use(validator.middleware(baseUrl, maxJsonBytes));\n"
        "  return client;\n}\n"
    )
    return [path.relative_to(destination) for path in (schema, wrapper, types)]


def generate_profile(name: str, config: dict[str, Any], destination: Path) -> list[Path]:
    """One producer/consumer namespace; internal protocols are never default exports."""
    document = yaml.safe_load((CONTRACTS / config["source"]).read_text())
    names = config["schemas"]
    manifest = {
        **config,
        "schemas": names,
        "version": document.get("info", {}).get("version")
        or document.get("x-speechnexus-schema-version", "1.0.0"),
        "definitions_prefix": "/components/schemas/",
        "title": name.title().replace("_", "") + "Contracts",
    }
    bundle = bundle_contracts(manifest)
    python = destination / "src/speechnexus_contracts"
    schema = python / f"{name}.schema.json"
    schema.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    generate_python_models(schema, python / f"{name}_models.py", static_projection=True)
    parser = (
        '"""Generated from contracts/bindings.json; do not edit."""\n'
        "from typing import Any, Literal, overload\n"
        f"from . import {name}_models as models\n"
        "from .validation import validate_profile\n\n"
    )
    for entry in names:
        model = entry if entry[0].isupper() else "".join(word.title() for word in entry.split("_"))
        parser += (
            f"@overload\ndef parse(name: Literal[{entry!r}], value: Any) -> models.{model}: ...\n\n"
        )
    parser += (
        f"def parse(name: str, value: Any) -> Any:\n"
        f"    return validate_profile({name!r}, name, value)\n"
    )
    (python / f"{name}.py").write_text(parser)
    types = destination / f"src/{name}.generated.ts"
    source = CONTRACTS / config["source"]
    subprocess.run(
        [
            "pnpm",
            "exec",
            "openapi-typescript",
            str(source),
            "--output",
            str(types),
            "--default-non-nullable",
            "false",
        ],
        cwd=ROOT,
        check=True,
    )
    script = (
        "// Generated from contracts/bindings.json; do not edit.\n"
        'import { createProfileParser } from "./profile-runtime.mjs";\n'
        'export { ContractValidationError } from "./errors.mjs";\n'
        f'import bundle from "./speechnexus_contracts/{name}.schema.json" '
        'with { type: "json" };\n'
        f'import type {{ components }} from "./{name}.generated";\n'
        f'export type {{ components }} from "./{name}.generated";\n'
        'export type Contracts = Pick<components["schemas"], '
        + " | ".join(json.dumps(entry) for entry in names)
        + ">;\n"
        "const parseValue = createProfileParser(bundle);\n"
        "export function parse<K extends keyof Contracts>("
        "name: K, value: unknown): Contracts[K] {\n"
        "  return parseValue(name, value) as Contracts[K];\n}\n"
    )
    script += (
        'import openapiFetch from "openapi-fetch";\n'
        f'import type {{ paths }} from "./{name}.generated";\n'
        f'export type {{ paths }} from "./{name}.generated";\n'
        "export function createClient(baseUrl: string, fetch?: typeof globalThis.fetch) {\n"
        "  return openapiFetch<paths>({ baseUrl, ...(fetch ? { fetch } : {}) });\n}\n"
    )
    (destination / f"src/{name}.ts").write_text(script)
    return [
        schema.relative_to(destination),
        (python / f"{name}_models.py").relative_to(destination),
        (python / f"{name}.py").relative_to(destination),
        types.relative_to(destination),
        Path(f"src/{name}.ts"),
    ]


def generate_python_models(schema: Path, output: Path, *, static_projection: bool = False) -> None:
    if static_projection:
        # Static projection revision 1. TypedDict cannot express conditionals
        # or intersect nested field refinements. The pinned generator replaces
        # required base fields with optional partial dictionaries in those cases.
        # Keep the base shape for static typing; runtime validators retain every
        # constraint in the unchanged authoritative bundle.
        def project(node: Any) -> Any:
            if isinstance(node, dict):
                result = {
                    key: project(value)
                    for key, value in node.items()
                    if key not in {"if", "then", "else", "dependentSchemas"}
                }
                base_fields = result.get("properties", {})
                for branch in result.get("allOf", []):
                    if isinstance(branch, dict) and "properties" in branch:
                        branch["properties"] = {
                            key: value
                            for key, value in branch["properties"].items()
                            if key not in base_fields
                        }
                        if not branch["properties"]:
                            del branch["properties"]
                return result
            if isinstance(node, list):
                return [project(value) for value in node]
            return node

        projection = output.with_suffix(".schema.json")
        projection.write_text(json.dumps(project(json.loads(schema.read_text()))))
        schema = projection
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
            str(output),
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
    flatten_typed_dicts(output)


def generate(destination: Path) -> list[Path]:
    manifest = json.loads((CONTRACTS / "bindings.json").read_text())
    python = destination / "src/speechnexus_contracts"
    python.mkdir(parents=True, exist_ok=True)
    schema = python / "file.schema.json"
    schema.write_text(json.dumps(bundle_contracts(manifest), ensure_ascii=False, indent=2) + "\n")
    coordination = python / "coordination.schema.json"
    coordination.write_text(
        json.dumps(bundle_contracts(manifest["coordination"]), ensure_ascii=False, indent=2) + "\n"
    )
    generate_python_models(schema, python / "models.py")
    names = manifest["schemas"]
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
    profile_outputs: list[Path] = []
    for profile, config in manifest["profiles"].items():
        profile_outputs.extend(generate_profile(profile, config, destination))
    http_profiles = {
        name: config
        for name, config in manifest.get("http_surfaces", {}).items()
        if config.get("validate")
    }
    for name, config in http_profiles.items():
        profile_outputs.extend(generate_http_profile(name, config, destination))
    registry = python / "profile_registry.py"
    registry.write_text(
        '"""Generated profile allowlist; do not edit."""\n'
        + "PROFILES = frozenset("
        + repr(tuple(manifest["profiles"]))
        + ")\n"
        + "HTTP_PROFILES = frozenset("
        + repr(tuple(http_profiles))
        + ")\n"
    )
    profile_outputs.append(registry.relative_to(destination))
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
            "--default-non-nullable",
            "false",
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
        Path("src/speechnexus_contracts/coordination.schema.json"),
        Path("src/speechnexus_contracts/models.py"),
        Path("src/speechnexus_contracts/parsers.py"),
        Path("src/speech.generated.ts"),
        Path("src/file.generated.ts"),
    ]
    outputs.extend(profile_outputs)
    for surface, config in manifest.get("http_surfaces", {}).items():
        artifact = python / f"{surface}.openapi.json"
        artifact.write_text(
            json.dumps(bundle_http_surface(config), ensure_ascii=False, indent=2) + "\n"
        )
        outputs.append(artifact.relative_to(destination))
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
