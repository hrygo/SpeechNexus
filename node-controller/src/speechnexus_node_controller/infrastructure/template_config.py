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
    ApprovedRuntimeTemplate,
    ReadinessKind,
    ReadinessRule,
    ReadOnlyBind,
)


class _ReadinessRuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ReadinessKind
    port: int | None = Field(default=None, ge=1, le=65535)


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
    readiness: _ReadinessRuleModel
    command: list[str] | None = None
    gpu_device_count: int = Field(default=0, ge=0)
    read_only_binds: list[_ReadOnlyBindModel] = Field(default_factory=list)


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
        readiness=ReadinessRule(
            kind=model.readiness.kind,
            port=model.readiness.port,
        ),
        command=tuple(model.command) if model.command is not None else None,
        gpu_device_count=model.gpu_device_count,
        read_only_binds=tuple(
            ReadOnlyBind(source=bind.source, target=bind.target) for bind in model.read_only_binds
        ),
    )
