"""The subprocess entry and the container entry point, without a live server."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from tests.unit.inspector.support import TOKEN, pcm_wav

from speechnexus_asset_inspector import child
from speechnexus_asset_inspector import main as entry


def _open_input(tmp_path: Path, data: bytes) -> int:
    path = tmp_path / "private-input"
    path.write_bytes(data)
    return os.open(path, os.O_RDONLY)


def _decode_argv(descriptor: int, *, max_duration_ms: int = 120_000) -> list[str]:
    return [
        "--input-fd",
        str(descriptor),
        "--max-encoded-bytes",
        "1048576",
        "--max-duration-ms",
        str(max_duration_ms),
        "--timeout-seconds",
        "10",
    ]


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, Any]]:
    code = child.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_child_reports_kernel_limits(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run(capsys, ["--report-limits"])
    assert code == 0
    assert set(payload["limits"]) == {"as", "cpu", "fsize", "nofile", "core"}


def test_child_decodes_a_private_descriptor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    descriptor = _open_input(tmp_path, pcm_wav(1_600))
    try:
        code, payload = _run(capsys, _decode_argv(descriptor))
    finally:
        os.close(descriptor)
    assert code == 0
    assert payload["status"] == "complete"
    assert payload["recipe_version"] == "pcm-decode-1"
    assert payload["encoded_bytes"] == len(pcm_wav(1_600))


def test_child_rejects_incomplete_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run(capsys, [])
    assert code == 2
    assert payload == {"schema_version": 1, "status": "unavailable"}


def test_child_maps_invalid_media_to_a_content_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    descriptor = _open_input(tmp_path, b"synthetic-corrupt-audio")
    try:
        code, payload = _run(capsys, _decode_argv(descriptor))
    finally:
        os.close(descriptor)
    assert code == 1
    assert payload == {"schema_version": 1, "status": "invalid_media"}


def test_child_maps_duration_limit_to_a_budget_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    descriptor = _open_input(tmp_path, pcm_wav(32_000))
    try:
        code, payload = _run(capsys, _decode_argv(descriptor, max_duration_ms=1_000))
    finally:
        os.close(descriptor)
    assert code == 1
    assert payload == {"schema_version": 1, "status": "media_limit_exceeded"}


def test_child_hides_unexpected_decoder_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("synthetic-private-decoder-failure")

    monkeypatch.setattr(child, "inspect_audio", explode)
    descriptor = _open_input(tmp_path, pcm_wav(80))
    try:
        code, payload = _run(capsys, _decode_argv(descriptor))
    finally:
        os.close(descriptor)
    assert code == 2
    assert payload == {"schema_version": 1, "status": "unavailable"}


def test_child_rejects_a_recipe_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        child,
        "inspect_audio",
        lambda **_kwargs: {"status": "complete", "recipe_version": "other-recipe"},
    )
    descriptor = _open_input(tmp_path, pcm_wav(80))
    try:
        code, payload = _run(capsys, _decode_argv(descriptor))
    finally:
        os.close(descriptor)
    assert code == 2
    assert payload == {"schema_version": 1, "status": "unavailable"}


def _clear_inspection_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("SPEECHNEXUS_INSPECTION_"):
            monkeypatch.delenv(name)


def test_main_requires_the_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_inspection_environment(monkeypatch)
    with pytest.raises(SystemExit, match="SPEECHNEXUS_INSPECTION_TOKEN"):
        entry.main()


def test_main_requires_a_private_runtime_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_inspection_environment(monkeypatch)
    monkeypatch.setenv("SPEECHNEXUS_INSPECTION_TOKEN", TOKEN)
    monkeypatch.setattr(entry, "RUNTIME_ROOT", tmp_path / "missing")
    with pytest.raises(SystemExit, match="runtime root"):
        entry.main()


def test_main_requires_prlimit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_inspection_environment(monkeypatch)
    monkeypatch.setenv("SPEECHNEXUS_INSPECTION_TOKEN", TOKEN)
    monkeypatch.setattr(entry, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(entry, "PRLIMIT", tmp_path / "missing-prlimit")
    with pytest.raises(SystemExit, match="prlimit"):
        entry.main()


def test_main_starts_the_configured_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_inspection_environment(monkeypatch)
    monkeypatch.setenv("SPEECHNEXUS_INSPECTION_TOKEN", TOKEN)
    monkeypatch.setenv("SPEECHNEXUS_INSPECTION_HOST", "127.0.0.1")
    monkeypatch.setenv("SPEECHNEXUS_INSPECTION_PORT", "18088")
    prlimit = tmp_path / "prlimit"
    prlimit.write_text("")
    monkeypatch.setattr(entry, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(entry, "PRLIMIT", prlimit)
    marker = object()
    monkeypatch.setattr(entry, "create_app", lambda _service: marker)
    calls: list[tuple[object, dict[str, Any]]] = []

    def record(app: object, **kwargs: Any) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(uvicorn, "run", record)
    entry.main()

    assert len(calls) == 1
    app, kwargs = calls[0]
    assert app is marker
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 18088,
        "log_level": "info",
        "access_log": False,
        "server_header": False,
        "date_header": False,
    }
