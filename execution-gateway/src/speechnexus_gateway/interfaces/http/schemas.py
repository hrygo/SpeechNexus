"""Operational gateway response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: str
    version: str


class ReadinessCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["ok", "failed"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "unready"]
    checks: list[ReadinessCheckResponse]
