"""Conservatively compare the declared realtime runtime entrypoints.

Constraint identity is required after closed repository reference expansion.
This is a change fence, not a general JSON Schema inclusion or timing proof.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

_ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/realtime-events.schema.json"
BINDINGS = "contracts/bindings.json"
_BASES = {
    "BaseClientEvent": "request",
    "BaseStandardServerEvent": "response",
    "BaseNexusServerEvent": "response",
}
_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SINGLES = {
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}
UNVERIFIED = (
    "General schema inclusion: constraint changes require explicit upgrade/consumer proof",
    "WS ordering, epoch/credit/gap, delivery, reconnect, cancellation and terminal-state semantics",
    "Actual Python/TypeScript interoperability and real service/Gate qualification",
    "Other standalone JSON protocols and undeclared realtime runtime entrypoints",
)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _non_json_constant(value: str) -> Any:
    raise ValueError("Non-JSON numeric constant")


def _load(path: str, ref: str | None) -> Any:
    file = _ROOT / path
    file.resolve().relative_to((_ROOT / "contracts").resolve())
    text = (
        subprocess.check_output(
            ["git", "-C", str(_ROOT), "show", ref + ":" + path],
            text=True,
            stderr=subprocess.PIPE,
        )
        if ref is not None
        else file.read_text(encoding="utf-8")
    )
    return json.loads(
        text, object_pairs_hook=_object, parse_constant=_non_json_constant, parse_float=Decimal
    )


def _schema_context(schema: Any, *, root: bool = True) -> None:
    """Do not reinterpret literal JSON as schema or guess resource/dynamic scopes."""
    if not isinstance(schema, Mapping):
        return
    unsupported = {
        "$anchor",
        "$dynamicAnchor",
        "$dynamicRef",
        "$recursiveAnchor",
        "$recursiveRef",
        "$vocabulary",
    }
    if unsupported.intersection(schema) or (not root and {"$id", "$schema"}.intersection(schema)):
        raise ValueError("Reference resource context needs explicit proof")
    for key, value in schema.items():
        if key in _MAPS and isinstance(value, Mapping):
            for child in value.values():
                _schema_context(child, root=False)
        elif key in _ARRAYS and isinstance(value, list):
            for child in value:
                _schema_context(child, root=False)
        elif key in _SINGLES:
            _schema_context(value, root=False)


def _kernel() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_realtime_change_kernel", Path(__file__).with_name("check-openai-contract-compat.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Realtime comparison kernel unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inventory(document: Any, bindings: Any) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or not isinstance(bindings, dict)
        or document.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or document.get("$id") != "https://speechnexus.internal/" + SOURCE
        or not isinstance(document.get("$defs"), dict)
    ):
        raise ValueError("Realtime source identity unavailable")
    profile = bindings.get("profiles", {}).get("realtime_events")
    if (
        not isinstance(profile, dict)
        or not isinstance(profile.get("version"), str)
        or not profile["version"].strip()
    ):
        raise ValueError("Realtime runtime profile/version unavailable")
    if (
        profile.get("source") != SOURCE.removeprefix("contracts/")
        or profile.get("format") != "json-schema"
        or profile.get("client") is not False
        or profile.get("definitions_prefix") != "/$defs/"
        or profile.get("schema_refs") != {"RealtimeEvent": "#"}
        or not isinstance(profile.get("schemas"), list)
        or sorted(profile["schemas"]) != ["OpenAIPcmFormat", "RealtimeEvent", "TranscriptionEvent"]
        or set(profile.get("schema_unions", {})) != {"TranscriptionEvent"}
        or "OpenAIPcmFormat" not in document["$defs"]
    ):
        raise ValueError("Runtime entrypoint mapping needs explicit scope")
    projection = profile["schema_unions"]["TranscriptionEvent"]
    if (
        not isinstance(projection, dict)
        or set(projection) != {"base", "members"}
        or projection.get("base") != "#"
    ):
        raise ValueError("Transcription projection base unavailable")
    branches = document.get("oneOf")
    if not isinstance(branches, list) or not branches:
        raise ValueError("Public event union unavailable")
    events: dict[str, Any] = {}
    types: set[str] = set()
    for branch in branches:
        if (
            not isinstance(branch, dict)
            or set(branch) != {"$ref"}
            or not isinstance(branch["$ref"], str)
        ):
            raise ValueError("Public event union representation needs explicit proof")
        ref = branch["$ref"]
        name = ref.removeprefix("#/$defs/")
        parts = document["$defs"].get(name, {}).get("allOf")
        if (
            not ref.startswith("#/$defs/")
            or name in events
            or not isinstance(parts, list)
            or len(parts) < 2
        ):
            raise ValueError("Public event definition unavailable/duplicate")
        base = parts[0]
        event_type = parts[1].get("properties", {}).get("type", {}).get("const")
        anchor = base.get("$ref", "").removeprefix("#/$defs/") if isinstance(base, dict) else ""
        if (
            base != {"$ref": "#/$defs/" + anchor}
            or anchor not in _BASES
            or not isinstance(event_type, str)
            or not event_type
            or event_type in types
        ):
            raise ValueError("Event direction/type identity unavailable/ambiguous")
        types.add(event_type)
        events[name] = {"type": event_type, "direction": _BASES[anchor]}
    members = projection.get("members")
    public_refs = {branch["$ref"] for branch in branches}
    if (
        not isinstance(members, list)
        or not members
        or not all(isinstance(ref, str) for ref in members)
        or len(set(members)) != len(members)
        or not set(members) <= public_refs
    ):
        raise ValueError(
            "Transcription projection members unavailable/duplicate/outside public union"
        )
    return {
        "binding_version": profile["version"],
        "events": events,
        "transcription_members": sorted(members),
    }


def inspect_source(base: str) -> dict[str, Any]:
    resolved = subprocess.check_output(
        ["git", "-C", str(_ROOT), "rev-parse", "--verify", "--end-of-options", base + "^{commit}"],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()
    report: dict[str, Any] = {
        "baseline": resolved,
        "source": SOURCE,
        "scope": "declared-realtime-constraint-identity",
        "entrypoints": ["RealtimeEvent", "TranscriptionEvent", "OpenAIPcmFormat"],
        "inventory": {"base": None, "current": None},
        "findings": [],
        "unverified": list(UNVERIFIED),
    }
    kernel = _kernel()
    documents: dict[tuple[str, str | None], Any] = {}

    def load(path: str, ref: str | None) -> Any:
        key = (path, ref)
        if key not in documents:
            document = _load(path, ref)
            if path != BINDINGS:
                if (
                    not isinstance(document, dict)
                    or document.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
                    or document.get("$id") != "https://speechnexus.internal/" + path
                ):
                    raise ValueError("Referenced resource identity/dialect unavailable")
                Draft202012Validator.check_schema(document)
                _schema_context(document)
            documents[key] = document
        return documents[key]

    # All transitive references use the same strict, per-invocation snapshots.
    vars(kernel)["_load_schema_document"] = load
    findings: list[str] = report["findings"]
    try:
        old, new = load(SOURCE, resolved), load(SOURCE, None)
        inventories = [
            _inventory(old, load(BINDINGS, resolved)),
            _inventory(new, load(BINDINGS, None)),
        ]
        report["inventory"] = dict(zip(("base", "current"), inventories, strict=True))
        before, after = inventories
        if before["events"] != after["events"]:
            findings.append(
                SOURCE + ": public event inventory/direction/type changed; explicit proof required"
            )
        if before["transcription_members"] != after["transcription_members"]:
            findings.append(
                BINDINGS + ": TranscriptionEvent projection changed; explicit proof required"
            )

        sides = [
            kernel._SchemaSide(old, SOURCE, resolved, {}, False),
            kernel._SchemaSide(new, SOURCE, None, {}, False),
        ]
        # Root closure/conditions apply to both public and projected unions. Do
        # not flatten allOf (notably NexusOperationDone's conditional branches).
        selections = {
            "root-envelope": [
                {k: v for k, v in d.items() if k not in {"$defs", "oneOf"}} for d in (old, new)
            ]
        }
        for name in sorted(set(before["events"]) & set(after["events"])):
            selections[name] = [d["$defs"][name] for d in (old, new)]
        selections["OpenAIPcmFormat"] = [d["$defs"]["OpenAIPcmFormat"] for d in (old, new)]
        for name, schemas in selections.items():
            shapes = []
            for schema, side in zip(schemas, sides, strict=True):
                expanded = kernel._dereference_schema(schema, side)
                Draft202012Validator.check_schema(expanded)
                shapes.append(kernel._compatibility_shape(expanded, standard_only=False))
            if not kernel._same_json_tree(*shapes):
                findings.append(
                    SOURCE + ":" + name + ": resolved constraints changed; explicit proof required"
                )
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        OSError,
        SchemaError,
        subprocess.CalledProcessError,
        RecursionError,
    ):
        findings.append(SOURCE + ": source/binding/reference invalid or unsupported; not compared")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git commit/ref of the frozen baseline")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    try:
        report = inspect_source(args.base)
    except OSError, subprocess.CalledProcessError, RuntimeError:
        print("Realtime comparison failed: baseline or kernel unavailable", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, sort_keys=True))
    else:
        print("Realtime baseline: " + report["baseline"])
        print("Runtime inventory: " + json.dumps(report["inventory"], sort_keys=True))
        for finding in report["findings"]:
            print("  - " + finding)
        print(
            "Declared realtime constraint identity: "
            + ("rejected" if report["findings"] else "passed")
        )
        print("Unverified:")
        for boundary in report["unverified"]:
            print("  - " + boundary)
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
