"""CustomVoice uses the existing offline verifier and registry preparation boundary."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts.binding_defaults_producer import parse

from tests.unit.test_file_asr_artifacts import GATE, ROOT, SCRIPT, fingerprint

PIN_PATH = ROOT / "deploy/engines/file-tts-customvoice/artifact-pin.json"
DEFINITION_PATH = PIN_PATH.with_name("binding-definition.json")
REGISTRATION = (
    "--emit-binding-request",
    "--binding-definition",
    str(DEFINITION_PATH),
    "--binding-id",
    "tts-handoff-synthetic",
    "--logical-model",
    "tts-customvoice",
    "--capacity-manifest-id",
    "manifest-handoff-synthetic",
)


@pytest.fixture
def tts_inputs(tmp_path: Path) -> dict[str, Any]:
    pin = json.loads(PIN_PATH.read_text())
    model = tmp_path / "private-model"
    model.mkdir()
    for index, entry in enumerate(pin["model"]["files"]):
        content = f"synthetic-tts-file-{index}".encode()
        entry["artifact"].update(fingerprint(content))
        entry["artifact"]["artifact_id"] = "model-" + fingerprint(content)["digest"][7:]
        path = model / entry["file_name"]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
    card = b"synthetic license source, not vendor content"
    pin["model"]["model_card"].update(fingerprint(card))
    (model / "README.md").write_bytes(card)
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
        "repository_root": repository,
    }


def verify(inputs: dict[str, Any]) -> dict[str, Any]:
    return dict(GATE.verify(**{key: value for key, value in inputs.items() if key != "pin"}))


def cli(inputs: dict[str, Any], *extra: str) -> subprocess.CompletedProcess[str]:
    import sys

    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--pin",
            str(inputs["pin_path"]),
            "--model-root",
            str(inputs["model_root"]),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_versioned_customvoice_pin_reuses_existing_artifact_and_engine_contracts() -> None:
    pin = json.loads(PIN_PATH.read_text())
    GATE._pin_validator(2).validate(pin)
    for entry in pin["repository_files"]:
        assert fingerprint((ROOT / entry["path"]).read_bytes()) == {
            "digest": entry["digest"],
            "size_bytes": entry["size_bytes"],
        }


def test_nested_customvoice_verification_is_offline_and_emits_no_private_paths(
    tts_inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline verifier attempted external I/O")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = verify(tts_inputs)
    assert report["version"] == 2 and report["scope"] == "local_artifact_integrity"
    assert report["engine"]["kind"] == "vllm_omni"
    assert "vad" not in report
    assert len(report["artifact_set"]) == 11
    assert report["runtime_artifact_bytes_total"] == sum(
        artifact["size_bytes"] for artifact in report["artifact_set"]
    )
    assert str(tts_inputs["model_root"]) not in json.dumps(report)


def test_complete_customvoice_create_request_preserves_explicit_unallocated_references(
    tts_inputs: dict[str, Any],
) -> None:
    result = cli(tts_inputs, *REGISTRATION)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    parse("BindingDefaultsCreateRequest", body)
    assert body["tasks"] == ["tts_file"]
    assert body["engine"] == verify(tts_inputs)["engine"]
    assert body["artifact_set"] == verify(tts_inputs)["artifact_set"]
    assert (
        body["parameter_defaults"] == json.loads(DEFINITION_PATH.read_text())["parameter_defaults"]
    )
    assert body["parameter_domains"]["tts"]["speed_range"]["minimum"] == 0.5
    assert body["binding_id"] == "tts-handoff-synthetic"
    assert body["execution_recipe"]["capacity_manifest_id"] == "manifest-handoff-synthetic"
    assert not {"revision_id", "snapshot_id", "publication", "created_at"} & body.keys()
    assert str(tts_inputs["model_root"]) not in result.stdout


@pytest.mark.parametrize("change", ["extra-file", "empty-directory", "symlink-directory", "fifo"])
def test_nested_tokenizer_rejects_unknown_and_special_entries(
    tts_inputs: dict[str, Any], change: str
) -> None:
    model = tts_inputs["model_root"]
    tokenizer = model / "speech_tokenizer"
    if change == "extra-file":
        (tokenizer / "untracked.bin").write_bytes(b"unexpected")
    elif change == "empty-directory":
        (tokenizer / "empty").mkdir()
    elif change == "symlink-directory":
        tokenizer.rename(model / "moved-tokenizer")
        tokenizer.symlink_to(model / "moved-tokenizer", target_is_directory=True)
    else:
        target = tokenizer / "model.safetensors"
        target.unlink()
        os.mkfifo(target)
    result = cli(tts_inputs)
    assert result.returncode == 1 and not result.stdout
    assert json.loads(result.stderr)["error"] == "model_directory_invalid"


@pytest.mark.parametrize("change", ["missing", "size", "digest"])
def test_nested_weights_and_codec_configuration_are_verified(
    tts_inputs: dict[str, Any], change: str
) -> None:
    target = tts_inputs["model_root"] / "speech_tokenizer/config.json"
    content = target.read_bytes()
    if change == "missing":
        target.unlink()
    elif change == "size":
        target.write_bytes(content + b"x")
    else:
        target.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    result = cli(tts_inputs)
    assert result.returncode == 1 and not result.stdout


def test_tokenizer_directory_swapped_to_symlink_during_open_is_rejected(
    tts_inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = os.open
    model = tts_inputs["model_root"]
    tokenizer = model / "speech_tokenizer"
    swapped = False

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if path == "speech_tokenizer" and flags & os.O_DIRECTORY and not swapped:
            swapped = True
            tokenizer.rename(model / "moved-tokenizer")
            tokenizer.symlink_to(model / "moved-tokenizer", target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(OSError):
        verify(tts_inputs)
    assert swapped


@pytest.mark.parametrize(
    "change", ["traversal", "absolute", "depth", "file-directory-collision", "duplicate-id"]
)
def test_v2_pin_rejects_unsafe_or_ambiguous_nested_names(
    tts_inputs: dict[str, Any], change: str
) -> None:
    pin = tts_inputs["pin"]
    first = pin["model"]["files"][0]
    if change == "file-directory-collision":
        first["file_name"] = "speech_tokenizer"
    elif change == "duplicate-id":
        pin["model"]["files"][1]["artifact"]["artifact_id"] = first["artifact"]["artifact_id"]
    else:
        first["file_name"] = {
            "traversal": "speech_tokenizer/../private",
            "absolute": "/private",
            "depth": "a/b/c/d/e",
        }[change]
    tts_inputs["pin_path"].write_text(json.dumps(pin))
    with pytest.raises(GATE.VerificationError, match=r"^pin_invalid$"):
        verify(tts_inputs)


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "source_commit",
        "python_version",
        "distribution_count",
        "distribution_inventory_digest",
    ],
)
def test_v2_runtime_cannot_be_relabelled_with_inconsistent_metadata(
    tts_inputs: dict[str, Any], field: str
) -> None:
    entry = tts_inputs["pin"]["repository_files"][0]
    path = tts_inputs["repository_root"] / entry["path"]
    runtime = json.loads(path.read_text())
    runtime[field] = "vllm" if field == "kind" else "inconsistent"
    raw = json.dumps(runtime).encode()
    path.write_bytes(raw)
    entry.update(fingerprint(raw))
    tts_inputs["pin_path"].write_text(json.dumps(tts_inputs["pin"]))
    with pytest.raises(GATE.VerificationError, match=r"^engine_pin_invalid$"):
        verify(tts_inputs)


def test_legacy_cli_without_vad_retains_its_usage_error(tts_inputs: dict[str, Any]) -> None:
    import sys

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--model-root", str(tts_inputs["model_root"])],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2 and not result.stdout
    assert json.loads(result.stderr) == {"error": "invalid_arguments"}


@pytest.mark.parametrize(
    "extra",
    [
        ("--vad-file", "unused-private-file"),
        ("--emit-binding-request",),
        ("--binding-definition", str(DEFINITION_PATH)),
        tuple(
            value
            for value in REGISTRATION
            if value not in ("--binding-definition", str(DEFINITION_PATH))
        ),
    ],
)
def test_v2_cli_does_not_invent_vad_or_binding_inputs(
    tts_inputs: dict[str, Any], extra: tuple[str, ...]
) -> None:
    result = cli(tts_inputs, *extra)
    assert result.returncode == 2 and not result.stdout
    assert json.loads(result.stderr) == {"error": "invalid_arguments"}


@pytest.mark.parametrize(
    "change",
    ["language", "streaming", "planner", "speed", "seed", "unknown", "nonfinite", "overflow"],
)
def test_customvoice_defaults_use_real_registry_composition_checks(
    tts_inputs: dict[str, Any], tmp_path: Path, change: str
) -> None:
    definition = json.loads(DEFINITION_PATH.read_text())
    values = definition["parameter_defaults"]["tts"]
    if change == "language":
        values["language"] = "fr"
    elif change == "streaming":
        values["audio_delivery"] = "stream"
    elif change == "planner":
        values["planner"]["max_chars"] = 401
    elif change == "speed":
        values["speed"] = 2.5
    elif change == "seed":
        definition["parameter_domains"]["tts"]["supports_seed"] = False
        values["seed"] = 7
    elif change == "unknown":
        definition["revision_id"] = "must-not-be-allocated"
    else:
        values["speed"] = float("nan") if change == "nonfinite" else float("inf")
    path = tmp_path / "definition.json"
    path.write_text(json.dumps(definition))
    with pytest.raises(GATE.VerificationError):
        GATE.binding_request(
            verify(tts_inputs),
            binding_id="synthetic-tts",
            logical_model="tts-customvoice",
            capacity_manifest_id="synthetic-manifest",
            definition_path=path,
        )


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("response_format", ["wav", "mp3", "pcm"])
async def test_declared_defaults_reach_existing_command_consumer_and_one_synthesis(
    language: str, response_format: str
) -> None:
    from tests.fake.execution_command import command_example
    from tests.unit.gateway.test_file_tts_command import build, tts_request

    definition = json.loads(DEFINITION_PATH.read_text())
    command = deepcopy(command_example(0))
    command["parameters"].update(definition["parameter_defaults"]["tts"])
    command["parameters"].update(language=language, response_format=response_format)
    command["text"] = "Synthetic handoff."
    runner, engine, encoder = build()
    assert not engine.calls
    result = await runner.synthesize(tts_request(command=command), cancelled=lambda: False)
    assert len(engine.calls) == len(encoder.calls) == 1
    assert engine.calls[0][1].language == language
    assert result.delivery.format.value == response_format
