"""Production node identities fail closed before constructing runtime dependencies."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from speechnexus_node_controller.settings import Settings


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use only synthetic settings, without a checkout .env or inherited credentials."""
    monkeypatch.chdir(tmp_path)
    for name in os.environ:
        if name.startswith("SPEECHNEXUS_CONTROLLER_"):
            monkeypatch.delenv(name)


@pytest.fixture
def required_settings(tmp_path: Path) -> dict[str, Any]:
    return {
        "component_token": "synthetic-controller-component-token",
        "approved_templates_file": tmp_path / "templates.json",
        "intent_store_path": tmp_path / "intents.sqlite3",
    }


@pytest.mark.parametrize("node_id", ["node-cube-01", "node-studio-02", "a-1", "n" * 128])
def test_production_preserves_explicit_node_identity(
    required_settings: dict[str, Any], node_id: str
) -> None:
    settings = Settings(environment="production", node_id=node_id, **required_settings)

    assert settings.node_id == node_id
    assert not settings.intent_store_path.exists()


def test_production_loads_explicit_identity_from_environment(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", "production")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODE_ID", "node-cube-01")

    settings = Settings(**required_settings)

    assert settings.environment == "production"
    assert settings.node_id == "node-cube-01"


def test_production_loads_explicit_identity_from_dotenv(
    required_settings: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "SPEECHNEXUS_CONTROLLER_ENVIRONMENT=production\n"
        "SPEECHNEXUS_CONTROLLER_NODE_ID=node-cube-01\n",
        encoding="utf-8",
    )

    assert Settings(**required_settings).node_id == "node-cube-01"


def test_production_rejects_missing_identity(required_settings: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="explicit non-default node_id"):
        Settings(environment="production", **required_settings)


@pytest.mark.parametrize("source", ["missing", "default"])
def test_production_rejects_missing_or_default_environment_identity(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", "production")
    if source == "default":
        monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODE_ID", "node-local-1")

    with pytest.raises(ValidationError, match="explicit non-default node_id"):
        Settings(**required_settings)


def test_a_misspelled_identity_key_is_refused_by_name(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SPEECHNEXUS_CONTROLLER_NODEID`` used to be dropped without a word (#674)."""
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", "production")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODEID", "node-cube-01")

    with pytest.raises(ValidationError) as error:
        Settings(**required_settings)

    message = str(error.value)
    assert "SPEECHNEXUS_CONTROLLER_NODEID" in message
    assert "SPEECHNEXUS_CONTROLLER_NODE_ID" in message


def test_production_rejects_explicit_development_default(
    required_settings: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="explicit non-default node_id"):
        Settings(environment="production", node_id="node-local-1", **required_settings)


def test_production_identity_error_does_not_render_component_credential(
    required_settings: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(environment="production", **required_settings)

    assert required_settings["component_token"] not in str(error.value)


@pytest.mark.parametrize(
    "node_id",
    ["", " ", "ab", "node 1", "NODE-1", "node/1", "node-1 ", "node-1\n", "n" * 129],
)
def test_production_rejects_invalid_identity(
    required_settings: dict[str, Any], node_id: str
) -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", node_id=node_id, **required_settings)


@pytest.mark.parametrize("environment", ["prod", "Production", ""])
def test_invalid_environment_cannot_bypass_production_validation(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENVIRONMENT", environment)
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_NODE_ID", "node-cube-01")

    with pytest.raises(ValidationError):
        Settings(**required_settings)


@pytest.mark.parametrize("environment", ["development", "test", "staging"])
def test_nonproduction_retains_implicit_and_explicit_development_default(
    required_settings: dict[str, Any], environment: str
) -> None:
    assert Settings(environment=environment, **required_settings).node_id == "node-local-1"
    assert (
        Settings(environment=environment, node_id="node-local-1", **required_settings).node_id
        == "node-local-1"
    )


def test_omitted_environment_retains_development_behavior(
    required_settings: dict[str, Any],
) -> None:
    settings = Settings(**required_settings)

    assert settings.environment == "development"
    assert settings.node_id == "node-local-1"


def test_runtime_driver_defaults_to_docker(required_settings: dict[str, Any]) -> None:
    assert Settings(**required_settings).runtime == "docker"


@pytest.mark.parametrize("runtime", ["docker", "k8s"])
def test_runtime_driver_loads_each_declared_selector(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_RUNTIME", runtime)
    if runtime == "k8s":
        # The cluster driver is identified by the namespace and ServiceAccount its
        # Role is bound to; loading the selector alone is not a valid deployment.
        monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE", "speechnexus")
        monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME", "node-controller")

    assert Settings(**required_settings).runtime == runtime


@pytest.mark.parametrize("runtime", ["podman", "K8S", "kubernetes", " docker", ""])
def test_runtime_driver_rejects_an_unknown_selector(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_RUNTIME", runtime)

    with pytest.raises(ValidationError):
        Settings(**required_settings)


def test_selecting_kubernetes_requires_its_identity(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Role is bound to one namespace and one ServiceAccount, so both are required."""
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_RUNTIME", "k8s")

    with pytest.raises(ValidationError) as caught:
        Settings(**required_settings)
    assert "SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE" in str(caught.value)
    assert "SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME" in str(caught.value)

    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE", "speechnexus")
    with pytest.raises(ValidationError, match="SERVICE_ACCOUNT_NAME"):
        Settings(**required_settings)

    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME", "node-controller")
    assert Settings(**required_settings).k8s_service_account_name == "node-controller"


@pytest.mark.parametrize("namespace", ["SpeechNexus", "speech nexus", "speechnexus_1", ""])
def test_kubernetes_identity_must_be_a_dns_label(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_RUNTIME", "k8s")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE", namespace)
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME", "node-controller")

    with pytest.raises(ValidationError):
        Settings(**required_settings)


def test_kubernetes_identity_is_refused_when_the_docker_driver_is_selected(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured cluster target that the selected driver never reads is inert."""
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE", "speechnexus")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME", "node-controller")

    with pytest.raises(ValidationError) as caught:
        Settings(**required_settings)
    assert "K8S_NAMESPACE" in str(caught.value)
    assert "K8S_SERVICE_ACCOUNT_NAME" in str(caught.value)


def test_kubernetes_timings_are_bounded(required_settings: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(
            runtime="k8s",
            k8s_namespace="speechnexus",
            k8s_service_account_name="node-controller",
            k8s_pod_ready_timeout_seconds=0,
            **required_settings,
        )


def test_a_blank_gpu_number_is_unset_not_a_startup_error(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compose and the Deployment always pass the variable; blank means "left out".

    A fresh deployment renders these keys without values, and failing at startup on
    a value nobody set would hide the real refusal — which belongs at prepare, where
    a template actually asks for the GPU (#1262).
    """
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_MIB", "")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENGINE_GPU_TOTAL_MIB", "  ")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_UTILIZATION", "")

    settings = Settings(**required_settings)

    assert settings.engine_gpu_memory_mib is None
    assert settings.engine_gpu_total_mib is None
    assert settings.engine_gpu_memory_utilization is None


def test_a_stated_gpu_bound_survives_the_blank_normalisation(
    required_settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_MIB", "12316")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENGINE_GPU_TOTAL_MIB", "124577")
    monkeypatch.setenv("SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_UTILIZATION", "0.0989")

    settings = Settings(**required_settings)

    assert settings.engine_gpu_memory_mib == 12316
    assert settings.engine_gpu_total_mib == 124577
    assert settings.engine_gpu_memory_utilization == 0.0989
