"""Operational HTTP response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Liveness response."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: str
    version: str


class ReadinessCheckResponse(BaseModel):
    """One dependency's readiness state."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["ok", "failed"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness response with stable aggregate and per-check states."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "unready"]
    checks: list[ReadinessCheckResponse]
