"""Load the approved runtime templates from the deployed configuration file.

The file is part of the deployment artifact, not of a request: it is the only
place where an image, argument list, readiness rule, GPU request, or read-only
artifact directory can be declared, and the loader refuses unknown keys so a
typo cannot silently widen what the node controller may run.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from speechnexus_node_controller.domain.intents import MemoryArchitecture
from speechnexus_node_controller.domain.templates import (
    MIN_CONTAINER_MEMORY_BYTES,
    ApprovedRuntimeTemplate,
    EngineAuthKind,
    ReadinessKind,
    ReadinessRule,
    ReadOnlyBind,
    WarmupRequest,
)


class _WarmupRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content_type: str
    body_path: str
    body_sha256: str
    attempts: int = Field(default=2, ge=1, le=4)
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_response_bytes: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)


class _ReadinessRuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ReadinessKind
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = None
    warmup: _WarmupRequestModel | None = None


class _ReadOnlyBindModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str


class _TemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    image_reference: str
    image_digest: str
    memory_architecture: MemoryArchitecture
    allowed_pool_ids: list[str] = Field(min_length=1)
    max_peak_memory_bytes: int = Field(gt=0)
    container_memory_limit_bytes: int = Field(ge=MIN_CONTAINER_MEMORY_BYTES, strict=True)
    readiness: _ReadinessRuleModel
    command: list[str] | None = None
    gpu_device_count: int = Field(default=0, ge=0)
    read_only_binds: list[_ReadOnlyBindModel] = Field(default_factory=list)
    network_name: str | None = None
    engine_auth: EngineAuthKind = EngineAuthKind.NONE


class _TemplateFileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates: list[_TemplateModel] = Field(min_length=1)


def load_approved_templates(path: Path) -> tuple[ApprovedRuntimeTemplate, ...]:
    """Read and validate the approved template file."""
    document = _TemplateFileModel.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return tuple(_to_template(template) for template in document.templates)


def _to_template(model: _TemplateModel) -> ApprovedRuntimeTemplate:
    return ApprovedRuntimeTemplate(
        template_id=model.template_id,
        image_reference=model.image_reference,
        image_digest=model.image_digest,
        memory_architecture=model.memory_architecture,
        allowed_pool_ids=frozenset(model.allowed_pool_ids),
        max_peak_memory_bytes=model.max_peak_memory_bytes,
        container_memory_limit_bytes=model.container_memory_limit_bytes,
        readiness=ReadinessRule(
            kind=model.readiness.kind,
            port=model.readiness.port,
            path=model.readiness.path,
            warmup=(
                WarmupRequest(
                    path=model.readiness.warmup.path,
                    content_type=model.readiness.warmup.content_type,
                    body_path=model.readiness.warmup.body_path,
                    body_sha256=model.readiness.warmup.body_sha256,
                    attempts=model.readiness.warmup.attempts,
                    timeout_seconds=model.readiness.warmup.timeout_seconds,
                    max_response_bytes=model.readiness.warmup.max_response_bytes,
                )
                if model.readiness.warmup is not None
                else None
            ),
        ),
        command=tuple(model.command) if model.command is not None else None,
        gpu_device_count=model.gpu_device_count,
        read_only_binds=tuple(
            ReadOnlyBind(source=bind.source, target=bind.target) for bind in model.read_only_binds
        ),
        network_name=model.network_name,
        engine_auth=model.engine_auth,
    )
