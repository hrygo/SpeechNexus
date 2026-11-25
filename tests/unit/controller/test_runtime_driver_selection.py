"""The composition root builds only the node runtime driver the settings name.

``SPEECHNEXUS_CONTROLLER_RUNTIME`` selects between the local container daemon and
engine Pods in the controller's own namespace (S2 of
``docs/plans/2026-11-25-engine-runtime-k8s.md``). Two failure modes matter here:

* a deployment that names ``k8s`` must never fall back to the Docker driver, which
  would report a healthy Kubernetes deployment while starting containers;
* the selected driver's client is built before the durable intent store, so a
  deployment with no usable cluster credential fails before any state is opened.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import SecretStr

import speechnexus_node_controller.app as app_module
from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.infrastructure.kubernetes_api import (
    KubernetesApi,
    KubernetesUnavailableError,
)
from speechnexus_node_controller.infrastructure.kubernetes_runtime import KubernetesNodeRuntime
from speechnexus_node_controller.settings import Settings

NAMESPACE = "speechnexus"
SERVICE_ACCOUNT = "speechnexus-node-controller"


class _StubKubernetesApi:
    """Stands in for the cluster client so assembly is observable without a cluster."""

    def __init__(self, *, settings: object) -> None:
        self.settings = settings
        self.closed = False

    @property
    def namespace(self) -> str:
        return NAMESPACE

    def ping(self) -> None:
        raise AssertionError("assembly must not contact the cluster")

    def close(self) -> None:
        self.closed = True


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
    kubernetes: dict[str, Any] = (
        {"k8s_namespace": NAMESPACE, "k8s_service_account_name": SERVICE_ACCOUNT}
        if runtime == "k8s"
        else {}
    )
    return Settings(
        component_token=SecretStr("synthetic-controller-component-token"),
        approved_templates_file=templates,
        # A not-yet-existing parent directory makes "the store was opened" a
        # fact the side-effect test can observe.
        intent_store_path=tmp_path / "state" / "intents.sqlite3",
        docker_socket_path=tmp_path / "missing-docker.sock",
        runtime=runtime,
        **kubernetes,
    )


def test_the_default_configuration_assembles_the_docker_driver(tmp_path: Path) -> None:
    service, _readiness, store, api = app_module._build_dependencies(_settings(tmp_path))
    try:
        assert isinstance(service, NodeRuntimeControlService)
        # The adapter is private wiring, but which adapter the root selects is
        # exactly the behavior this slice adds; nothing public exposes it.
        assert isinstance(service._runtime, DockerNodeRuntime)
        assert isinstance(api, DockerApi)
    finally:
        api.close()
        store.close()


def test_selecting_kubernetes_assembles_the_cluster_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "KubernetesApi", _StubKubernetesApi)
    monkeypatch.setattr(
        app_module,
        "DockerApi",
        lambda **_kwargs: pytest.fail("the k8s driver must never construct the daemon client"),
    )

    service, _readiness, store, api = app_module._build_dependencies(
        _settings(tmp_path, runtime="k8s")
    )
    try:
        assert isinstance(service, NodeRuntimeControlService)
        assert isinstance(service._runtime, KubernetesNodeRuntime)
        assert isinstance(api, _StubKubernetesApi)
    finally:
        api.close()
        store.close()


def test_selecting_kubernetes_fails_before_the_intent_store_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_kwargs: object) -> KubernetesApi:
        raise KubernetesUnavailableError("no mounted ServiceAccount token")

    monkeypatch.setattr(app_module, "KubernetesApi", refuse)
    settings = _settings(tmp_path, runtime="k8s")

    with pytest.raises(KubernetesUnavailableError):
        app_module._build_dependencies(settings)

    assert not settings.intent_store_path.exists()
    assert not settings.intent_store_path.parent.exists()


def test_starting_the_application_with_kubernetes_serves_instead_of_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "KubernetesApi", _StubKubernetesApi)

    app = app_module.create_app(settings=_settings(tmp_path, runtime="k8s"))

    assert isinstance(app.state.runtime_control._runtime, KubernetesNodeRuntime)
