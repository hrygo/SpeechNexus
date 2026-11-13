"""Configured engine credentials must be usable by an approved template.

Issue #660: ``SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY`` was accepted even when
no approved template declared engine authentication, so the deployment looked like
an authenticated engine fleet while the key was never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

import speechnexus_node_controller.app as app_module
from speechnexus_node_controller.settings import Settings

ROOT_KEY = "a" * 64


def _template(**overrides: object) -> dict[str, object]:
    template: dict[str, object] = {
        "template_id": "template-qwen3-asr-0p6b",
        "image_reference": "local/synthetic-engine:0.1.0",
        "image_digest": "sha256:" + "c" * 64,
        "memory_architecture": "unified_memory",
        "allowed_pool_ids": ["asr-batch"],
        "max_peak_memory_bytes": 16 * 1024**3,
        "container_memory_limit_bytes": 1024**3,
        "readiness": {"kind": "tcp_port_accepting", "port": 8000},
        "command": ["python", "-m", "http.server", "8000"],
        "gpu_device_count": 0,
        "read_only_binds": [{"source": "/srv/artifacts", "target": "/artifacts"}],
    }
    template.update(overrides)
    return template


def _settings(tmp_path: Path, *, engine_auth_root_key: SecretStr | None = None) -> Settings:
    templates = tmp_path / "approved-templates.json"
    templates.write_text(json.dumps({"templates": [_template()]}), encoding="utf-8")
    return Settings(
        component_token=SecretStr("synthetic-controller-component-token"),
        approved_templates_file=templates,
        intent_store_path=tmp_path / "intents.sqlite3",
        docker_socket_path=tmp_path / "docker.sock",
        engine_auth_root_key=engine_auth_root_key,
    )


def test_a_root_key_without_an_authenticated_template_names_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path, engine_auth_root_key=SecretStr(ROOT_KEY))

    with pytest.raises(ValueError) as caught:
        app_module._build_dependencies(settings)

    message = str(caught.value)
    assert "SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY" in message
    assert "template" in message
    assert "unset the key" in message
