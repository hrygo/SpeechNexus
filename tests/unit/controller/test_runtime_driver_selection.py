"""The composition root builds only the node runtime driver the settings name.

``SPEECHNEXUS_CONTROLLER_RUNTIME=k8s`` is a reserved selector for S2 of
``docs/plans/2026-11-25-engine-runtime-k8s.md``. Until that adapter exists the
controller must refuse at build time -- before the approved templates, the
intent store, or the daemon client are opened -- and must never fall back to the
Docker driver, which would report a healthy Kubernetes deployment while starting
containers in the wrong runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import SecretStr

import speechnexus_node_controller.app as app_module
from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.settings import Settings

PLAN_PATH = "docs/plans/2026-11-25-engine-runtime-k8s.md"


def _settings(tmp_path: Path, *, runtime: Literal["docker", "k8s"] = "docker") -> Settings:
    templates = tmp_path / "approved-templates.json"
    if not templates.exists():
        templates.write_text(
            json.dumps(
                {
                    "templates": [
                        {
                            "template_id": "template-synthetic-asr",
                            "image_reference": "local/synthetic-engine:0.1.0",
                            "image_digest": "sha256:" + "c" * 64,
                            "memory_architecture": "unified_memory",
                            "allowed_pool_ids": ["asr-batch"],
                            "max_peak_memory_bytes": 16 * 1024**3,
                            "container_memory_limit_bytes": 1024**3,
                            "readiness": {"kind": "process_running"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    return Settings(
        component_token=SecretStr("synthetic-controller-component-token"),
        approved_templates_file=templates,
        # A not-yet-existing parent directory makes "the store was opened" a
        # fact the side-effect test can observe.
        intent_store_path=tmp_path / "state" / "intents.sqlite3",
        docker_socket_path=tmp_path / "missing-docker.sock",
        runtime=runtime,
    )


def test_the_default_configuration_assembles_the_docker_driver(tmp_path: Path) -> None:
    service, _readiness, store, api = app_module._build_dependencies(_settings(tmp_path))
    try:
        assert isinstance(service, NodeRuntimeControlService)
        # The adapter is private wiring, but which adapter the root selects is
        # exactly the behavior this slice adds; nothing public exposes it.
        assert isinstance(service._runtime, DockerNodeRuntime)
    finally:
        api.close()
        store.close()


def test_selecting_kubernetes_fails_at_build_time_with_the_plan_path(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError) as caught:
        app_module._build_dependencies(_settings(tmp_path, runtime="k8s"))

    assert PLAN_PATH in str(caught.value)
    assert "S2" in str(caught.value)


def test_selecting_kubernetes_fails_before_the_intent_store_is_opened(tmp_path: Path) -> None:
    settings = _settings(tmp_path, runtime="k8s")

    with pytest.raises(NotImplementedError):
        app_module._build_dependencies(settings)

    assert not settings.intent_store_path.exists()
    assert not settings.intent_store_path.parent.exists()


def test_starting_the_application_with_kubernetes_refuses_instead_of_serving(
    tmp_path: Path,
) -> None:
    with pytest.raises(NotImplementedError, match="engine-runtime-k8s"):
        app_module.create_app(settings=_settings(tmp_path, runtime="k8s"))
