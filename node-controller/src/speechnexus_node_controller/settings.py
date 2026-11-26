"""Validated process configuration for the restricted node controller.

The controller runs on one node and is useless without a component credential,
an approved-template file, and a durable intent store, so those have no
defaults: a half-configured controller fails at startup instead of silently
serving intents it cannot act on or remember.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_node_controller.domain.intents import (
    InvalidRuntimeRequestError,
    require_identifier,
)
from speechnexus_node_controller.environment import (
    PLANES,
    refuse_unrecognised_prefixed_environment,
)

KUBERNETES_RUNTIME: Final = "k8s"
_DNS_LABEL: Final = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


class Settings(BaseSettings):
    """Runtime settings loaded from SPEECHNEXUS_CONTROLLER_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SPEECHNEXUS_CONTROLLER_",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    service_name: str = Field(
        default="speechnexus-node-controller",
        min_length=1,
        max_length=64,
    )
    node_id: str = Field(default="node-local-1", min_length=1, max_length=128)
    host: str = "127.0.0.1"
    port: int = Field(default=8020, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    component_token: SecretStr = Field(min_length=16)
    engine_auth_root_key: SecretStr | None = None
    approved_templates_file: Path
    intent_store_path: Path
    # Which node runtime adapter the composition root constructs: the local
    # container daemon, or engine Pods in this controller's own namespace. The
    # selector is explicit so a deployment cannot keep starting Docker containers
    # while believing it schedules Pods.
    runtime: Literal["docker", "k8s"] = "docker"
    docker_socket_path: Path = Path("/var/run/docker.sock")
    docker_request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    stop_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    exit_wait_seconds: float = Field(default=30.0, gt=0, le=600)
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    # The GPU memory budget is deployment configuration, not a template constant:
    # the same approved template runs on a 48 GB production card and on this
    # unified-memory box. A GPU template with no statement here is refused at
    # prepare instead of being started unbounded, because the engine's own default
    # takes 92% of the whole card (#1262). `resolve_gpu_budget` is the only place
    # that decides which of these three values the engine is told.
    engine_gpu_memory_mib: int | None = Field(default=None, gt=0, le=1_000_000)
    engine_gpu_total_mib: int | None = Field(default=None, gt=0, le=1_000_000)
    engine_gpu_memory_utilization: float | None = Field(default=None, gt=0, le=1.0)
    engine_libvgpu_path: Path | None = None
    # Kubernetes driver identity. The namespaced Role grants pod permissions to
    # exactly one ServiceAccount in one namespace; the driver also verifies the
    # mounted credential belongs to that identity. Both have no default: a Pod
    # created in the wrong namespace is a rejection, not a working engine.
    k8s_namespace: str | None = None
    k8s_service_account_name: str | None = None
    # Pod readiness is transport health with its own wall-clock budget; the
    # plan records that the real create-to-serving time is still unmeasured.
    k8s_pod_ready_timeout_seconds: float = Field(default=180.0, gt=0, le=3600)
    k8s_pod_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    k8s_request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    local_observer_enabled: bool = True
    observation_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    observation_batch_size: int = Field(default=50, ge=1, le=500)

    @field_validator(
        "engine_gpu_memory_mib",
        "engine_gpu_total_mib",
        "engine_gpu_memory_utilization",
        mode="before",
    )
    @classmethod
    def an_empty_gpu_number_is_unset(cls, value: object) -> object:
        """One spelling for "unset": a blank env value never parses as a number.

        ``docker compose`` and the Deployment manifest always pass the variable, so
        the blank string is what "the operator left it out" looks like on the way
        in. Treating it as a parse error would make a fresh deployment fail at
        startup on a value it never set; treating it as unset keeps the refusal at
        prepare, where the template asks for the GPU.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def prefixed_environment_names_a_setting(self) -> Settings:
        # A misspelled `SPEECHNEXUS_CONTROLLER_*` name is otherwise invisible: the
        # production identity check can only report the default it fell back to.
        refuse_unrecognised_prefixed_environment(
            Settings, "the node controller service", planes=PLANES
        )
        return self

    @model_validator(mode="after")
    def validate_kubernetes_driver_identity(self) -> Settings:
        """Require the Kubernetes identity exactly when that driver is selected.

        Configured-but-inert is its own failure: a namespace or ServiceAccount
        name next to ``RUNTIME=docker`` reads as a configured cluster target that
        nothing uses. Missing values are refused as well, because the Role is
        bound to one namespace and one ServiceAccount.
        """
        if self.runtime != KUBERNETES_RUNTIME:
            if self.k8s_namespace is not None or self.k8s_service_account_name is not None:
                raise ValueError(
                    "SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE and "
                    "SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME only apply to "
                    f"SPEECHNEXUS_CONTROLLER_RUNTIME={KUBERNETES_RUNTIME}"
                )
            return self
        for name, value in (
            ("SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE", self.k8s_namespace),
            (
                "SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME",
                self.k8s_service_account_name,
            ),
        ):
            if value is None:
                raise ValueError(
                    "the Kubernetes runtime driver requires both "
                    "SPEECHNEXUS_CONTROLLER_K8S_NAMESPACE and "
                    "SPEECHNEXUS_CONTROLLER_K8S_SERVICE_ACCOUNT_NAME"
                )
            if _DNS_LABEL.fullmatch(value) is None:
                raise ValueError(f"{name} must be a DNS-1123 label")
        return self

    @model_validator(mode="after")
    def validate_production_node_identity(self) -> Settings:
        """Require a configured production identity before opening node resources."""
        if self.environment == "production":
            if "node_id" not in self.model_fields_set or self.node_id == "node-local-1":
                raise ValueError("production requires an explicit non-default node_id")
            if self.node_id != self.node_id.strip():
                raise ValueError("production node_id must not have surrounding whitespace")
            try:
                require_identifier(self.node_id, "node_id")
            except InvalidRuntimeRequestError as error:
                raise ValueError("production node_id must be a valid runtime identifier") from error
        return self
