"""Validated process configuration for the speech backend."""

from __future__ import annotations

from pathlib import Path
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
    # The default text inspector keeps asr_input/voice_reference unavailable; the
    # audio kind requires a configured HTTP inspector and never falls back to text.
    # Audio bytes are decoded by the separate `asset-inspector` deployment unit, so
    # this process holds only the request policy it revalidates locally.
    asset_inspector: Literal["text", "audio"] = "text"
    asset_inspection_url: AnyHttpUrl | None = None
    asset_inspection_token: SecretStr | None = Field(default=None, min_length=1)
    asset_inspection_timeout_seconds: float = Field(default=15, gt=0, le=60)
    asset_inspection_max_response_bytes: int = Field(default=4096, ge=1, le=65536)
    # Retained API-side policy for the audio inspector: the duration ceiling, the
    # local byte ceiling, the decode budget sent as a request header, and the
    # receipt bound. The remaining eight keys have no consumer left: the five
    # local process selectors and `audio_inspection_memory_bytes`, `_cpu_seconds`
    # and `_output_bytes` are declared so a stale environment is refused at
    # startup instead of being silently ignored (see `_RETIRED_PREREQUISITES` and
    # `_MIGRATED_LIMITS` in `asset_runtime.py`). They stay declared for that
    # diagnostic value alone.
    audio_inspection_runtime_root: Path | None = None
    audio_inspection_command: str = ""
    audio_inspection_launcher_python: str = ""
    audio_inspection_bubblewrap: Path | None = None
    audio_inspection_prlimit: Path | None = None
    audio_inspection_max_duration_ms: int = Field(default=120_000, ge=1, le=86_400_000)
    # A reference clip is a different asset from one ASR utterance, so its media
    # ceiling is configured separately. The clone policy the gateway enforces
    # admits 1-60 s of reference audio; this ceiling spans exactly that range, so
    # an upload can never become a `ready` asset that every clone call refuses.
    # An operator may narrow it, never widen it.
    audio_inspection_max_reference_duration_ms: int = Field(default=60_000, ge=1_000, le=60_000)
    audio_inspection_max_input_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=1024**3)
    audio_inspection_memory_bytes: int = Field(default=256 * 1024**2, ge=64 * 1024**2, le=1024**3)
    audio_inspection_cpu_seconds: int = Field(default=5, ge=1, le=30)
    audio_inspection_wall_seconds: float = Field(default=10, gt=0, le=60)
    audio_inspection_output_bytes: int = Field(default=4096, ge=1, le=65536)
    audio_inspection_max_receipt_bytes: int = Field(default=4096, ge=1, le=65536)
    profile_writer_database_url: SecretStr | None = Field(default=None, min_length=1)
    profile_receive_max_bytes: int = Field(default=262144, ge=1, le=1048576)
    profile_receive_concurrency: int = Field(default=4, ge=1, le=4)
    profile_receive_timeout_seconds: float = Field(default=5, gt=0, le=30)
    profile_transaction_timeout_seconds: float = Field(default=3, gt=0, le=30)

    model_catalog_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    model_catalog_read_concurrency: int = Field(default=2, ge=1, le=4)
    model_catalog_read_timeout_seconds: float = Field(default=3, gt=0, le=30)

    artifact_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    artifact_read_timeout_seconds: float = Field(default=3, gt=0, le=30)

    resource_pool_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    resource_pool_read_concurrency: int = Field(default=2, ge=1, le=4)
    resource_pool_read_timeout_seconds: float = Field(default=3, gt=0, le=30)

    binding_list_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    binding_list_read_concurrency: int = Field(default=2, ge=1, le=4)
    binding_list_read_timeout_seconds: float = Field(default=3, gt=0, le=30)

    audit_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    audit_read_concurrency: int = Field(default=2, ge=1, le=4)
    audit_read_timeout_seconds: float = Field(default=3, gt=0, le=30)

    model_instance_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    model_instance_read_concurrency: int = Field(default=2, ge=1, le=4)
    model_instance_read_timeout_seconds: float = Field(default=3, gt=0, le=30)

    workspace_writer_database_url: SecretStr | None = Field(default=None, min_length=1)

    capability_default_workspace_id: str = Field(
        default="workspace-default", pattern=r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$"
    )
    capability_policy_revision: int = Field(default=1, ge=1, le=9_999_999_999_999_999_999)
    capability_max_text_codepoints: int = Field(default=1_000_000, ge=1, le=1_000_000)
    capability_max_segment_ms: int = Field(default=30_000, ge=1000, le=30_000)
    capability_max_realtime_session_ms: int = Field(default=600_000, ge=60_000)
    capability_max_queue_wait_ms: int = Field(default=600_000, ge=0)

    pronunciation_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    pronunciation_read_concurrency: int = Field(default=4, ge=1, le=4)
    pronunciation_read_timeout_seconds: float = Field(default=3, gt=0, le=30)
    pronunciation_writer_database_url: SecretStr | None = Field(default=None, min_length=1)
    pronunciation_publisher_database_url: SecretStr | None = Field(default=None, min_length=1)
    pronunciation_write_concurrency: int = Field(default=4, ge=1, le=4)
    pronunciation_receive_max_bytes: int = Field(
        default=32 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024
    )
    pronunciation_receive_timeout_seconds: float = Field(default=30, gt=0, le=120)
    pronunciation_transaction_timeout_seconds: float = Field(default=3, gt=0, le=30)

    binding_writer_database_url: SecretStr | None = Field(default=None, min_length=1)
    binding_defaults_writer_database_url: SecretStr | None = Field(default=None, min_length=1)
    binding_admin_reader_database_url: SecretStr | None = Field(default=None, min_length=1)
    binding_lifecycle_database_url: SecretStr | None = Field(default=None, min_length=1)
    binding_receive_max_bytes: int = Field(default=262144, ge=1, le=1048576)
    binding_receive_concurrency: int = Field(default=4, ge=1, le=4)
    binding_receive_timeout_seconds: float = Field(default=5, gt=0, le=30)
    binding_transaction_timeout_seconds: float = Field(default=3, gt=0, le=30)

    job_writer_database_url: SecretStr | None = Field(default=None, min_length=1)
    job_admission_database_url: SecretStr | None = Field(default=None, min_length=1)
    voice_publisher_database_url: SecretStr | None = Field(default=None, min_length=1)
    job_receive_max_bytes: int = Field(default=262144, ge=1, le=1048576)
    job_receive_timeout_seconds: float = Field(default=5, gt=0, le=30)
    job_transaction_timeout_seconds: float = Field(default=3, gt=0, le=30)
    job_event_heartbeat_seconds: float = Field(default=15, gt=0, le=60)
    job_event_send_timeout_seconds: float = Field(default=10, gt=0, le=60)

    execution_gateway_base_url: AnyHttpUrl | None = None
    execution_gateway_component_credential: SecretStr | None = Field(default=None, min_length=1)
    execution_gateway_timeout_seconds: float = Field(default=10, gt=0, le=600)
    execution_gateway_max_retries: int = Field(default=2, ge=0, le=5)
    execution_gateway_backoff_seconds: float = Field(default=0.25, ge=0, le=10)
    execution_gateway_max_response_bytes: int = Field(default=1_048_576, ge=1024, le=4_194_304)

    transcription_recipe_file: Path | None = None
    transcription_input_bucket: str | None = Field(default=None, min_length=3, max_length=63)
    transcription_poll_seconds: float = Field(default=0.25, gt=0, le=30)
    transcription_max_audio_ms: int = Field(default=120_000, ge=100, le=3_600_000)
    transcription_max_upload_bytes: int = Field(
        default=25 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024
    )
    transcription_timeout_seconds: float = Field(default=120, gt=0, le=600)
    transcription_max_command_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    transcription_storage_timeout_seconds: float = Field(default=3, gt=0, le=30)

    speech_recipe_file: Path | None = None
    speech_settlement_database_url: SecretStr | None = Field(default=None, min_length=1)
    speech_default_language: str | None = Field(
        default=None, pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,4}$"
    )
    speech_poll_seconds: float = Field(default=0.25, gt=0, le=30)
    speech_timeout_seconds: float = Field(default=120, gt=0, le=600)
    speech_storage_timeout_seconds: float = Field(default=3, gt=0, le=30)
    speech_max_command_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    speech_max_output_audio_ms: int = Field(default=120_000, ge=100, le=3_600_000)
    speech_max_payload_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)

    @field_validator(
        "profile_writer_database_url",
        "model_catalog_reader_database_url",
        "artifact_reader_database_url",
        "resource_pool_reader_database_url",
        "binding_list_reader_database_url",
        "audit_reader_database_url",
        "model_instance_reader_database_url",
        "workspace_writer_database_url",
        "pronunciation_reader_database_url",
        "pronunciation_writer_database_url",
        "pronunciation_publisher_database_url",
        "binding_writer_database_url",
        "binding_defaults_writer_database_url",
        "binding_admin_reader_database_url",
        "binding_lifecycle_database_url",
        "job_writer_database_url",
        "job_admission_database_url",
        "voice_publisher_database_url",
        "execution_gateway_component_credential",
        "transcription_recipe_file",
        "transcription_input_bucket",
        "speech_recipe_file",
        "speech_settlement_database_url",
        "speech_default_language",
        "audio_inspection_runtime_root",
        "audio_inspection_bubblewrap",
        "audio_inspection_prlimit",
        "asset_inspection_url",
        "asset_inspection_token",
        mode="before",
    )
    @classmethod
    def empty_profile_writer_is_disabled(cls, value: Any) -> Any:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        return None if isinstance(raw, str) and not raw.strip() else value

    @field_validator("asset_inspection_url")
    @classmethod
    def inspection_url_carries_no_credentials(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        """Keep the inspector bearer in its own secret field, never in the URL."""
        if value is not None and (value.username or value.password):
            raise ValueError(
                "SPEECHNEXUS_ASSET_INSPECTION_URL must not embed credentials; "
                "set SPEECHNEXUS_ASSET_INSPECTION_TOKEN"
            )
        return value

    @field_validator("asset_inspector", mode="before")
    @classmethod
    def empty_asset_inspector_is_text(cls, value: Any) -> Any:
        return "text" if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def asset_credentials_are_paired(self) -> Self:
        if (
            self.binding_writer_database_url is not None
            and self.binding_defaults_writer_database_url is not None
        ):
            raise ValueError("Configure one explicit binding writer mode")
        if (self.object_storage_access_key is None) != (self.object_storage_secret_key is None):
            raise ValueError("Configure both object-storage keys together")
        if (self.execution_gateway_base_url is None) != (
            self.execution_gateway_component_credential is None
        ):
            raise ValueError("Configure both execution-gateway URL and credential together")
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
