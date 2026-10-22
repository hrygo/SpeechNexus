"""Validated execution gateway settings."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from SPEECHNEXUS_GATEWAY_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SPEECHNEXUS_GATEWAY_",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    service_name: str = Field(
        default="speechnexus-execution-gateway",
        min_length=1,
        max_length=64,
    )
    node_id: str = Field(default="node-local-1", min_length=1, max_length=128)
    gateway_id: str = Field(default="gateway-1", min_length=1, max_length=128)
    host: str = "127.0.0.1"
    port: int = Field(default=8010, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    control_database_url: SecretStr
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
