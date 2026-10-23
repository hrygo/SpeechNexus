"""Operational HTTP response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Liveness response."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    status: Literal["ok"] = "ok"
    service: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1)


class ReadinessCheckResponse(BaseModel):
    """One dependency's readiness state."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    name: str = Field(min_length=1)
    status: Literal["ok", "failed"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness response with stable aggregate and per-check states."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    status: Literal["ready", "unready"]
    checks: list[ReadinessCheckResponse] = Field(min_length=1)
