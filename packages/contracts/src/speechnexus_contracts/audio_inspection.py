"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, cast

from . import audio_inspection_models as models
from .validation import validate_profile


def parse(name: Literal["AudioInspectionSuccess"], value: Any) -> models.AudioInspectionSuccess:
    return cast(models.AudioInspectionSuccess, validate_profile("audio_inspection", name, value))
