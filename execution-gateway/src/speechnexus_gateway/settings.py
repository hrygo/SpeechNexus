"""Validated execution gateway settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from SPEECHNEXUS_GATEWAY_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SPEECHNEXUS_GATEWAY_",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    service_name: str = Field(
        default="speechnexus-execution-gateway",
        min_length=1,
        max_length=64,
    )
    node_id: str = Field(default="node-local-1", pattern=r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
    gateway_id: str = Field(default="gateway-1", pattern=r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
    host: str = "127.0.0.1"
    port: int = Field(default=8010, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    control_database_url: SecretStr
    control_query_database_url: SecretStr | None = Field(default=None, repr=False)
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    component_token: SecretStr | None = Field(default=None, min_length=1)
    ingress_lock_directory: Path | None = None
    control_schema: str = Field(default="speech", pattern=r"^[a-z][a-z0-9_]{2,62}$")
    control_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    recovery_config_file: Path | None = Field(default=None, repr=False)
    recovery_node_component_token: SecretStr | None = Field(default=None, repr=False)
    file_asr_config_file: Path | None = Field(default=None, repr=False)
    file_asr_object_access_key: SecretStr | None = Field(default=None, min_length=1, repr=False)
    file_asr_object_secret_key: SecretStr | None = Field(default=None, min_length=1, repr=False)
    file_tts_config_file: Path | None = Field(default=None, repr=False)
    file_tts_object_access_key: SecretStr | None = Field(default=None, min_length=1, repr=False)
    file_tts_object_secret_key: SecretStr | None = Field(default=None, min_length=1, repr=False)
    file_voice_design_config_file: Path | None = Field(default=None, repr=False)
    file_voice_design_object_access_key: SecretStr | None = Field(
        default=None, min_length=1, repr=False
    )
    file_voice_design_object_secret_key: SecretStr | None = Field(
        default=None, min_length=1, repr=False
    )
    file_voice_clone_config_file: Path | None = Field(default=None, repr=False)
    file_voice_clone_object_access_key: SecretStr | None = Field(
        default=None, min_length=1, repr=False
    )
    file_voice_clone_object_secret_key: SecretStr | None = Field(
        default=None, min_length=1, repr=False
    )

    @model_validator(mode="after")
    def validate_control_configuration(self) -> Settings:
        if (self.component_token is None) != (self.ingress_lock_directory is None):
            raise ValueError(
                "execution control requires both a component token and a lock directory"
            )
        if self.control_query_database_url is not None and self.component_token is None:
            # Without execution control this DSN is never opened: the read-only
            # query pool belongs to the control lifespan. Accepting it alone would
            # leave an operator believing a reader identity is in use (#660).
            raise ValueError(
                "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL is configured but "
                "SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN is blank; the read-only query pool "
                "belongs to execution control, so configure the component token and ingress "
                "lock directory or unset the query DSN"
            )
        if self.ingress_lock_directory is not None:
            if not self.ingress_lock_directory.is_absolute():
                raise ValueError("ingress lock directory must be an absolute host-mounted path")
            if not {"node_id", "gateway_id"} <= self.model_fields_set:
                raise ValueError("execution control requires explicit node and gateway identities")
        if (self.recovery_config_file is None) != (self.recovery_node_component_token is None):
            raise ValueError("recovery requires both a configuration file and a node credential")
        if self.recovery_config_file is not None:
            if not self.execution_control_enabled or not self.recovery_config_file.is_absolute():
                raise ValueError(
                    "recovery requires execution control and an absolute configuration"
                )
            if self.recovery_node_component_token == self.component_token:
                raise ValueError("recovery requires an independent node credential")
        file_asr = (
            self.file_asr_config_file,
            self.file_asr_object_access_key,
            self.file_asr_object_secret_key,
        )
        if any(value is not None for value in file_asr):
            if not all(value is not None for value in file_asr):
                raise ValueError("file ASR requires a configuration and both object credentials")
            if (
                not self.execution_control_enabled
                or self.file_asr_config_file is None
                or not self.file_asr_config_file.is_absolute()
            ):
                raise ValueError("file ASR requires owned control and an absolute configuration")
        file_tts = (
            self.file_tts_config_file,
            self.file_tts_object_access_key,
            self.file_tts_object_secret_key,
        )
        if any(value is not None for value in file_tts):
            if not all(value is not None for value in file_tts):
                raise ValueError("file TTS requires a configuration and both object credentials")
            if (
                not self.execution_control_enabled
                or self.file_tts_config_file is None
                or not self.file_tts_config_file.is_absolute()
            ):
                raise ValueError("file TTS requires owned control and an absolute configuration")
        voice_design = (
            self.file_voice_design_config_file,
            self.file_voice_design_object_access_key,
            self.file_voice_design_object_secret_key,
        )
        if any(value is not None for value in voice_design):
            if not all(value is not None for value in voice_design):
                raise ValueError(
                    "file VoiceDesign requires a configuration and both object credentials"
                )
            if (
                not self.execution_control_enabled
                or self.file_voice_design_config_file is None
                or not self.file_voice_design_config_file.is_absolute()
            ):
                raise ValueError(
                    "file VoiceDesign requires owned control and an absolute configuration"
                )
        voice_clone = (
            self.file_voice_clone_config_file,
            self.file_voice_clone_object_access_key,
            self.file_voice_clone_object_secret_key,
        )
        if any(value is not None for value in voice_clone):
            if not all(value is not None for value in voice_clone):
                raise ValueError(
                    "file voice clone requires a configuration and both object credentials"
                )
            if (
                not self.execution_control_enabled
                or self.file_voice_clone_config_file is None
                or not self.file_voice_clone_config_file.is_absolute()
            ):
                raise ValueError(
                    "file voice clone requires owned control and an absolute configuration"
                )
        variants = (
            self.file_tts_config_file,
            self.file_voice_design_config_file,
            self.file_voice_clone_config_file,
        )
        if sum(value is not None for value in variants) > 1:
            raise ValueError("one gateway cannot serve more than one resident TTS variant")
        return self

    @property
    def execution_control_enabled(self) -> bool:
        return self.component_token is not None
