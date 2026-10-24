"""The approved-template file is deployment configuration, not request input."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from speechnexus_node_controller.domain.intents import (
    InvalidRuntimeRequestError,
    MemoryArchitecture,
)
from speechnexus_node_controller.domain.templates import ReadinessKind
from speechnexus_node_controller.infrastructure.template_config import (
    load_approved_templates,
)


def _template(**overrides: Any) -> dict[str, Any]:
    template: dict[str, Any] = {
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


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "approved-templates.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_an_approved_template_file_loads_into_the_registry(tmp_path: Path) -> None:
    templates = load_approved_templates(_write(tmp_path, {"templates": [_template()]}))

    assert len(templates) == 1
    template = templates[0]
    assert template.template_id == "template-qwen3-asr-0p6b"
    assert template.memory_architecture is MemoryArchitecture.UNIFIED_MEMORY
    assert template.readiness.kind is ReadinessKind.TCP_PORT_ACCEPTING
    assert template.readiness.port == 8000
    assert template.command == ("python", "-m", "http.server", "8000")
    assert template.container_memory_limit_bytes == 1024**3
    assert template.read_only_binds[0].target == "/artifacts"


@pytest.mark.parametrize(
    "document",
    [
        {"templates": []},
        {"templates": [{}]},
        {"templates": [_template(unexpected="field")]},
        {"templates": [_template(image_digest="not-a-digest")]},
        {"templates": [_template(readiness={"kind": "tcp_port_accepting"})]},
        {"templates": [_template(readiness={"kind": "process_running", "port": 8000})]},
        {"templates": [_template(allowed_pool_ids=[])]},
        {"templates": [_template(gpu_device_count=-1)]},
        {"templates": [_template(max_peak_memory_bytes=0)]},
        {"templates": [_template(container_memory_limit_bytes=0)]},
        {"templates": [_template(container_memory_limit_bytes=-1)]},
        {"templates": [_template(container_memory_limit_bytes=5 * 1024**2)]},
        {"templates": [_template(container_memory_limit_bytes=17 * 1024**3)]},
        {"templates": [_template(container_memory_limit_bytes="1073741824")]},
        {"templates": [_template(container_memory_limit_bytes=True)]},
        {"templates": [_template(network_name="host")]},
        {"templates": [_template(network_name="container:other")]},
        {"templates": [_template(network_name="bridge")]},
        {"templates": [_template(readiness={"kind": "http_status_ok", "port": 8000})]},
        {"templates": [_template(readiness={"kind": "process_running", "path": "/health"})]},
        {"templates": [_template()], "unexpected": "field"},
    ],
)
def test_a_template_file_that_could_widen_what_runs_is_refused(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    with pytest.raises((ValidationError, ValueError, InvalidRuntimeRequestError)):
        load_approved_templates(_write(tmp_path, document))


@pytest.mark.parametrize(
    "path",
    [
        "http://other/health",
        "//other/health",
        "/health?url=other",
        "/health#other",
        "/../health",
        "/%2fother",
        "/health\\other",
        "/health\n",
        "/" + "a" * 256,
    ],
)
def test_http_readiness_refuses_endpoints_outside_a_fixed_container_path(
    tmp_path: Path, path: str
) -> None:
    with pytest.raises(ValueError, match="fixed absolute health path"):
        load_approved_templates(
            _write(
                tmp_path,
                {
                    "templates": [
                        _template(readiness={"kind": "http_status_ok", "port": 8000, "path": path})
                    ]
                },
            )
        )


def test_http_template_loads_without_changing_existing_readiness_kinds(tmp_path: Path) -> None:
    template = load_approved_templates(
        _write(
            tmp_path,
            {
                "templates": [
                    _template(readiness={"kind": "http_status_ok", "port": 8000, "path": "/health"})
                ]
            },
        )
    )[0]
    assert template.readiness.kind is ReadinessKind.HTTP_STATUS_OK
    assert template.readiness.path == "/health"


def test_legacy_template_cannot_silently_keep_unlimited_container_memory(tmp_path: Path) -> None:
    legacy = _template()
    del legacy["container_memory_limit_bytes"]
    with pytest.raises(ValidationError, match="container_memory_limit_bytes"):
        load_approved_templates(_write(tmp_path, {"templates": [legacy]}))
