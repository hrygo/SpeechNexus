"""Load the private admission recipe that makes synchronous ASR executable.

The public binding document names a logical model and its parameter domain. It
does not carry the private binding/manifest revision, runtime plan or engine
parameters that authorize an execution. Those stay in one operator-owned file,
read with the same bounded/private/duplicate-rejecting rules as the gateway's
own deployment files. This module validates the file; it does not grant
capacity or publish a binding.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from speechnexus_backend.speech.domain.admission import (
    ApprovedAdmissionRecipe,
    ApprovedStage,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeBinding,
)

MAX_RECIPE_BYTES = 65536
_DIGEST = r"sha256:[0-9a-f]{64}"


def private_file(path: Path, limit: int) -> bytes:
    """Read one owner-only regular file without following links or growing reads."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not 0 < info.st_size <= limit
        ):
            raise ValueError
        content = source.read(limit + 1)
        if not 0 < len(content) <= limit:
            raise ValueError
        return content


def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def nonfinite(_: str) -> Any:
    raise ValueError


class _Private(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class _Physical(_Private):
    unified_physical_memory_bytes: int = Field(ge=0)
    device_memory_bytes: int = Field(ge=0)
    host_memory_bytes: int = Field(ge=0)
    cpu_decode_millicores: int = Field(ge=0)
    host_pcm_buffer_bytes: int = Field(ge=0)
    scratch_disk_bytes: int = Field(ge=0)

    def domain(self) -> PhysicalResources:
        return PhysicalResources(
            self.unified_physical_memory_bytes,
            self.device_memory_bytes,
            self.host_memory_bytes,
            self.cpu_decode_millicores,
            self.host_pcm_buffer_bytes,
            self.scratch_disk_bytes,
        )


class _Binding(_Private):
    target_node_id: str
    pool_id: str
    gateway_id: str
    approved_template_id: str
    binding_revision_id: str
    manifest_id: str
    config_digest: str

    def domain(self) -> RuntimeBinding:
        return RuntimeBinding(
            target_node_id=self.target_node_id,
            pool_id=self.pool_id,
            gateway_id=self.gateway_id,
            approved_template_id=self.approved_template_id,
            binding_revision_id=self.binding_revision_id,
            manifest_id=self.manifest_id,
            config_digest=self.config_digest,
        )


class _RuntimePlan(_Private):
    binding: _Binding
    topology_digest: str
    qualification_digest: str
    memory_architecture: Literal["unified_memory", "discrete_device_memory"]
    service_class: Literal["realtime", "interactive", "batch"]
    loading_policy: Literal["resident", "on_demand"]
    max_slots: int = Field(ge=1)
    realtime_reserved_slots: int = Field(ge=0)
    peak: _Physical
    steady: _Physical
    per_slot: _Physical

    def domain(self) -> ApprovedRuntimePlan:
        return ApprovedRuntimePlan(
            binding=self.binding.domain(),
            topology_digest=self.topology_digest,
            qualification_digest=self.qualification_digest,
            memory_architecture=self.memory_architecture,
            service_class=self.service_class,
            loading_policy=self.loading_policy,
            max_slots=self.max_slots,
            realtime_reserved_slots=self.realtime_reserved_slots,
            peak=self.peak.domain(),
            steady=self.steady.domain(),
            per_slot=self.per_slot.domain(),
        )


class _Stage(_Private):
    stage_id: str
    instance_id: str
    minimum_slots: int = Field(ge=1)
    reserved_slots: int = Field(ge=1)


class _Asr(_Private):
    silence_duration_ms: int = Field(ge=0, le=30_000)
    preview_interval_ms: int = Field(ge=100, le=5_000)
    temperature: float = Field(ge=0, le=1)
    final_deadline_ms: int = Field(ge=100, le=10_000)
    auxiliary_deadline_ms: int = Field(ge=100, le=10_000)


class _File(_Private):
    protocol: Literal["transcription_admission_v1"]
    model_id: str
    binding_publication_id: str
    binding_revision_id: str
    manifest_id: str
    recipe_digest: str = Field(pattern=_DIGEST)
    policy_revision_id: str
    service_class: Literal["realtime", "interactive", "batch"]
    admission_expires_at_us: int = Field(ge=1)
    quota_dimensions: list[str] = Field(min_length=1, max_length=8)
    stage: _Stage
    runtime_plan: _RuntimePlan
    asr: _Asr


@dataclass(frozen=True, slots=True)
class AsrParameters:
    """The engine parameters one private recipe fixes for every file ASR."""

    silence_duration_ms: int
    preview_interval_ms: int
    temperature: float
    auxiliary_deadline_ms: int
    final_deadline_ms: int

    def __post_init__(self) -> None:
        if type(self.silence_duration_ms) is not int or not 0 <= self.silence_duration_ms <= 30_000:
            raise ValueError("Invalid ASR silence duration")
        if (
            type(self.preview_interval_ms) is not int
            or not 100 <= self.preview_interval_ms <= 5_000
        ):
            raise ValueError("Invalid ASR preview interval")
        if type(self.temperature) not in (int, float) or not 0 <= self.temperature <= 1:
            raise ValueError("Invalid ASR temperature")
        if (
            type(self.auxiliary_deadline_ms) is not int
            or not 100 <= self.auxiliary_deadline_ms <= 10_000
        ):
            raise ValueError("Invalid ASR auxiliary deadline")
        if type(self.final_deadline_ms) is not int or not 100 <= self.final_deadline_ms <= 10_000:
            raise ValueError("Invalid ASR final deadline")


@dataclass(frozen=True, slots=True)
class TranscriptionRecipe:
    """One private model-to-recipe association used by the synchronous adapter."""

    model_id: str
    binding_publication_id: str
    stage_id: str
    instance_id: str
    reserved_slots: int
    asr: AsrParameters
    approved: ApprovedAdmissionRecipe


def load_transcription_recipe(path: Path) -> TranscriptionRecipe:
    """Read one owner-only recipe file; all ambiguity is an explicit failure."""
    try:
        if not path.is_absolute():
            raise ValueError
        document = json.loads(
            private_file(path, MAX_RECIPE_BYTES),
            object_pairs_hook=unique_pairs,
            parse_constant=nonfinite,
        )
        recipe = _File.model_validate(document)
        _verify_digest(document, recipe.recipe_digest)
        if recipe.stage.reserved_slots < recipe.stage.minimum_slots:
            raise ValueError
        if recipe.runtime_plan.binding.binding_revision_id != recipe.binding_revision_id:
            raise ValueError
        if recipe.runtime_plan.binding.manifest_id != recipe.manifest_id:
            raise ValueError
        if recipe.service_class != recipe.runtime_plan.service_class:
            raise ValueError
        if recipe.quota_dimensions != ["concurrent_attempts"]:
            raise ValueError
        stage = ApprovedStage(
            recipe.stage.stage_id, recipe.stage.minimum_slots, recipe.runtime_plan.domain()
        )
        approved = ApprovedAdmissionRecipe(
            binding_revision_id=recipe.binding_revision_id,
            manifest_id=recipe.manifest_id,
            recipe_digest=recipe.recipe_digest,
            policy_revision_id=recipe.policy_revision_id,
            service_class=recipe.service_class,
            stages=(stage,),
            admission_expires_at_us=recipe.admission_expires_at_us,
            quota_dimensions=frozenset(recipe.quota_dimensions),
        )
        return TranscriptionRecipe(
            model_id=recipe.model_id,
            binding_publication_id=recipe.binding_publication_id,
            stage_id=recipe.stage.stage_id,
            instance_id=recipe.stage.instance_id,
            reserved_slots=recipe.stage.reserved_slots,
            asr=AsrParameters(
                silence_duration_ms=recipe.asr.silence_duration_ms,
                preview_interval_ms=recipe.asr.preview_interval_ms,
                temperature=recipe.asr.temperature,
                auxiliary_deadline_ms=recipe.asr.auxiliary_deadline_ms,
                final_deadline_ms=recipe.asr.final_deadline_ms,
            ),
            approved=approved,
        )
    except ValueError, TypeError, KeyError, UnicodeError, RecursionError:
        raise ValueError("Invalid private transcription admission recipe") from None


def _verify_digest(document: Any, declared: str) -> None:
    if not isinstance(document, dict):
        raise ValueError
    body = {key: value for key, value in document.items() if key != "recipe_digest"}
    actual = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    if actual != declared:
        raise ValueError


__all__ = [
    "MAX_RECIPE_BYTES",
    "AsrParameters",
    "TranscriptionRecipe",
    "load_transcription_recipe",
    "private_file",
    "unique_pairs",
]
