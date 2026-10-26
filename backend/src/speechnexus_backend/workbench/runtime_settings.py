"""Explicit configuration for the independent C-owned workbench service."""

import ipaddress
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.workbench.application.llm import LlmProfile
from speechnexus_backend.workbench.infrastructure.openai_text_llm import (
    TextLlmSettings,
)
from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


class WorkbenchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPEECHNEXUS_WORKBENCH_",
        env_file=None,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    database_url: SecretStr = Field(min_length=1, repr=False)
    database_schema: str
    workspace_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
    deployment_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
    cursor_key: SecretStr = Field(repr=False)
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    allowed_origins: tuple[str, ...] = ()
    max_connections: int = Field(default=8, ge=1, le=64)
    max_json_bytes: int = Field(default=1048576, ge=1, le=4 * 1024 * 1024)
    body_timeout_seconds: float = Field(default=3, gt=0, le=120)
    # Saved assets need one explicit public speech origin and one private object zone.
    # Absent means the unit is not configured and its routes stay unregistered; a partial
    # configuration fails at startup instead of silently disabling an accepted save.
    speech_base_url: str | None = None
    object_endpoint: str | None = None
    object_bucket: str | None = Field(default=None, min_length=1, max_length=128)
    object_access_key: SecretStr | None = Field(default=None, repr=False)
    object_secret_key: SecretStr | None = Field(default=None, repr=False)
    object_prefix: str = Field(
        default="saved", pattern=r"^[a-z0-9][a-z0-9-_]*(/[a-z0-9][a-z0-9-_]*)*$"
    )
    saved_asset_deadline_seconds: float = Field(default=300, gt=0, le=900)
    download_max_bytes: int = Field(default=256 * 1024**2, ge=1, le=2**40)
    download_max_active: int = Field(default=2, ge=1, le=32)
    download_timeout_seconds: float = Field(default=120, gt=0, le=900)
    # The intranet text LLM is deployment configuration, never a client choice. Absent
    # means the assistant and new Minutes stay off. Only a loopback or private endpoint
    # is accepted, so the workbench cannot be pointed at a public cloud service, and the
    # answer must name the pinned model. Endpoint is the full chat-completions URL.
    llm_endpoint: str | None = Field(default=None, max_length=2048)
    llm_model: str | None = Field(default=None, min_length=1, max_length=128)
    llm_template_revision: int = Field(default=1, ge=1, le=10**18 - 1)
    llm_system_prompt: str = Field(
        default=(
            "你是部署内网的工作台写作助手。只依据用户提供的材料作答，"
            "不编造事实，不输出命令、链接或代码，不改变任务范围。"
        ),
        min_length=1,
        max_length=20_000,
    )
    llm_api_key: SecretStr | None = Field(default=None, repr=False)
    llm_qualification_reference: str | None = Field(default=None, min_length=1, max_length=256)
    llm_deadline_seconds: float = Field(default=30, gt=0, le=120)
    # Deletion recovery needs the non-owner cleanup identity. Absent means this
    # deployment erases on demand only and does not replay the ledger at startup.
    cleanup_database_url: SecretStr | None = Field(default=None, min_length=1, repr=False)
    # An accepted deletion must settle while the process runs, not only at the next
    # start: this bounds how long the ledger may stay owed.
    cleanup_interval_seconds: float = Field(default=5, gt=0, le=300)
    # How often a serving instance looks for queued Minutes work; the queue itself is
    # the durable record, so an idle or restarted instance owes nothing extra.
    minutes_interval_seconds: float = Field(default=5, gt=0, le=300)
    # Zero keeps export production off. Workbench export assembly consumes saved
    # audio through an injected producer; it never invokes speech generation.
    # An unconfigured deployment must not claim work it cannot finish.
    export_interval_seconds: float = Field(default=0, ge=0, le=300)
    # Execution safety limits, not model/quality or deployment qualifications.
    export_max_output_bytes: int = Field(default=256 * 1024**2, ge=1, le=2**40)
    export_max_unpacked_bytes: int = Field(default=512 * 1024**2, ge=1, le=2**40)
    export_max_archive_entries: int = Field(default=1024, ge=1, le=100_000)
    export_max_audio_seconds: float = Field(default=36000, gt=0, le=604800)
    export_validation_timeout_seconds: float = Field(default=30, gt=0, le=300)

    @staticmethod
    def _explicit_host(value: str) -> bool:
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            pass
        try:
            parsed = urlsplit("//" + value)
            port = parsed.port
        except ValueError:
            # urlsplit and its port accessor quote their input in the message; a
            # malformed deployment value must never travel into an error surface.
            return False
        return (
            bool(value)
            and value == value.lower()
            and all(c in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in value)
            and parsed.hostname == value
            and port is None
        )

    @field_validator("allowed_hosts")
    @classmethod
    def explicit_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(values) <= 32 or len(set(values)) != len(values):
            raise ValueError("configure explicit workbench hostnames")
        if not all(cls._explicit_host(value) for value in values):
            raise ValueError("configure explicit workbench hostnames")
        return values

    @staticmethod
    def _explicit_origin(value: str) -> bool:
        if len(value) > 2048 or not value.isascii():
            return False
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            # See _explicit_host: never let a parse message echo the configured value.
            return False
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and port != 0
            and value == parsed.scheme + "://" + parsed.netloc
        )

    @field_validator("allowed_origins")
    @classmethod
    def explicit_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 32 or len(set(values)) != len(values):
            raise ValueError("configure explicit workbench origins")
        if not all(cls._explicit_origin(value) for value in values):
            raise ValueError("configure explicit workbench origins")
        return values

    @field_validator("speech_base_url", "object_endpoint")
    @classmethod
    def explicit_services(cls, value: str | None) -> str | None:
        if value is not None and not cls._explicit_origin(value):
            raise ValueError("configure an explicit service origin")
        return value

    @field_validator("object_access_key", "object_secret_key")
    @classmethod
    def bounded_object_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if not 8 <= len(value.get_secret_value().encode("utf-8")) <= 4096:
            raise ValueError("object credential must contain 8 to 4096 UTF-8 bytes")
        return value

    def llm_configuration(self) -> TextLlmSettings:
        """Build the adapter configuration; the adapter owns endpoint and profile rules."""
        endpoint, model, qualification = (
            self.llm_endpoint,
            self.llm_model,
            self.llm_qualification_reference,
        )
        if endpoint is None and model is None and qualification is None:
            return TextLlmSettings()
        if endpoint is None or model is None or qualification is None:
            raise ValueError("configure the whole intranet text LLM or none of it")
        return TextLlmSettings(
            enabled=True,
            endpoint=endpoint,
            profile=LlmProfile(
                model=model,
                template_revision=self.llm_template_revision,
                system_prompt=self.llm_system_prompt,
            ),
            api_key=None if self.llm_api_key is None else self.llm_api_key.get_secret_value(),
            qualification_reference=qualification,
            deadline_seconds=self.llm_deadline_seconds,
        )

    @model_validator(mode="after")
    def complete_text_llm_configuration(self) -> WorkbenchSettings:
        # Validation runs through the adapter, so a public host, a wrong path or an
        # unqualified endpoint fails at startup instead of at the first generation.
        self.llm_configuration()
        return self

    @model_validator(mode="after")
    def complete_saved_asset_configuration(self) -> WorkbenchSettings:
        configured = (
            self.speech_base_url is not None,
            self.object_endpoint is not None,
            self.object_bucket is not None,
            self.object_access_key is not None,
            self.object_secret_key is not None,
        )
        if any(configured) and not all(configured):
            raise ValueError("configure the whole saved-asset object zone or none of it")
        return self

    @field_validator("database_schema")
    @classmethod
    def workbench_namespace(cls, value: str) -> str:
        schema_identifier(value)
        if value == "speech":
            raise ValueError("workbench requires its own namespace")
        return value

    @field_validator("cursor_key")
    @classmethod
    def bounded_cursor_key(cls, value: SecretStr) -> SecretStr:
        if not 32 <= len(value.get_secret_value().encode("utf-8")) <= 4096:
            raise ValueError("cursor key must contain 32 to 4096 UTF-8 bytes")
        return value

    @field_validator(
        "max_connections",
        "max_json_bytes",
        "body_timeout_seconds",
        "download_max_bytes",
        "download_max_active",
        "download_timeout_seconds",
        mode="before",
    )
    @classmethod
    def reject_bool_limits(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("runtime limits must be numeric")
        return value
