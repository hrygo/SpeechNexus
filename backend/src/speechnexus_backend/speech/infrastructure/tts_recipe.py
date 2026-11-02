"""Load bounded private allocation inputs; publication and qualification remain authoritative."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from speechnexus_backend.speech.domain.admission import ApprovedAdmissionRecipe, ApprovedStage
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    _DIGEST,
    MAX_RECIPE_BYTES,
    _Private,
    _RuntimePlan,
    _Stage,
    _verify_digest,
    nonfinite,
    private_file,
    unique_pairs,
)
from speechnexus_backend.speech.infrastructure.tts_admission import TtsRecipe


class _TtsFile(_Private):
    protocol: Literal["tts_admission_v1"]
    model_id: str
    binding_revision_id: str
    manifest_id: str
    recipe_digest: str = Field(pattern=_DIGEST)
    policy_revision_id: str
    service_class: Literal["realtime", "interactive", "batch"]
    admission_expires_at_us: int = Field(ge=1)
    quota_dimensions: list[str] = Field(min_length=1, max_length=8)
    stage: _Stage
    runtime_plan: _RuntimePlan


def load_tts_recipe(path: Path) -> TtsRecipe:
    """Private configuration only supplies allocation; it never qualifies a voice."""
    try:
        if not path.is_absolute():
            raise ValueError
        document = json.loads(
            private_file(path, MAX_RECIPE_BYTES),
            object_pairs_hook=unique_pairs,
            parse_constant=nonfinite,
        )
        recipe = _TtsFile.model_validate(document)
        _verify_digest(document, recipe.recipe_digest)
        if (
            recipe.stage.reserved_slots < recipe.stage.minimum_slots
            or recipe.runtime_plan.binding.binding_revision_id != recipe.binding_revision_id
            or recipe.runtime_plan.binding.manifest_id != recipe.manifest_id
            or recipe.service_class != recipe.runtime_plan.service_class
            or recipe.quota_dimensions != ["concurrent_attempts"]
        ):
            raise ValueError
        stage = ApprovedStage(
            recipe.stage.stage_id, recipe.stage.minimum_slots, recipe.runtime_plan.domain()
        )
        approved = ApprovedAdmissionRecipe(
            recipe.binding_revision_id,
            recipe.manifest_id,
            recipe.recipe_digest,
            recipe.policy_revision_id,
            recipe.service_class,
            (stage,),
            recipe.admission_expires_at_us,
            frozenset(recipe.quota_dimensions),
        )
        return TtsRecipe(
            recipe.model_id,
            recipe.stage.stage_id,
            recipe.stage.instance_id,
            recipe.stage.reserved_slots,
            approved,
        )
    except ValueError, TypeError, KeyError, UnicodeError, RecursionError, OSError:
        raise ValueError("Invalid private speech admission recipe") from None
