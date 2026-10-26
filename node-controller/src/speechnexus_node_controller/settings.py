"""Validated process configuration for the restricted node controller.

The controller runs on one node and is useless without a component credential,
an approved-template file, and a durable intent store, so those have no
defaults: a half-configured controller fails at startup instead of silently
serving intents it cannot act on or remember.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from SPEECHNEXUS_CONTROLLER_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SPEECHNEXUS_CONTROLLER_",
        extra="ignore",
        frozen=True,
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
