"""Validated process configuration for the restricted node controller.

The controller runs on one node and is useless without a component credential,
an approved-template file, and a durable intent store, so those have no
defaults: a half-configured controller fails at startup instead of silently
serving intents it cannot act on or remember.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_node_controller.domain.intents import (
    InvalidRuntimeRequestError,
    require_identifier,
)
from speechnexus_node_controller.environment import (
    PLANES,
    refuse_unrecognised_prefixed_environment,
)


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
    docker_socket_path: Path = Path("/var/run/docker.sock")
    docker_request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    stop_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    exit_wait_seconds: float = Field(default=30.0, gt=0, le=600)
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    local_observer_enabled: bool = True
    observation_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    observation_batch_size: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def prefixed_environment_names_a_setting(self) -> Settings:
        # A misspelled `SPEECHNEXUS_CONTROLLER_*` name is otherwise invisible: the
        # production identity check can only report the default it fell back to.
        refuse_unrecognised_prefixed_environment(
            Settings, "the node controller service", planes=PLANES
        )
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
