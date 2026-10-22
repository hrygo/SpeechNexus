"""Validated process configuration for the speech backend."""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from SPEECHNEXUS_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SPEECHNEXUS_",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    service_name: str = Field(default="speechnexus-api", min_length=1, max_length=64)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    database_url: SecretStr
    object_storage_endpoint: AnyHttpUrl
    object_storage_healthcheck_url: AnyHttpUrl
    object_storage_bucket: str = Field(min_length=3, max_length=63)
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
