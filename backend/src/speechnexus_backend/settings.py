"""Validated process configuration for the speech backend."""

from __future__ import annotations

from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProcessRole = Literal[
    "api",
    "realtime-gateway",
    "scheduler",
    "job-worker",
    "workbench-api",
    "workbench-export-worker",
]


class Settings(BaseSettings):
    """Runtime settings loaded from SPEECHNEXUS_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SPEECHNEXUS_",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    service_name: str = Field(default="speechnexus-api", min_length=1, max_length=64)
    process_role: ProcessRole = "api"
    node_id: str = Field(default="node-local-1", min_length=1, max_length=128)
    instance_id: str = Field(default="api-1", min_length=1, max_length=128)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    database_url: SecretStr
    object_storage_endpoint: AnyHttpUrl
    object_storage_healthcheck_url: AnyHttpUrl
    object_storage_bucket: str = Field(min_length=3, max_length=63)
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    database_schema: str = Field(default="speech", pattern=r"^[a-z][a-z0-9_]{2,62}$")
    object_storage_access_key: SecretStr | None = Field(default=None, min_length=1)
    object_storage_secret_key: SecretStr | None = Field(default=None, min_length=1)
    asset_default_ttl_seconds: int | None = Field(default=604800, ge=60, le=31536000)
    asset_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=4 * 1024 * 1024)
    asset_upload_concurrency: int = Field(default=4, ge=1, le=4)
    asset_receive_timeout_seconds: float = Field(default=30, gt=0, le=120)
    asset_maintenance_interval_seconds: float = Field(default=5, gt=0, le=300)
    asset_maintenance_batch_size: int = Field(default=10, ge=1, le=100)
    asset_allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    asset_allowed_origins: tuple[str, ...] = ()
    profile_writer_database_url: SecretStr | None = Field(default=None, min_length=1)
    profile_receive_max_bytes: int = Field(default=262144, ge=1, le=1048576)
    profile_receive_concurrency: int = Field(default=4, ge=1, le=4)
    profile_receive_timeout_seconds: float = Field(default=5, gt=0, le=30)
    profile_transaction_timeout_seconds: float = Field(default=3, gt=0, le=30)

    binding_writer_database_url: SecretStr | None = Field(default=None, min_length=1)
    binding_receive_max_bytes: int = Field(default=262144, ge=1, le=1048576)
    binding_receive_concurrency: int = Field(default=4, ge=1, le=4)
    binding_receive_timeout_seconds: float = Field(default=5, gt=0, le=30)
    binding_transaction_timeout_seconds: float = Field(default=3, gt=0, le=30)

    @field_validator("profile_writer_database_url", "binding_writer_database_url", mode="before")
    @classmethod
    def empty_profile_writer_is_disabled(cls, value: Any) -> Any:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        return None if isinstance(raw, str) and not raw.strip() else value

    @model_validator(mode="after")
    def asset_credentials_are_paired(self) -> Self:
        if (self.object_storage_access_key is None) != (self.object_storage_secret_key is None):
            raise ValueError("Configure both object-storage keys together")
        for host in self.asset_allowed_hosts:
            if not host or host != host.lower() or any(c in host for c in "*/@?# "):
                raise ValueError("Configure explicit lowercase asset hostnames")
            parsed = urlsplit("//" + (f"[{host}]" if ":" in host else host))
            if not parsed.hostname or parsed.port is not None or parsed.path:
                raise ValueError("Configure asset hostnames without a scheme or port")
        for origin in self.asset_allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Configure explicit HTTP origins without a path or credentials")
        return self
