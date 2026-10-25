"""Synthetic integrity fixtures must never require models, Docker or network."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/verify-file-asr-artifacts.py"
SPEC = importlib.util.spec_from_file_location("file_asr_artifact_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
PIN_PATH = ROOT / "deploy/engines/file-asr-audio/artifact-pin.json"


def fingerprint(content: bytes) -> dict[str, Any]:
    return {
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, Any]:
    """Keep repository locks real; replace only large vendor artifacts with tiny bytes."""
    pin = json.loads(PIN_PATH.read_text())
    model = tmp_path / "private-model"
    model.mkdir()
    for index, entry in enumerate(pin["model"]["files"]):
        content = f"synthetic-model-file-{index}".encode()
        entry["artifact"].update(fingerprint(content))
        entry["artifact"]["artifact_id"] = "model-" + hashlib.sha256(content).hexdigest()
        (model / entry["file_name"]).write_bytes(content)
    card = b"synthetic license source, not a downloaded model"
    pin["model"]["model_card"].update(fingerprint(card))
    (model / pin["model"]["model_card"]["file_name"]).write_bytes(card)
    (model / ".complete").write_bytes(b"not evidence")
    vad = tmp_path / "private-vad.onnx"
    content = b"synthetic-vad"
    vad.write_bytes(content)
    pin["vad"]["artifact"].update(fingerprint(content))
    pin["vad"]["artifact"]["artifact_id"] = "model-" + hashlib.sha256(content).hexdigest()
    repository = tmp_path / "repository"
    for entry in pin["repository_files"]:
        destination = repository / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / entry["path"]).read_bytes())
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin))
    return {
        "pin": pin,
        "pin_path": path,
        "model_root": model,
        "vad_file": vad,
        "repository_root": repository,
    }


def verify(inputs: dict[str, Any]) -> dict[str, Any]:
    return dict(GATE.verify(**{key: value for key, value in inputs.items() if key != "pin"}))


def rewrite(inputs: dict[str, Any]) -> None:
    inputs["pin_path"].write_text(json.dumps(inputs["pin"]))


def cli(inputs: dict[str, Any], *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--pin",
            str(inputs["pin_path"]),
            "--model-root",
            str(inputs["model_root"]),
            "--vad-file",
            str(inputs["vad_file"]),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_success_emits_existing_contract_references_without_private_paths(
    inputs: dict[str, Any],
) -> None:
    result = cli(inputs)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    bundle = json.loads(
        files("speechnexus_contracts").joinpath("model_bindings.schema.json").read_text()
    )
    for name, values in (
        ("ArtifactReference", report["artifact_set"]),
        ("EngineDescriptor", [report["engine"]]),
    ):
        validator = Draft202012Validator(
            {"$ref": f"#/$defs/{name}", "$defs": bundle["$defs"]},
            format_checker=FormatChecker(),
        )
        for value in values:
            validator.validate(value)
    assert len(report["artifact_set"]) == 11
    assert report["runtime_artifact_bytes_total"] == sum(
        artifact["size_bytes"] for artifact in report["artifact_set"]
    )
    assert report["pin_digest"] == fingerprint(inputs["pin_path"].read_bytes())["digest"]
    assert report["scope"] == "local_artifact_integrity"
    assert "no_instance_generation_or_stop_evidence" in report["limitations"]
    assert str(inputs["model_root"].parent) not in result.stdout


def test_versioned_default_pin_is_valid_and_matches_repository_files() -> None:
    pin = json.loads(PIN_PATH.read_text())
    GATE._pin_validator().validate(pin)
    for entry in pin["repository_files"]:
        content = (ROOT / entry["path"]).read_bytes()
        assert fingerprint(content) == {
            "digest": entry["digest"],
            "size_bytes": entry["size_bytes"],
        }


def test_offline_verification_opens_no_socket_or_subprocess(
    inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline verification attempted external I/O")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert verify(inputs)["scope"] == "local_artifact_integrity"


@pytest.mark.parametrize("field", ["image", "commit"])
def test_rehashed_runtime_pin_still_requires_valid_engine_identity(
    inputs: dict[str, Any], field: str
) -> None:
    entry = inputs["pin"]["repository_files"][0]
    path = inputs["repository_root"] / entry["path"]
    runtime = json.loads(path.read_bytes())
    runtime[field] = "invalid"
    content = json.dumps(runtime).encode()
    path.write_bytes(content)
    entry.update(fingerprint(content))
    rewrite(inputs)
    with pytest.raises(GATE.VerificationError, match=r"^engine_pin_invalid$"):
        verify(inputs)


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "unknown",
        "nested-unknown",
        "role",
        "missing-size",
        "boolean-size",
        "digest",
        "duplicate-file",
        "duplicate-id",
        "card-collision",
        "metadata-collision",
        "metadata-wildcard",
        "duplicate-repository",
        "missing-runtime",
        "traversal",
        "absolute",
        "dot",
        "slash",
        "backslash",
        "repository-traversal",
    ],
)
def test_invalid_pin_is_rejected_before_local_artifact_reads(
    inputs: dict[str, Any], change: str
) -> None:
    pin = inputs["pin"]
    first = pin["model"]["files"][0]
    if change == "version":
        pin["version"] = True
    elif change == "unknown":
        pin["qualification"] = "passed"
    elif change == "nested-unknown":
        first["artifact"]["local_path"] = "must-not-be-accepted"
    elif change == "role":
        first["artifact"]["role"] = "invented"
    elif change == "missing-size":
        first["artifact"].pop("size_bytes")
    elif change == "boolean-size":
        first["artifact"]["size_bytes"] = True
    elif change == "digest":
        first["artifact"]["digest"] = "not-a-digest"
    elif change == "duplicate-file":
        pin["model"]["files"].append(deepcopy(first))
    elif change == "duplicate-id":
        pin["model"]["files"][1]["artifact"]["artifact_id"] = first["artifact"]["artifact_id"]
    elif change == "card-collision":
        pin["model"]["model_card"]["file_name"] = first["file_name"]
    elif change == "metadata-collision":
        first["file_name"] = ".complete"
    elif change == "metadata-wildcard":
        pin["model"]["ignored_metadata"].append("*")
    elif change == "duplicate-repository":
        pin["repository_files"].append(deepcopy(pin["repository_files"][0]))
    elif change == "missing-runtime":
        pin["repository_files"].pop(0)
    elif change == "repository-traversal":
        pin["repository_files"][1]["path"] = "../private"
    else:
        first["file_name"] = {
            "traversal": "../private",
            "absolute": "/private",
            "dot": "..",
            "slash": "nested/file",
            "backslash": r"nested\file",
        }[change]
    rewrite(inputs)
    with pytest.raises(GATE.VerificationError, match=r"^pin_invalid$"):
        verify(inputs)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":1}',
        b'{"model":{"revision":"a","revision":"b"}}',
        b'{"version":NaN}',
        b'{"version":Infinity}',
        b'{"version":1.0}',
        b"[]",
        b"\xff",
        b"{} trailing",
    ],
)
def test_bad_json_is_rejected_without_echoing_contents(inputs: dict[str, Any], raw: bytes) -> None:
    inputs["pin_path"].write_bytes(raw)
    result = cli(inputs)
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] in {"pin_invalid", "verification_failed"}
    assert str(inputs["pin_path"]) not in result.stderr


@pytest.mark.parametrize("component", ["weights", "card", "vad", "repository"])
@pytest.mark.parametrize("corruption", ["missing", "size", "digest"])
def test_modified_or_missing_files_fail(
    inputs: dict[str, Any], component: str, corruption: str
) -> None:
    pin = inputs["pin"]
    paths = {
        "weights": inputs["model_root"] / pin["model"]["files"][0]["file_name"],
        "card": inputs["model_root"] / pin["model"]["model_card"]["file_name"],
        "vad": inputs["vad_file"],
        "repository": inputs["repository_root"] / pin["repository_files"][0]["path"],
    }
    path = paths[component]
    original = path.read_bytes()
    if corruption == "missing":
        path.unlink()
    elif corruption == "size":
        path.write_bytes(original + b"x")
    else:
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises((GATE.VerificationError, OSError)):
        verify(inputs)


@pytest.mark.parametrize("kind", ["extra-file", "extra-directory", "symlink", "fifo"])
def test_model_directory_rejects_extra_and_non_regular_entries(
    inputs: dict[str, Any], kind: str
) -> None:
    model = inputs["model_root"]
    if kind == "extra-file":
        (model / "untracked-model.bin").write_bytes(b"extra")
    elif kind == "extra-directory":
        (model / "extra").mkdir()
    else:
        path = model / inputs["pin"]["model"]["files"][0]["file_name"]
        path.unlink()
        if kind == "symlink":
            path.symlink_to(inputs["vad_file"])
        else:
            os.mkfifo(path)
    result = cli(inputs)
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "model_directory_invalid"


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
def test_vad_special_file_never_blocks(inputs: dict[str, Any], kind: str) -> None:
    path = inputs["vad_file"]
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(inputs["pin_path"])
    else:
        path.mkdir()
    result = cli(inputs)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) not in result.stderr


def test_repository_parent_symlink_cannot_escape(inputs: dict[str, Any]) -> None:
    parent = inputs["repository_root"] / "deploy"
    moved = inputs["repository_root"] / "outside"
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        verify(inputs)


def test_completion_marker_does_not_replace_missing_weights(inputs: dict[str, Any]) -> None:
    (inputs["model_root"] / inputs["pin"]["model"]["files"][0]["file_name"]).unlink()
    result = cli(inputs)
    assert result.returncode == 1
    assert result.stdout == ""


def test_bounded_document_read_and_sanitized_os_errors(inputs: dict[str, Any]) -> None:
    inputs["pin_path"].write_bytes(b" " * (GATE.MAX_DOCUMENT_BYTES + 1))
    result = cli(inputs)
    assert result.returncode == 1
    assert json.loads(result.stderr) == {"error": "document_too_large"}
    inputs["pin_path"].unlink()
    result = cli(inputs)
    assert result.returncode == 1
    assert json.loads(result.stderr) == {"error": "verification_failed"}
    assert str(inputs["pin_path"]) not in result.stderr


def test_bad_cli_does_not_echo_private_argument(inputs: dict[str, Any]) -> None:
    result = cli(inputs, "--unknown-private-path", str(inputs["model_root"]))
    assert result.returncode == 2
    assert json.loads(result.stderr) == {"error": "invalid_arguments"}
    assert str(inputs["model_root"]) not in result.stderr


def test_concurrent_append_is_bounded_and_rejected(
    inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_read = os.read
    target = inputs["vad_file"]
    inode = target.stat().st_ino
    mutated = False

    def read(fd: int, count: int) -> bytes:
        nonlocal mutated
        content = original_read(fd, count)
        if os.fstat(fd).st_ino == inode and not mutated:
            mutated = True
            with target.open("ab") as stream:
                stream.write(b"unexpected append")
        return content

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(GATE.VerificationError, match=r"^file_changed$"):
        verify(inputs)
    assert mutated
