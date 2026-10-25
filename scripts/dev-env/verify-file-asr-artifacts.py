#!/usr/bin/env python3
"""Offline integrity inputs for binding registration; never grants runtime qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Never

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PIN = ROOT / "deploy/engines/file-asr-audio/artifact-pin.json"
DEFAULT_DEFINITION = ROOT / "deploy/engines/file-asr-audio/binding-definition.json"
MAX_DOCUMENT_BYTES = 256 * 1024
CHUNK_BYTES = 1024 * 1024
_NAME = r"^[A-Za-z0-9_.-]{1,128}$"
_COMMIT = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
_DIGEST = {"$ref": "#/$defs/Sha256Digest"}
_SIZE = {"type": "integer", "minimum": 1}


class VerificationError(ValueError):
    """Only fixed error codes may reach CLI output; never include local paths."""


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


@lru_cache(maxsize=1)
def _pin_validator() -> Draft202012Validator:
    # Reuse the generated offline bundle, including transitive local definitions.
    bundle = json.loads(
        files("speechnexus_contracts").joinpath("model_bindings.schema.json").read_text()
    )
    named_file = {"type": "string", "pattern": _NAME}
    artifact = {"allOf": [{"$ref": "#/$defs/ArtifactReference"}, {"required": ["size_bytes"]}]}
    schema = _object(
        {
            "version": {"type": "integer", "const": 1},
            "model": _object(
                {
                    "repository": {"type": "string", "minLength": 1, "maxLength": 128},
                    "revision": _COMMIT,
                    "license": {"const": "Apache-2.0"},
                    "model_card": _object(
                        {
                            "file_name": named_file,
                            "source_git_blob": _COMMIT,
                            "digest": _DIGEST,
                            "size_bytes": _SIZE,
                        }
                    ),
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": _object(
                            {
                                "file_name": named_file,
                                "source_git_blob": _COMMIT,
                                "artifact": artifact,
                            }
                        ),
                    },
                    "ignored_metadata": {
                        "type": "array",
                        "maxItems": 3,
                        "uniqueItems": True,
                        "items": {"enum": [".complete", ".gitattributes", "configuration.json"]},
                    },
                }
            ),
            "vad": _object(
                {
                    "repository": {"type": "string", "minLength": 1, "maxLength": 128},
                    "revision": _COMMIT,
                    "source_file": {"type": "string", "minLength": 1, "maxLength": 256},
                    "license": {"const": "MIT"},
                    "artifact": artifact,
                }
            ),
            "engine": _object(
                {
                    "runtime_pin": {"type": "string", "minLength": 1, "maxLength": 256},
                    "source_commit": _COMMIT,
                    "python_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
                    "distribution_count": {"type": "integer", "minimum": 1},
                    "distribution_inventory_digest": _DIGEST,
                }
            ),
            "repository_files": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": _object(
                    {
                        "path": {"type": "string", "minLength": 1, "maxLength": 256},
                        "digest": _DIGEST,
                        "size_bytes": _SIZE,
                    }
                ),
            },
        }
    )
    schema["$defs"] = bundle["$defs"]
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _reject_number(_: str) -> Any:
    raise VerificationError("pin_invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError("pin_invalid")
        result[key] = value
    return result


def _document(raw: bytes) -> dict[str, Any]:
    value = json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_float=_reject_number,
        parse_constant=_reject_number,
    )
    if not isinstance(value, dict):
        raise VerificationError("pin_invalid")
    return value


def _parts(name: str) -> list[str]:
    parts = name.split("/")
    if any(part in ("", ".", "..") or re.fullmatch(_NAME, part) is None for part in parts):
        raise VerificationError("pin_invalid")
    return parts


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _regular(directory: int, name: str) -> Iterator[int]:
    """Open each relative component without following symlinks, including parents."""
    parents: list[int] = []
    try:
        parts = _parts(name)
        for part in parts[:-1]:
            directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            parents.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise VerificationError("file_not_regular")
            yield fd
        finally:
            os.close(fd)
    finally:
        for parent in reversed(parents):
            os.close(parent)


def _read(fd: int, limit: int) -> bytes:
    if os.fstat(fd).st_size > limit:
        raise VerificationError("document_too_large")
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(CHUNK_BYTES, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise VerificationError("document_too_large")


def _verify_file(
    directory: int, name: str, expected: dict[str, Any], *, retain: bool = False
) -> bytes:
    size: int = expected["size_bytes"]
    if retain and size > MAX_DOCUMENT_BYTES:
        raise VerificationError("document_too_large")
    with _regular(directory, name) as fd:
        before = os.fstat(fd)
        if before.st_size != size:
            raise VerificationError("file_size_mismatch")
        digest = hashlib.sha256()
        data = bytearray()
        count = 0
        while count <= size:
            chunk = os.read(fd, min(CHUNK_BYTES, size + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
            if retain:
                data.extend(chunk)
        after = os.fstat(fd)
        if (
            count != size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise VerificationError("file_changed")
        if "sha256:" + digest.hexdigest() != expected["digest"]:
            raise VerificationError("file_digest_mismatch")
        return bytes(data)


def verify(
    *, pin_path: Path, model_root: Path, vad_file: Path, repository_root: Path = ROOT
) -> dict[str, Any]:
    """Verify a local snapshot against a trusted pin, without network or engine I/O."""
    with _directory(pin_path.parent) as parent, _regular(parent, pin_path.name) as fd:
        raw = _read(fd, MAX_DOCUMENT_BYTES)
    pin = _document(raw)
    if not _pin_validator().is_valid(pin):
        raise VerificationError("pin_invalid")
    model = pin["model"]
    model_names = [entry["file_name"] for entry in model["files"]]
    required_names = [*model_names, model["model_card"]["file_name"]]
    allowed_names = [*required_names, *model["ignored_metadata"]]
    repository_names = [entry["path"] for entry in pin["repository_files"]]
    artifacts = [entry["artifact"] for entry in model["files"]] + [pin["vad"]["artifact"]]
    if (
        len(set(allowed_names)) != len(allowed_names)
        or len(set(repository_names)) != len(repository_names)
        or len({entry["artifact_id"] for entry in artifacts}) != len(artifacts)
        or pin["engine"]["runtime_pin"] not in repository_names
    ):
        raise VerificationError("pin_invalid")
    for name in [*allowed_names, *repository_names, pin["engine"]["runtime_pin"]]:
        _parts(name)
    runtime = {}
    with _directory(repository_root) as directory:
        for entry in pin["repository_files"]:
            is_runtime = entry["path"] == pin["engine"]["runtime_pin"]
            content = _verify_file(directory, entry["path"], entry, retain=is_runtime)
            if is_runtime:
                runtime = _document(content)
    engine = {
        "kind": "vllm",
        "image_digest": runtime.get("image"),
        "runtime_version": runtime.get("version"),
    }
    schema = _pin_validator().schema
    assert not isinstance(schema, bool)
    bundle = schema["$defs"]
    if (
        not Draft202012Validator(
            {"$ref": "#/$defs/EngineDescriptor", "$defs": bundle},
            format_checker=FormatChecker(),
        ).is_valid(engine)
        or runtime.get("commit") != "g" + pin["engine"]["source_commit"][:9]
    ):
        raise VerificationError("engine_pin_invalid")
    with _directory(model_root) as directory:
        seen: set[str] = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in allowed_names or not entry.is_file(follow_symlinks=False):
                    raise VerificationError("model_directory_invalid")
                seen.add(entry.name)
        if not set(required_names) <= seen:
            raise VerificationError("model_directory_invalid")
        for entry in model["files"]:
            _verify_file(directory, entry["file_name"], entry["artifact"])
        _verify_file(directory, model["model_card"]["file_name"], model["model_card"])
    with _directory(vad_file.parent) as directory:
        _verify_file(directory, vad_file.name, pin["vad"]["artifact"])
    return {
        "version": 1,
        "scope": "local_artifact_integrity",
        "pin_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "model": {key: model[key] for key in ("repository", "revision", "license")},
        "vad": {key: pin["vad"][key] for key in ("repository", "revision", "license")},
        "engine": engine,
        "artifact_set": artifacts,
        "runtime_artifact_bytes_total": sum(entry["size_bytes"] for entry in artifacts),
        "limitations": [
            "trusted_pin_required",
            "no_live_image_or_dependency_attestation",
            "no_registration_publication_or_capacity_qualification",
            "no_instance_generation_or_stop_evidence",
            "mutable_files_require_read_only_deployment",
        ],
    }


def binding_request(
    report: dict[str, Any],
    *,
    binding_id: str,
    logical_model: str,
    capacity_manifest_id: str,
    definition_path: Path = DEFAULT_DEFINITION,
) -> dict[str, Any]:
    """Prepare an exact create input from verified artifacts; never POST or allocate IDs.

    The caller retains authority for the family/model/manifest reference. The
    registry still allocates revision and snapshot identities, and publication
    must separately verify the referenced capacity and qualification evidence.
    """
    from speechnexus_contracts import ContractValidationError

    from speechnexus_backend.speech.interfaces.binding_default_drafts import (
        prepare_binding_defaults_draft,
    )

    with _directory(definition_path.parent) as parent, _regular(parent, definition_path.name) as fd:
        try:
            definition = _document(_read(fd, MAX_DOCUMENT_BYTES))
        except ValueError, TypeError, RecursionError, OverflowError:
            raise VerificationError("binding_definition_invalid") from None
    if (
        set(definition)
        != {
            "tasks",
            "execution_recipe",
            "parameter_domains",
            "release_tier",
            "parameter_defaults",
        }
        or not isinstance(definition["execution_recipe"], dict)
        or "capacity_manifest_id" in definition["execution_recipe"]
    ):
        raise VerificationError("binding_definition_invalid")
    request = {
        **definition,
        "binding_id": binding_id,
        "logical_model": logical_model,
        "engine": deepcopy(report["engine"]),
        "artifact_set": deepcopy(report["artifact_set"]),
    }
    request["execution_recipe"]["capacity_manifest_id"] = capacity_manifest_id
    try:
        # Use the real consumer's schema AND composition/default checks. This
        # pure preparation does no I/O; the validation identity is never stored.
        prepare_binding_defaults_draft(request, "offline-file-asr-validation")
    except ContractValidationError:
        raise VerificationError("binding_request_invalid") from None
    return request


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, '{"error":"invalid_arguments"}\n')


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=DEFAULT_PIN)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--vad-file", type=Path, required=True)
    parser.add_argument(
        "--emit-binding-request",
        action="store_true",
        help="emit an offline BindingDefaultsCreateRequest instead of the integrity report",
    )
    parser.add_argument("--binding-id")
    parser.add_argument("--logical-model")
    parser.add_argument("--capacity-manifest-id")
    args = parser.parse_args(argv)
    identities = (args.binding_id, args.logical_model, args.capacity_manifest_id)
    if (args.emit_binding_request and not all(identities)) or (
        not args.emit_binding_request and any(value is not None for value in identities)
    ):
        parser.error("binding request identities must be explicit")
    try:
        report = verify(pin_path=args.pin, model_root=args.model_root, vad_file=args.vad_file)
        if args.emit_binding_request:
            report = binding_request(
                report,
                binding_id=args.binding_id,
                logical_model=args.logical_model,
                capacity_manifest_id=args.capacity_manifest_id,
            )
    except VerificationError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError, ImportError:
        print(json.dumps({"error": "verification_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
